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
        ("BIFMA", "BIFMA X5.1-2017(R2022)", "BIFMA X5.1", "2017", "Standard", None),
        ("ALI", "ANSI/ALI A14.1-2026 Errata", "ANSI/ALI A14.1", "2026", "Corrigendum",
         "ANSI/ALI A14.1-2026"),
        ("WSTDA", "WSTDA-WS-1-2015", "WSTDA-WS-1", "2015", "Standard", None),
    ],
)
def test_references_are_split_into_family_edition_and_kind(
    body, reference, family, edition, kind, amends
):
    info = describe_reference(reference, body)
    assert (info.family, info.edition, info.kind, info.amends) == (family, edition, kind, amends)


def test_reaffirmation_year_is_read():
    assert describe_reference("BIFMA X5.1-2017(R2022)", "BIFMA").reaffirmed == "2022"
    assert describe_reference("BIFMA X5.1-2017", "BIFMA").reaffirmed is None


def test_startup_repair_removes_yearless_iso_and_reads_reaffirmations(clean_db):
    """Rows stored by the earlier parsers are corrected in place."""
    from app.models import ExtractionRun
    from app.services.ingest import repair_misread_references

    iso = Source(body="ISO", tc_code="ISO/TC 61/SC 4", url="https://www.iso.org/x")
    bifma = Source(body="BIFMA", tc_code="BIFMA", url="https://www.madcad.com/x")
    run = ExtractionRun(status="success", trigger="manual", mode="live",
                        added_count=2, withdrawn_count=1)
    clean_db.add_all([iso, bifma, run])
    clean_db.flush()
    draft = Standard(source_id=iso.id, standard_id="ISO 22702", standard_id_norm="ISO 22702",
                     status=STATUS_WITHDRAWN, publish_date="2026/10")
    real = Standard(source_id=iso.id, standard_id="ISO 22702:2026",
                    standard_id_norm="ISO 22702:2026", status=STATUS_CURRENT,
                    publish_date="2026/10", supersedes="ISO 22702, ISO 22702:2018")
    chair = Standard(source_id=bifma.id, standard_id="BIFMA X5.1-2017",
                     standard_id_norm="BIFMA X5.1-2017", status=STATUS_CURRENT,
                     publish_date="2017", title_en="(R2022) Office Chairs")
    clean_db.add_all([draft, real, chair])
    clean_db.flush()
    clean_db.add_all([
        ChangeRecord(run_id=run.id, source_id=iso.id, standard_pk=draft.id,
                     standard_id=draft.standard_id, change_type="withdrawn", details={}),
        ChangeRecord(run_id=run.id, source_id=iso.id, standard_pk=real.id,
                     standard_id=real.standard_id, change_type="added",
                     details={"supersedes": ["ISO 22702", "ISO 22702:2018"]}),
        ChangeRecord(run_id=run.id, source_id=bifma.id, standard_pk=chair.id,
                     standard_id=chair.standard_id, change_type="added", details={}),
    ])
    clean_db.commit()

    assert repair_misread_references(clean_db) == {
        "removed_yearless_iso": 1, "reaffirmations_read": 1
    }
    rows = by_id(clean_db)
    assert "ISO 22702" not in rows
    assert rows["ISO 22702:2026"].supersedes == "ISO 22702:2018"
    fixed = rows["BIFMA X5.1-2017(R2022)"]
    assert (fixed.title_en, fixed.publish_date) == ("Office Chairs", "2022")
    assert fixed.standard_id_norm == "BIFMA X5.1-2017(R2022)"
    clean_db.refresh(run)
    assert run.withdrawn_count == 0
    changes = {c.standard_id: c for c in clean_db.query(ChangeRecord).all()}
    assert "ISO 22702" not in changes
    assert changes["ISO 22702:2026"].details["supersedes"] == ["ISO 22702:2018"]
    assert "BIFMA X5.1-2017(R2022)" in changes
    # Idempotent.
    assert repair_misread_references(clean_db) == {
        "removed_yearless_iso": 0, "reaffirmations_read": 0
    }


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


