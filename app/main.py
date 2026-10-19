"""Automated Standard Update Monitoring — FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import SessionLocal, init_db
from app.routers import export, runs, sources, standards, subscriptions, training, users
from app.seed import bootstrap
from app.services.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DS_DIR = Path(__file__).resolve().parent / "ds"

DESCRIPTION = """
Periodically captures standard publication/update information from selected
standards bodies, compares it with the internal monitoring database, records
the changes, and alerts subscribed authorised users.

**Monitored bodies:** CEN, ISO, ASTM, ASME, BIFMA, ALI, WSTDA.

**Update rule** — standards are compared on the full *Standard ID + Version*:
a record that disappears from its source becomes `Withdrawn`; a record that
appears for the first time is added as `Current`.

**Authentication** — every request carries an individual API key in the
`X-API-Key` header. Administrator-only: user and source management, triggering
runs, and the Excel export.

Annotation activity is captured in parallel as W3C-compliant training data
(`/ai_training_schema`).
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        info = bootstrap(db)
        logger.info("bootstrap: admin=%s new sources=%s", info["admin"], info["sources_created"])
    finally:
        db.close()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=DESCRIPTION,
    lifespan=lifespan,
    contact={"name": "TÜV SÜD"},
)

for router in (
    standards.router,
    sources.router,
    runs.router,
    subscriptions.router,
    users.router,
    export.router,
    training.router,
):
    app.include_router(router)


@app.get("/health", tags=["meta"], summary="Liveness probe")
def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "extraction_mode": "live",  # extraction is always live; no replay mode
        "scheduler_enabled": settings.scheduler_enabled,
        "email_backend": settings.email_backend,
        "training_capture": settings.training_capture_enabled,
    }


# Paths follow the CPS CoE design-system convention: the shared design system
# is mounted at /ds and app assets at /static, so the markup in shell.html works
# unchanged and the design system can be replaced wholesale on an update.
if DS_DIR.exists():
    app.mount("/ds", StaticFiles(directory=DS_DIR), name="ds")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return RedirectResponse("/docs")
