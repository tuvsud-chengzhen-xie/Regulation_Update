"""Change classification, traceability between runs, and fetch-failure recording.

Runs last (alphabetically) because `clean_db` drops every table.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock
from xml.sax.saxutils import escape

import httpx
import pytest

from app.config import settings
from app.extractors.base import ExtractorError, dedupe
from app.extractors.cen import CENExtractor
from app.extractors.fetching import FetchContext, FetchError
from app.extractors.references import describe_reference
from app.models import (
    ISSUE_SOURCE,
    POLICY_SOURCE_DATE,
    STATUS_CURRENT,
    STATUS_WITHDRAWN,
    ChangeRecord,
    ExtractionIssue,
    FetchAttempt,
    Source,
    SourceRunResult,
    Standard,
)
from app.services.ingest import run_extraction

TITLE = "Furniture - test title"


def cen_export(rows: list[tuple[str, str]]) -> bytes:
    """A CEN 'XLS export' (SpreadsheetML) with Reference | Date | Title rows."""
    body = "".join(
        "<Row>"
        f'<Cell><Data ss:Type="String">{escape(ref)}</Data></Cell>'
        f'<Cell><Data ss:Type="String">{date}</Data></Cell>'
        f'<Cell><Data ss:Type="String">{TITLE}</Data></Cell>'
        "</Row>"
        for ref, date in rows
    )
    return (
        '<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="P">'
        "<Table><Row><Cell><Data ss:Type=\"String\">Reference</Data></Cell>"
        "<Cell><Data ss:Type=\"String\">Date</Data></Cell>"
        "<Cell><Data ss:Type=\"String\">Title</Data></Cell></Row>"
        f"{body}</Table></Worksheet></Workbook>"
    ).encode()


class Catalogue:
    """Serves whatever CEN export the test sets, through the real parser."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def __call__(self, source, ctx):
        result = CENExtractor(source).parse(cen_export(self.rows))
        result.rows = dedupe(result.rows)
        return result


@pytest.fixture
def cen_source(clean_db):
    source = Source(
        body="CEN",
        tc_code="CEN/TC 207",
        url="https://standards.cencenelec.eu/x",
        publish_date_policy=POLICY_SOURCE_DATE,
    )
    clean_db.add(source)
    clean_db.commit()
    return source


@pytest.fixture
def catalogue():
    feed = Catalogue()
    with mock.patch("app.services.ingest.extract_source", feed):
        yield feed


def by_id(db) -> dict[str, Standard]:
    return {s.standard_id: s for s in db.query(Standard).all()}


# --- reference parsing --------------------------------------------------------

@pytest.mark.parametrize(
    ("body", "reference", "family", "edition", "kind", "amends"),
    [
        ("CEN", "EN 16121:2023", "EN 16121", "2023", "Standard", None),
        ("CEN", "EN 16121:2023/AC:2025", "EN 16121", "2023", "Corrigendum", "EN 16121:2023"),
        ("CEN", "EN 1335-1:2020+A1:2022", "EN 1335-1", "2020", "Standard", None),
        ("CEN", "EN 716-1:2017+AC:2019", "EN 716-1", "2017", "Standard", None),
        ("CEN", "CEN/TS 18231:2026", "CEN/TS 18231", "2026", "Technical Specification", None),
        ("CEN", "CEN/TR 15071:2020/AC:2021", "CEN/TR 15071", "2020", "Corrigendum",
         "CEN/TR 15071:2020"),
        ("ISO", "ISO 8124-1:2022/Amd 1:2023", "ISO 8124-1", "2022", "Amendment",
         "ISO 8124-1:2022"),
        ("ISO", "ISO 105-F01:2001", "ISO 105-F01", "2001", "Standard", None),
        ("ASTM", "ASTM F2388-21(2026)", "ASTM F2388", "21", "Standard", None),
        ("ALI", "ANSI/ALI A14.1-2018", "ANSI/ALI A14.1", "2018", "Standard", None),
        ("WSTDA", "WSTDA-WS-1-2015", "WSTDA-WS-1", "2015", "Standard", None),
    ],
)
def test_references_are_split_into_family_edition_and_kind(
    body, reference, family, edition, kind, amends
):
    info = describe_reference(reference, body)
    assert (info.family, info.edition, info.kind, info.amends) == (family, edition, kind, amends)


# --- CEN validation -----------------------------------------------------------