def test_bifma_duplicate_is_superseded_by_existing_reaffirmed_record(clean_db):
    from app.services.ingest import run_upload

    source = Source(body="BIFMA", tc_code="BIFMA", url="https://www.madcad.com/store/")
    clean_db.add(source)
    clean_db.flush()
    for reference in ("BIFMA X5.4-2020", "BIFMA X5.4-2020(R2025)"):
        clean_db.add(Standard(
            source_id=source.id, standard_id=reference, standard_id_norm=reference,
            family="BIFMA X5.4", status=STATUS_CURRENT,
            title_en="Public and Lounge Seating",
        ))
    clean_db.commit()
    payload = (
        b"<p>BIFMA X5.4-2020 Public and Lounge Seating</p>"
        b"<p>BIFMA X5.4-2020 (R2025) Public and Lounge Seating</p>"
    )
    run = run_upload(clean_db, source_id=source.id, payload=payload, send_alerts=False)
    assert run.superseded_count == 1 and run.withdrawn_count == 0
    rows = by_id(clean_db)
    assert rows["BIFMA X5.4-2020"].status == STATUS_WITHDRAWN
    assert rows["BIFMA X5.4-2020"].superseded_by == "BIFMA X5.4-2020(R2025)"
    reaffirmed = rows["BIFMA X5.4-2020(R2025)"]
    assert reaffirmed.status == STATUS_CURRENT
    assert reaffirmed.supersedes == "BIFMA X5.4-2020"
    again = run_upload(clean_db, source_id=source.id, payload=payload, send_alerts=False)
    assert again.superseded_count == 0 and again.reinstated_count == 0


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

    # A timeout is temporary, so the source is retried once at the end of the run.
    result = clean_db.query(SourceRunResult).filter_by(run_id=run.id).one()
    assert (result.status, result.error_cause, result.attempts) == ("failed", "timeout", 6)
    assert clean_db.query(FetchAttempt).filter_by(run_id=run.id).count() == 6
    issue = clean_db.query(ExtractionIssue).filter_by(run_id=run.id).one()
    assert issue.reason == "source_unavailable"
    assert "timeout" in issue.location and "did not respond in time" in issue.detail


def test_a_source_blocked_by_an_interstitial_is_retried_at_the_end_of_the_run(
    clean_db, cen_source
):
    good = Catalogue()
    good.rows = [("EN 581-1:2017", "2017-03-08")]
    calls = []

    def flaky(source, ctx):
        calls.append(source.id)
        ctx.attempts.append(SimpleNamespace(
            url=source.url, transport="browser", attempt=1,
            outcome="failed" if len(calls) == 1 else "ok",
            cause="challenge" if len(calls) == 1 else None,
            http_status=None, duration_ms=10, bytes=0, message=None,
        ))
        if len(calls) == 1:
            raise FetchError("interstitial never cleared", cause="challenge", url=source.url)
        return good(source, ctx)

    with mock.patch("app.services.ingest.extract_source", flaky):
        run = run_extraction(clean_db, send_alerts=False)

    assert len(calls) == 2
    assert (run.status, run.failed_source_count, run.added_count) == ("success", 0, 1)
    result = clean_db.query(SourceRunResult).filter_by(run_id=run.id).one()
    assert (result.status, result.attempts) == ("success", 2)
    assert clean_db.query(FetchAttempt).filter_by(run_id=run.id).count() == 2
    assert not clean_db.query(ExtractionIssue).filter_by(run_id=run.id, scope=ISSUE_SOURCE).all()


def test_a_parse_error_is_not_retried(clean_db, cen_source):
    calls = []

    def broken(source, ctx):
        calls.append(source.id)
        raise ExtractorError("catalogue page did not contain a standards table")

    with mock.patch("app.services.ingest.extract_source", broken):
        run = run_extraction(clean_db, send_alerts=False)
    assert len(calls) == 1 and run.status == "failed"


# --- CEN supersession relations ----------------------------------------------

CEN_PROJECT_RELATIONS = """
<div id="DASHBOARD_LISTRELATIONS"><table class="dashlist">
  <tr><th class="detail">Supersedes</th><td>
    <a href="f?p=CEN:110:::::FSP_PROJECT:77427&cs=16C1">EN 16232:2013+A2:2023</a><br/>
    <a href="f?p=CEN:110:::::FSP_PROJECT:66211&cs=1A27">EN 12790-2:2023</a><br/>
    <a href="f?p=CEN:110:::::FSP_PROJECT:66210&cs=13A1">EN 12790-1:2023</a>
  </td></tr>
  <tr><th class="detail">Superseded by</th><td> </td></tr>
  <tr><th class="detail">Normative reference (6)</th><td> </td></tr>
</table></div>"""
SUPERSEDED = ["EN 16232:2013+A2:2023", "EN 12790-2:2023", "EN 12790-1:2023"]


