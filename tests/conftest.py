"""Test fixtures.

Environment variables are set before `app.config` is imported so that every
test run uses an isolated database, outbox and training-data directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="standards-monitor-tests-"))
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{TMP / 'test.db'}",
        "EXPORT_DIR": str(TMP / "exports"),
        "OUTBOX_DIR": str(TMP / "outbox"),
        "TRAINING_DATA_DIR": str(TMP / "AI_Training_Data"),
        "EMAIL_BACKEND": "console",
        "SCHEDULER_ENABLED": "false",
        "LIVE_FETCH_DELAY_SECONDS": "0",
        "BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "BOOTSTRAP_ADMIN_API_KEY": "test-admin-key",
    }
)

from unittest import mock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.extractors.base import ExtractorError, dedupe  # noqa: E402
from app.extractors.registry import get_extractor  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.seed import bootstrap  # noqa: E402

ADMIN_KEY = "test-admin-key"


# --- offline extraction ------------------------------------------------------
# Extraction is live in production. Tests must never reach the network, so the
# fetch step is replaced with bundled sample payloads while the *real* parsers
# still run — the parsers are what the tests are actually checking.

SAMPLE_PAYLOADS: dict[str, str] = {
    "CEN/TC 207": "cen_tc207.xml",
    "CEN/TC 52": "cen_tc52.xml",
    "CEN/TC 93": "cen_tc93.xml",
    "ISO/TC 136": "iso_tc136.html",
    "ISO/TC 181": "iso_tc181.html",
    "ISO/TC 29/SC 5": "iso_tc29sc5.html",
    "F15.42": "astm_f1542.html",
    "F15.22": "astm_f1522.html",
    "F15.24": "astm_f1524.html",
    "A112.18.1/CSA B125.1": "asme_products.json",
    "B94": "asme_products.json",
    "B107": "asme_products.json",
    "PASE": "asme_products.json",
    "BIFMA": "bifma_store.html",
    "ALI": "ali_a14.html",
    "WSTDA": "wstda_products.html",
}


class SamplePayloads:
    """Controls which sample payload each source serves during a test."""

    def __init__(self) -> None:
        self.overrides: dict[str, str | None] = {}

    def set(self, tc_code: str, name: str | None) -> None:
        self.overrides[tc_code] = name

    def reset(self) -> None:
        self.overrides.clear()

    def name_for(self, source) -> str | None:
        if source.tc_code in self.overrides:
            return self.overrides[source.tc_code]
        return SAMPLE_PAYLOADS.get(source.tc_code)


PAYLOADS = SamplePayloads()


def _offline_extract_source(source, ctx):
    name = PAYLOADS.name_for(source)
    if name is None:
        raise ExtractorError(f"no sample payload configured for {source.key}")
    extractor = get_extractor(source)
    result = extractor.parse(extractor.sample_bytes(name))
    result.rows = dedupe(result.rows)
    return result


@pytest.fixture(scope="session", autouse=True)
def offline_extraction():
    with mock.patch("app.services.ingest.extract_source", _offline_extract_source):
        yield PAYLOADS


@pytest.fixture
def payloads():
    """Per-test control over the sample payload a source serves."""
    PAYLOADS.reset()
    yield PAYLOADS
    PAYLOADS.reset()


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    init_db()
    session = SessionLocal()
    try:
        bootstrap(session)
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": ADMIN_KEY}


@pytest.fixture
def clean_db():
    """A database with schema but no rows (for diff/ingest unit tests)."""
    from app.db import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