def cen_parser() -> CENExtractor:
    return CENExtractor(SimpleNamespace(tc_code="CEN/TC 207", url="https://x", options={}))


def test_cen_export_captures_every_corrigendum_as_its_own_record():
    extractor = cen_parser()
    rows = {r.standard_id for r in extractor.parse(extractor.sample_bytes("cen_tc207.xml")).rows}
    for reference in ("EN 16121:2023/AC:2025", "EN 1729-1:2015/AC:2016", "EN 1130:2019/AC:2020",
                      "EN 16121:2023", "EN 716-1:2017+AC:2019"):
        assert reference in rows


def test_cen_html_error_page_is_rejected_not_parsed_as_rows():
    page = (b"<!DOCTYPE html><html><body><h1>Sorry, this page isn't available</h1>"
            b"Session state protection violation</body></html>")
    with pytest.raises(ExtractorError, match="HTML page instead of the XLS export"):
        cen_parser().parse(page)


def test_cen_row_that_is_not_a_reference_is_reported_not_added():
    result = cen_parser().parse(cen_export([("EN 1022:2023", "2023-11-29"),
                                            ("Some heading text", "")]))
    assert [r.standard_id for r in result.rows] == ["EN 1022:2023"]
    assert [i.reason for i in result.issues if i.scope == "record"] == ["unrecognised_reference"]


# --- classification and traceability -----------------------------------------

def test_new_corrigenda_editions_and_withdrawals_are_classified_and_linked(
    clean_db, cen_source, catalogue
):
    catalogue.rows = [
        ("EN 16121:2023", "2023-12-20"),
        ("EN 581-1:2017", "2017-03-08"),
        ("EN 1130:2019", "2019-11-27"),
        ("EN 9999:2010", "2010-01-01"),
    ]
    first = run_extraction(clean_db, send_alerts=False)
    assert (first.added_count, first.updated_count) == (4, 0)

    catalogue.rows = [
        ("EN 16121:2023", "2023-12-20"),
        ("EN 16121:2023/AC:2025", "2025-04-23"),  # new corrigendum
        ("EN 581-1:2026", "2026-05-20"),           # new edition replaces 2017
        ("EN 1130:2019", "2019-11-27"),
        ("EN 20000:2026", "2026-10-07"),           # brand new standard
    ]                                              # EN 9999:2010 removed
    second = run_extraction(clean_db, send_alerts=False)
    assert second.added_count == 1
    assert second.updated_count == 2
    assert second.superseded_count == 1
    assert second.withdrawn_count == 1

    rows = by_id(clean_db)
    old, new = rows["EN 581-1:2017"], rows["EN 581-1:2026"]
    assert old.status == STATUS_WITHDRAWN
    assert old.superseded_by == "EN 581-1:2026"
    assert old.withdrawn_run_id == second.id
    assert old.last_change_type == "superseded"
    assert new.status == STATUS_CURRENT and new.supersedes == "EN 581-1:2017"
    assert new.first_seen_run_id == second.id and new.last_change_type == "updated"

    ac = rows["EN 16121:2023/AC:2025"]
    assert (ac.publication_type, ac.family) == ("Corrigendum", "EN 16121")
    assert rows["EN 16121:2023"].status == STATUS_CURRENT  # a corrigendum withdraws nothing
    assert rows["EN 9999:2010"].last_change_type == "withdrawn"
    assert rows["EN 20000:2026"].last_change_type == "added"

    changes = {c.standard_id: c for c in clean_db.query(ChangeRecord).filter_by(run_id=second.id)}
    assert changes["EN 16121:2023/AC:2025"].details["relation"] == "corrigendum"
    assert changes["EN 16121:2023/AC:2025"].details["amends"] == "EN 16121:2023"
    assert changes["EN 581-1:2026"].details["relation"] == "new_edition"
    assert changes["EN 581-1:2017"].details["supersededBy"] == ["EN 581-1:2026"]
    assert changes["EN 581-1:2017"].details["lastSeenRunId"] == first.id

    # The removed standard comes back: reinstated, not duplicated.
    catalogue.rows.append(("EN 9999:2010", "2010-01-01"))
    third = run_extraction(clean_db, send_alerts=False)
    assert third.reinstated_count == 1 and third.added_count == 0
    back = by_id(clean_db)["EN 9999:2010"]
    assert back.status == STATUS_CURRENT and back.withdrawn_run_id is None
    assert clean_db.query(Standard).filter_by(standard_id="EN 9999:2010").count() == 1