def test_cen_project_page_relations_are_parsed():
    from app.extractors.cen_relations import parse_relations, project_link

    supersedes, superseded_by = parse_relations(CEN_PROJECT_RELATIONS)
    assert supersedes == ["EN 16232:2013+A2:2023", "EN 12790-2:2023", "EN 12790-1:2023"]
    assert superseded_by == []

    search = ('<a href="f?p=CEN:110:::::FSP_PROJECT,FSP_ORG_ID:77256,6233&amp;cs=19F2">'
              'EN 12790:2026</a><a href="f?p=CEN:110:::::FSP_PROJECT:1&amp;cs=AA">EN 12790-1:2023</a>')
    assert project_link(search, "EN 12790:2026") == (
        "https://standards.cencenelec.eu/ords/f?p=CEN:110:::::FSP_PROJECT,FSP_ORG_ID:77256,6233&cs=19F2"
    )
    assert project_link(search, "EN 9999:2020") is None


def test_cen_one_to_many_supersession_is_linked_from_the_project_page(
    clean_db, cen_source, catalogue, monkeypatch
):
    from app.extractors import cen_relations

    catalogue.rows = [
        ("EN 12790-1:2023", "2023-01-01"), ("EN 12790-2:2023", "2023-01-01"),
        ("EN 16232:2013+A2:2023", "2023-01-01"), ("EN 1130:2019", "2019-11-27"),
    ]
    run_extraction(clean_db, send_alerts=False)

    monkeypatch.setattr(settings, "cen_relations_enabled", True)
    looked_up = []

    def lookup(ctx, reference):
        looked_up.append(reference)
        if reference == "EN 12790:2026":
            return cen_relations.CENRelations(reference, "https://x", list(SUPERSEDED), [])
        return None

    monkeypatch.setattr(cen_relations, "lookup", lookup)
    catalogue.rows = [("EN 12790:2026", "2026-11-04"), ("EN 1130:2019", "2019-11-27")]
    run = run_extraction(clean_db, send_alerts=False)

    assert looked_up == ["EN 12790:2026"]  # the old ones were linked by the new one
    assert (run.added_count, run.updated_count, run.withdrawn_count, run.superseded_count) == (
        0, 1, 0, 3)
    rows = by_id(clean_db)
    new = rows["EN 12790:2026"]
    assert new.supersedes == ", ".join(SUPERSEDED)
    assert new.last_change_type == "updated"
    for old in SUPERSEDED:
        assert rows[old].status == STATUS_WITHDRAWN
        assert rows[old].superseded_by == "EN 12790:2026"
        assert rows[old].last_change_type == "superseded"
    changes = {c.standard_id: c for c in clean_db.query(ChangeRecord).filter_by(run_id=run.id)}
    assert changes["EN 12790:2026"].change_type == "updated"
    assert changes["EN 12790:2026"].details["supersedes"] == SUPERSEDED
    assert changes["EN 12790-1:2023"].change_type == "superseded"
    assert changes["EN 12790-1:2023"].details["supersededBy"] == ["EN 12790:2026"]
    result = clean_db.query(SourceRunResult).filter_by(run_id=run.id).one()
    assert (result.added_count, result.updated_count, result.withdrawn_count,
            result.superseded_count) == (0, 1, 0, 3)


def test_a_failed_cen_lookup_is_reported_and_leaves_the_change_alone(
    clean_db, cen_source, catalogue, monkeypatch
):
    from app.extractors import cen_relations

    catalogue.rows = [("EN 1130:2019", "2019-11-27")]
    run_extraction(clean_db, send_alerts=False)

    monkeypatch.setattr(settings, "cen_relations_enabled", True)

    def lookup(ctx, reference):
        raise FetchError("CEN search timed out", cause="timeout")

    monkeypatch.setattr(cen_relations, "lookup", lookup)
    catalogue.rows = [("EN 20000:2026", "2026-10-07")]
    run = run_extraction(clean_db, send_alerts=False)
    assert run.status == "success"
    assert (run.added_count, run.withdrawn_count) == (1, 1)
    issues = clean_db.query(ExtractionIssue).filter_by(run_id=run.id, reason="relations_unavailable")
    assert {i.standard_id for i in issues} == {"EN 20000:2026", "EN 1130:2019"}


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


# --- subscriptions -----------------------------------------------------------

@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ({"status": "Succeeded"}, ("sent", None)),
        ({"status": "Failed", "error": {"message": "sender not allowed"}},
         ("failed", "ACS status Failed: sender not allowed")),
    ],
)
def test_acs_backend_sends_plain_text_from_the_configured_sender(monkeypatch, outcome, expected):
    from app.services import notify

    monkeypatch.setattr(settings, "email_backend", "acs")
    monkeypatch.setattr(settings, "email_from", "DoNotReply@example.azurecomm.net")
    poller = mock.Mock()
    poller.done.return_value = True
    poller.result.return_value = outcome
    client = mock.Mock()
    client.begin_send.return_value = poller
    monkeypatch.setattr(notify, "_acs_client", lambda: client)

    assert notify.send_email("user@tuvsud.com", "Subject", "Body") == expected
    sent = client.begin_send.call_args.args[0]
    assert sent["senderAddress"] == "DoNotReply@example.azurecomm.net"
    assert sent["recipients"] == {"to": [{"address": "user@tuvsud.com"}]}
    assert sent["content"] == {"subject": "Subject", "plainText": "Body"}


