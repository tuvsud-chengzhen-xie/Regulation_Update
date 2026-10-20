"""Extractor contract shared by every standards body.

An extractor turns a raw payload (an Excel export, or an HTML page) into rows
already shaped like the target database format. Fetching and parsing are
deliberately separate so parsers can be unit-tested against sample payloads
without touching the network.

Two things every parser must do:

* return an `ExtractionIssue` for **every entry it could not turn into a
  record**, instead of silently dropping it. A silently dropped entry looks
  exactly like a standard that has left the catalogue, which is the one thing
  the update rule must never guess at;
* leave `publish_date` as `None` when the source does not expose one, so the
  ingest layer can apply the stamping rule rather than inventing a date.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_DIR = Path(__file__).resolve().parent / "fixtures"

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_WS_RE = re.compile(r"\s+")

#: An issue that cost us one catalogue entry — the record is NOT in the database.
ISSUE_RECORD = "record"
#: The record was captured, but one of its fields could not be read.
ISSUE_FIELD = "field"
#: An issue that cost us the whole source (fetch failure, challenge page, ...).
ISSUE_SOURCE = "source"

RAW_SNIPPET_LIMIT = 400


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and normalise unicode punctuation."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace("\xa0", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def normalize_standard_id(standard_id: str) -> str:
    """Comparison key for the update rule.

    The rule compares "full information of Standard ID + Version", so the key
    keeps the version but ignores cosmetic differences (dash style, spacing,
    case) that the source sites are inconsistent about.
    """
    text = clean_text(standard_id) or ""
    text = text.translate(_DASHES)
    text = text.replace(" - ", "-")
    text = _WS_RE.sub(" ", text)
    return text.upper().strip()


@dataclass(slots=True)
class ExtractedStandard:
    """One row as captured from a source, before database reconciliation."""

    standard_id: str
    tc_code: str | None = None
    title_en: str | None = None
    title_zh: str | None = None
    # Raw publish date exactly as the source gave it; `None` means the source
    # does not expose one and the ingest layer applies the stamping rule.
    publish_date: str | None = None
    implement_date: str | None = None
    source_url: str | None = None
    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.standard_id = clean_text(self.standard_id) or ""
        self.tc_code = clean_text(self.tc_code)
        self.title_en = clean_text(self.title_en)
        self.title_zh = clean_text(self.title_zh)
        self.publish_date = clean_text(self.publish_date)
        self.implement_date = clean_text(self.implement_date)

    @property
    def key(self) -> str:
        return normalize_standard_id(self.standard_id)


@dataclass(slots=True)
class ExtractionIssue:
    """One catalogue entry (or one whole source) that could not be captured.

    `raw` carries the source text verbatim so a user can look at what the site
    actually said and decide whether the parser or the site is at fault.
    """

    reason: str  # short stable code, e.g. "missing_edition_year"
    detail: str  # one human sentence
    raw: str = ""
    standard_id: str | None = None  # partial identifier, when one was recognised
    scope: str = ISSUE_RECORD
    location: str | None = None  # row number, line number, page — where it was

    def __post_init__(self) -> None:
        self.raw = (clean_text(self.raw) or "")[:RAW_SNIPPET_LIMIT]
        self.standard_id = clean_text(self.standard_id)


@dataclass(slots=True)
class ParseResult:
    """What a parser produced: the rows it captured and what it could not."""

    rows: list[ExtractedStandard] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)

    def add(self, row: ExtractedStandard) -> None:
        self.rows.append(row)

    def fail(
        self,
        reason: str,
        detail: str,
        *,
        raw: str = "",
        standard_id: str | None = None,
        location: str | None = None,
    ) -> None:
        """Record one catalogue entry we could not turn into a standard."""
        self.issues.append(
            ExtractionIssue(
                reason=reason,
                detail=detail,
                raw=raw,
                standard_id=standard_id,
                scope=ISSUE_RECORD,
                location=location,
            )
        )

    def warn(
        self,
        reason: str,
        detail: str,
        *,
        raw: str = "",
        standard_id: str | None = None,
        location: str | None = None,
    ) -> None:
        """Record a field we could not read on an entry we did capture."""
        self.issues.append(
            ExtractionIssue(
                reason=reason,
                detail=detail,
                raw=raw,
                standard_id=standard_id,
                scope=ISSUE_FIELD,
                location=location,
            )
        )

    @property
    def failed_count(self) -> int:
        """Entries lost entirely. Incomplete fields are counted separately."""
        return sum(1 for issue in self.issues if issue.scope == ISSUE_RECORD)

    @property
    def incomplete_count(self) -> int:
        return sum(1 for issue in self.issues if issue.scope == ISSUE_FIELD)


class ExtractorError(RuntimeError):
    """Raised when a source cannot be fetched or parsed at all."""


class BaseExtractor(ABC):
    """Base class for all standards-body extractors."""

    body: str = ""
    #: Default transport for this body. A source row may override it.
    #: "http"    — plain HTTPS request (fast; fine for open endpoints)
    #: "browser" — rendered in a real browser (needed for JS-built listings
    #:             and for sites behind an interstitial check)
    fetch_strategy: str = "http"
    #: Extra request headers for the plain-HTTP transport.
    extra_headers: dict[str, str] = {}
    #: Browser transport hints.
    browser_wait_for: str | None = None  # CSS selector that means "content is in"
    browser_wait_until: str = "domcontentloaded"

    def __init__(self, source) -> None:
        self.source = source

    # --- fetching ----------------------------------------------------------
    def target_url(self) -> str:
        """The URL to fetch. Overridden where a filter must be forced on."""
        return self.source.url

    def strategy(self) -> str:
        return getattr(self.source, "fetch_strategy", None) or self.fetch_strategy

    def fetch(self, ctx) -> bytes:
        """Download the raw payload using this source's transport.

        If the headless browser cannot start on this host, a browser-backed
        source is tried once over plain HTTPS before it is given up on, so a
        missing Chromium only costs the sites that truly need one.
        """
        from app.config import settings
        from app.extractors.fetching import CAUSE_BROWSER_UNAVAILABLE, FetchError

        url = self.target_url()
        if self.strategy() != "browser":
            return ctx.http_get(url, body=self.body, headers=self.extra_headers)
        try:
            return ctx.browser_get(
                url,
                body=self.body,
                wait_for=self.browser_wait_for,
                wait_until=self.browser_wait_until,
            )
        except FetchError as exc:
            if exc.cause != CAUSE_BROWSER_UNAVAILABLE or not settings.browser_http_fallback:
                raise
            logger.warning("%s: browser unavailable, falling back to plain HTTPS for %s",
                           self.body, url)
            try:
                return ctx.http_get(
                    url, body=self.body, headers=self.extra_headers, transport="http-fallback"
                )
            except FetchError as fallback_exc:
                raise FetchError(
                    f"{exc} Plain-HTTPS fallback also failed: {fallback_exc}",
                    cause=CAUSE_BROWSER_UNAVAILABLE,
                    url=url,
                    http_status=fallback_exc.http_status,
                    transient=False,
                ) from fallback_exc

    def sample_bytes(self, name: str | None = None) -> bytes:
        """Read a bundled sample payload. Used by the parser unit tests."""
        path = SAMPLE_DIR / (name or f"{self.body.lower()}_sample.html")
        if not path.exists():
            raise ExtractorError(f"sample payload not found: {path.name}")
        return path.read_bytes()

    # --- parsing -----------------------------------------------------------
    @abstractmethod
    def parse(self, payload: bytes) -> ParseResult:
        """Turn a raw payload into rows plus a report of what failed."""

    # --- orchestration -----------------------------------------------------
    def extract(self, ctx) -> ParseResult:
        result = self.parse(self.fetch(ctx))
        result.rows = dedupe(result.rows)
        return result


def dedupe(rows: list[ExtractedStandard]) -> list[ExtractedStandard]:
    """Drop repeated Standard IDs, keeping the first (richest) occurrence.

    A repeat is not a failure — catalogues list the same reference under
    several headings — so this is deliberately silent.
    """
    seen: dict[str, ExtractedStandard] = {}
    for row in rows:
        if not row.standard_id:
            continue
        key = row.key
        if key in seen:
            existing = seen[key]
            existing.title_en = existing.title_en or row.title_en
            existing.publish_date = existing.publish_date or row.publish_date
            continue
        seen[key] = row
    return list(seen.values())


def decode(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")