def test_a_run_that_would_withdraw_most_of_a_source_is_held_back(
    clean_db, cen_source, catalogue, monkeypatch
):
    monkeypatch.setattr(settings, "max_withdrawal_ratio", 0.5)
    monkeypatch.setattr(settings, "withdrawal_guard_min_current", 2)
    catalogue.rows = [(f"EN {n}:2020", "2020-01-01") for n in range(100, 106)]
    run_extraction(clean_db, send_alerts=False)

    catalogue.rows = catalogue.rows[:1]
    held = run_extraction(clean_db, send_alerts=False)
    assert held.status == "failed" and held.withdrawn_count == 0
    assert clean_db.query(Standard).filter_by(status=STATUS_WITHDRAWN).count() == 0
    issue = clean_db.query(ExtractionIssue).filter_by(run_id=held.id, scope=ISSUE_SOURCE).one()
    assert issue.reason == "suspicious_withdrawal"
    result = clean_db.query(SourceRunResult).filter_by(run_id=held.id).one()
    assert result.error_cause == "suspicious_withdrawal"

    confirmed = run_extraction(clean_db, send_alerts=False, allow_mass_withdrawal=True)
    assert confirmed.withdrawn_count == 5


def test_fetch_failures_are_stored_with_cause_and_every_attempt(clean_db, cen_source):
    def failing(source, ctx):
        for attempt in (1, 2, 3):
            ctx.attempts.append(SimpleNamespace(
                url=source.url, transport="http", attempt=attempt, outcome="failed",
                cause="timeout", http_status=None, duration_ms=10, bytes=0, message="timed out",
            ))
        raise FetchError("timed out fetching CEN", cause="timeout", url=source.url)

    with mock.patch("app.services.ingest.extract_source", failing):
        run = run_extraction(clean_db, send_alerts=False)

    result = clean_db.query(SourceRunResult).filter_by(run_id=run.id).one()
    assert (result.status, result.error_cause, result.attempts) == ("failed", "timeout", 3)
    assert clean_db.query(FetchAttempt).filter_by(run_id=run.id).count() == 3
    issue = clean_db.query(ExtractionIssue).filter_by(run_id=run.id).one()
    assert issue.reason == "source_unavailable"
    assert "timeout" in issue.location and "did not respond in time" in issue.detail


# --- transport ---------------------------------------------------------------

def context(handler) -> FetchContext:
    ctx = FetchContext()
    ctx._client = httpx.Client(transport=httpx.MockTransport(handler))
    return ctx


def test_transient_http_errors_are_retried_then_succeed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=b"<ok/>")

    ctx = context(handler)
    assert ctx.http_get("https://www.astm.org/x", body="ASTM") == b"<ok/>"
    attempts = ctx.drain_attempts()
    assert [(a.outcome, a.cause, a.http_status) for a in attempts] == [
        ("failed", "http_5xx", 503), ("ok", None, None)
    ]


def test_permanent_http_errors_fail_at_once_with_their_cause():
    ctx = context(lambda request: httpx.Response(404))
    with pytest.raises(FetchError) as caught:
        ctx.http_get("https://www.astm.org/gone", body="ASTM")
    assert (caught.value.cause, caught.value.http_status) == ("not_found", 404)
    assert len(ctx.drain_attempts()) == 1


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (b"<html><title>Just a moment...</title></html>", "challenge"),
        (b"<html>Session state protection violation</html>", "error_page"),
        (b"   ", "empty_response"),
    ],
)
def test_pages_that_are_not_the_catalogue_are_failures(content, cause):
    ctx = context(lambda request: httpx.Response(200, content=content))
    with pytest.raises(FetchError) as caught:
        ctx.http_get("https://standards.cencenelec.eu/x", body="CEN")
    assert caught.value.cause == cause


def test_browser_sources_fall_back_to_https_when_no_browser_can_start():
    from app.extractors.wstda import WSTDAExtractor

    class Ctx:
        def browser_get(self, url, **_):
            raise FetchError("no chromium", cause="browser_unavailable", transient=False)

        def http_get(self, url, **kwargs):
            assert kwargs["transport"] == "http-fallback"
            return b"<html>listing</html>"

    extractor = WSTDAExtractor(SimpleNamespace(url="https://wstda.com/page/products",
                                               fetch_strategy="browser", options={}))
    assert extractor.fetch(Ctx()) == b"<html>listing</html>"