def test_acs_backend_without_endpoint_fails_the_alert_not_the_run(monkeypatch):
    from app.services import notify

    monkeypatch.setattr(settings, "email_backend", "acs")
    monkeypatch.setattr(settings, "acs_endpoint", "")
    monkeypatch.setattr(settings, "acs_connection_string", "")
    notify._acs_client.cache_clear()
    status, error = notify.send_email("user@tuvsud.com", "Subject", "Body")
    notify._acs_client.cache_clear()
    assert status == "failed" and "ACS_ENDPOINT" in error

@pytest.mark.parametrize("valid_catalogue", [True, False])
def test_iso_browser_challenge_falls_back_only_to_a_real_catalogue(valid_catalogue):
    from app.extractors.iso import ISOExtractor

    payload = (
        b"<table><tr><td class='entry-name'>ISO 4211-1:2025</td>"
        b"<td class='entry-description'>Furniture tests</td><td>60.60</td></tr></table>"
        if valid_catalogue else b"<html>catalogue loading...</html>"
    )
    browser_error = FetchError("verification pending", cause="challenge")
    ctx = mock.Mock()
    ctx.browser_get.side_effect = browser_error
    ctx.http_get.return_value = payload
    extractor = ISOExtractor(SimpleNamespace(
        url="https://www.iso.org/committee/52448/x/catalogue", tc_code="ISO/TC 136",
        fetch_strategy="browser", options={},
    ))
    if valid_catalogue:
        assert extractor.fetch(ctx) == payload
    else:
        with pytest.raises(FetchError) as caught:
            extractor.fetch(ctx)
        assert caught.value is browser_error
    assert ctx.http_get.call_args.kwargs["transport"] == "http-fallback"

def test_a_new_sources_first_capture_is_not_alerted_but_later_changes_are(
    clean_db, cen_source, catalogue
):
    from app.models import AlertLog, Subscription, User

    user = User(email="alerts@example.com", api_key="k-alerts", role="user")
    clean_db.add(user)
    clean_db.flush()
    clean_db.add(Subscription(user_id=user.id, type="body", value="CEN"))
    clean_db.commit()

    catalogue.rows = [("EN 581-1:2017", "2017-03-08"), ("EN 1130:2019", "2019-11-27")]
    baseline = run_extraction(clean_db)
    assert baseline.added_count == 2 and baseline.alerts_sent == 0

    catalogue.rows = [("EN 581-1:2026", "2026-05-20"), ("EN 1130:2019", "2019-11-27")]
    later = run_extraction(clean_db)
    assert later.alerts_sent == 1
    body = clean_db.query(AlertLog).filter_by(run_id=later.id).one().body
    assert "EN 581-1:2026" in body and "replaced by EN 581-1:2026" in body
    assert "Subscriptions page" in body and "/api/subscriptions" not in body


@pytest.mark.parametrize(
    ("kind", "value", "standard_id", "tc_code", "body", "expected"),
    [
        ("standard", "F1169", "ASTM F1169-25", "F15.18", "ASTM", True),
        ("standard", "ASTM F1169", "ASTM F1169-25", "F15.18", "ASTM", True),
        ("standard", "F116", "ASTM F1169-25", "F15.18", "ASTM", False),
        ("standard", "EN 16121", "EN 16121:2023/AC:2025", "CEN/TC 207", "CEN", True),
        ("standard", "EN 581-1", "EN 581-1:2026", "CEN/TC 207", "CEN", True),
        ("standard", "EN 581-2", "EN 581-1:2026", "CEN/TC 207", "CEN", False),
        ("committee", "F15", "ASTM F1169-25", "F15.18", "ASTM", True),
        ("committee", "F15.1", "ASTM F1169-25", "F15.18", "ASTM", False),
        ("committee", "ISO/TC 29", "ISO 525:2020", "ISO/TC 29/SC 5", "ISO", True),
    ],
)
def test_subscription_matching(kind, value, standard_id, tc_code, body, expected):
    from app.services.notify import subscription_matches

    change = SimpleNamespace(standard_id=standard_id, tc_code=tc_code, title_en=None, details={})
    source = SimpleNamespace(body=body, tc_code=tc_code, title=None)
    subscription = SimpleNamespace(type=kind, value=value)
    assert subscription_matches(subscription, change, source) is expected
