"""The update rule, including the worked example from the specification."""

from __future__ import annotations

from datetime import datetime, timezone

from app.extractors.base import ExtractedStandard
from app.models import (
    POLICY_SOURCE_DATE,
    POLICY_YEAR_THEN_STAMP,
    STATUS_CURRENT,
    STATUS_WITHDRAWN,
    Source,
    Standard,
)
from app.services.dates import extraction_stamp, resolve_publish_date
from app.services.diff import diff_source
from app.services.ingest import run_extraction

MOMENT = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)


def make_standard(standard_id, status=STATUS_CURRENT, **kwargs):
    return Standard(
        source_id=1,
        standard_id=standard_id,
        standard_id_norm=standard_id.upper(),
        status=status,
        **kwargs,
    )


def test_specification_example_en_581_1():
    """DB holds EN 581-1:1998; the new run returns EN 581-1:2026 only."""
    existing = [make_standard("EN 581-1:1998", title_en="Outdoor furniture")]
    extracted = [ExtractedStandard(standard_id="EN 581-1:2026", title_en="Outdoor furniture")]

    diff = diff_source(existing, extracted)

    assert [s.standard_id for s in diff.withdrawn] == ["EN 581-1:1998"]
    assert [s.standard_id for s in diff.added] == ["EN 581-1:2026"]
    assert not diff.modified and not diff.unchanged


def test_unchanged_standards_are_left_alone():
    existing = [make_standard("EN 1022:2023", title_en="Furniture - Seating")]
    extracted = [ExtractedStandard(standard_id="EN 1022:2023", title_en="Furniture - Seating")]
    diff = diff_source(existing, extracted)
    assert diff.total_changes == 0
    assert len(diff.unchanged) == 1


def test_comparison_key_ignores_cosmetic_differences():
    existing = [make_standard("EN 1022:2023")]
    extracted = [ExtractedStandard(standard_id="en  1022:2023")]
    assert diff_source(existing, extracted).total_changes == 0


def test_metadata_change_does_not_change_status():
    existing = [make_standard("EN 1022:2023", title_en="Old title")]
    extracted = [ExtractedStandard(standard_id="EN 1022:2023", title_en="New title")]
    diff = diff_source(existing, extracted)
    assert not diff.withdrawn and not diff.added
    standard, _, changes = diff.modified[0]
    assert changes[0].field == "title_en"
    assert standard.status == STATUS_CURRENT


def test_withdrawn_standard_reappearing_is_reinstated_not_duplicated():
    existing = [make_standard("EN 1022:2023", status=STATUS_WITHDRAWN)]
    extracted = [ExtractedStandard(standard_id="EN 1022:2023")]
    diff = diff_source(existing, extracted)
    assert not diff.added
    assert len(diff.reinstated) == 1


def test_a_missing_optional_field_never_blanks_a_stored_value():
    existing = [make_standard("EN 1022:2023", title_en="Known title")]
    extracted = [ExtractedStandard(standard_id="EN 1022:2023", title_en=None)]
    assert diff_source(existing, extracted).total_changes == 0


# --- publish-date rules -----------------------------------------------------

def test_cen_keeps_the_source_date():
    assert (
        resolve_publish_date(
            "2012-07-18", policy=POLICY_SOURCE_DATE, is_first_capture=True, moment=MOMENT
        )
        == "2012-07-18"
    )


def test_first_run_stores_the_source_year():
    assert (
        resolve_publish_date(
            "2000", policy=POLICY_YEAR_THEN_STAMP, is_first_capture=True, moment=MOMENT
        )
        == "2000"
    )


def test_later_runs_stamp_year_and_extraction_month():
    assert (
        resolve_publish_date(
            "2024", policy=POLICY_YEAR_THEN_STAMP, is_first_capture=False, moment=MOMENT
        )
        == "2026/08"
    )
    assert extraction_stamp(MOMENT) == "2026/08"


def test_missing_date_falls_back_to_the_registration_stamp():
    assert (
        resolve_publish_date(
            None, policy=POLICY_SOURCE_DATE, is_first_capture=True, moment=MOMENT
        )
        == "2026/08"
    )


# --- end-to-end through the ingest service ----------------------------------

def test_ingest_applies_the_rule_across_two_runs(clean_db, payloads):
    source = Source(
        body="CEN",
        tc_code="CEN/TC 207",
        title="Furniture",
        url="https://example.invalid",
        publish_date_policy=POLICY_SOURCE_DATE,
    )
    clean_db.add(source)
    clean_db.commit()

    first = run_extraction(clean_db, send_alerts=False)
    assert first.status == "success"
    assert first.added_count > 50
    assert first.withdrawn_count == 0

    second = run_extraction(clean_db, send_alerts=False)
    assert second.added_count == 0
    assert second.withdrawn_count == 0
    assert second.status == "success"


def test_a_broken_source_never_withdraws_anything(clean_db, payloads):
    source = Source(
        body="CEN",
        tc_code="CEN/TC 207",
        url="https://example.invalid",
        publish_date_policy=POLICY_SOURCE_DATE,
    )
    clean_db.add(source)
    clean_db.commit()
    run_extraction(clean_db, send_alerts=False)

    payloads.set("CEN/TC 207", "does_not_exist.html")

    run = run_extraction(clean_db, send_alerts=False)
    assert run.status == "failed"
    assert run.withdrawn_count == 0
    assert all(
        s.status == STATUS_CURRENT
        for s in clean_db.query(Standard).all()
    )


def test_a_failed_source_is_recorded_as_an_issue(clean_db, payloads):
    """A whole source that could not be fetched is a visible gap, not a log line."""
    from app.models import ISSUE_SOURCE, ExtractionIssue

    clean_db.add(
        Source(
            body="CEN",
            tc_code="CEN/TC 207",
            url="https://example.invalid",
            publish_date_policy=POLICY_SOURCE_DATE,
        )
    )
    clean_db.commit()
    payloads.set("CEN/TC 207", None)

    run = run_extraction(clean_db, send_alerts=False)
    issues = clean_db.query(ExtractionIssue).filter_by(run_id=run.id).all()
    assert [i.scope for i in issues] == [ISSUE_SOURCE]
    assert issues[0].reason == "source_unavailable"
    assert "CEN/TC 207" in issues[0].detail


def test_entries_that_cannot_be_captured_are_reported(clean_db, payloads):
    """A listing entry with no edition year must be reported, never dropped."""
    from app.models import ISSUE_RECORD, ExtractionIssue

    clean_db.add(Source(body="ALI", tc_code="ALI", url="https://example.invalid"))
    clean_db.commit()

    run = run_extraction(clean_db, send_alerts=False)
    lost = (
        clean_db.query(ExtractionIssue)
        .filter_by(run_id=run.id, scope=ISSUE_RECORD)
        .all()
    )
    assert lost, "the ALI sample contains an entry with no edition year"
    assert run.failed_record_count == len(lost)
    issue = lost[0]
    assert issue.reason == "missing_edition_year"
    assert issue.standard_id and issue.raw  # the user can see what failed
    assert issue.location.startswith("line ")
