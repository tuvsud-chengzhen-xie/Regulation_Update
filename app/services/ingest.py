"""Monitoring run orchestration: extract -> reconcile -> record -> alert."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractors.base import ExtractedStandard, normalize_standard_id
from app.extractors.registry import ExtractorError, build_context, extract_source, get_extractor
from app.models import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_REINSTATED,
    CHANGE_WITHDRAWN,
    ISSUE_SOURCE,
    POLICY_SOURCE_DATE,
    STATUS_CURRENT,
    STATUS_WITHDRAWN,
    ChangeRecord,
    ExtractionIssue,
    ExtractionRun,
    Source,
    SourceRunResult,
    Standard,
)
from app.services import training
from app.services.dates import resolve_publish_date
from app.services.diff import diff_source

logger = logging.getLogger(__name__)


def run_extraction(
    db: Session,
    *,
    trigger: str = "manual",
    source_ids: list[int] | None = None,
    send_alerts: bool = True,
    session_id: str | None = None,
) -> ExtractionRun:
    """Execute one monitoring run: every source is fetched live from its site."""
    session_id = session_id or training.SERVER_SESSION_ID
    moment = datetime.now(timezone.utc)

    query = select(Source).where(Source.is_active.is_(True)).order_by(Source.body, Source.tc_code)
    if source_ids:
        query = query.where(Source.id.in_(source_ids))
    sources = list(db.scalars(query))

    run = ExtractionRun(
        started_at=moment,
        status="running",
        trigger=trigger,
        mode="live",
        is_first_run=db.scalar(select(Standard.id).limit(1)) is None,
    )
    db.add(run)
    db.flush()

    # One context for the whole run: a shared HTTP client, plus a single
    # headless browser started lazily the first time a browser-backed source
    # is reached.
    with build_context() as ctx:
        for index, source in enumerate(sources):
            if index:
                time.sleep(settings.live_fetch_delay_seconds)  # be polite to the sources
            _process_source(
                db,
                run=run,
                source=source,
                ctx=ctx,
                moment=moment,
                session_id=session_id,
            )

    statuses = {result.status for result in run.results if result.status != "skipped"}
    if not statuses:
        run.status = "success"
        run.message = "no active sources"
    elif statuses == {"failed"}:
        run.status = "failed"
    elif "failed" in statuses:
        run.status = "partial"
    else:
        run.status = "success"
    run.finished_at = datetime.now(timezone.utc)
    db.flush()

    if send_alerts:
        from app.services.notify import dispatch_alerts

        run.alerts_sent = dispatch_alerts(db, run, session_id=session_id)

    db.commit()
    db.refresh(run)
    return run


def _process_source(
    db: Session,
    *,
    run: ExtractionRun,
    source: Source,
    ctx,
    moment: datetime,
    session_id: str,
) -> None:
    started = time.perf_counter()
    strategy = get_extractor(source).strategy()
    result = SourceRunResult(run_id=run.id, source_id=source.id, fetch_strategy=strategy)
    db.add(result)

    existing = list(db.scalars(select(Standard).where(Standard.source_id == source.id)))
    is_first_capture = not existing

    try:
        parsed = extract_source(source, ctx)
        extracted = parsed.rows
        if not extracted and existing:
            # A source that suddenly returns nothing is far more likely to be
            # broken than to have withdrawn its whole catalogue. Refuse to
            # apply the withdrawal rule on no evidence.
            raise ExtractorError(
                "extraction returned 0 standards while the database holds "
                f"{len(existing)}; the withdrawal rule was not applied"
            )
    except Exception as exc:  # noqa: BLE001 - any failure must be contained per source
        logger.warning("extraction failed for %s: %s", source.key, exc)
        result.status = "failed"
        result.error = str(exc)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        # The whole source is a gap in the database; record it alongside the
        # per-record issues so one screen shows everything that was missed.
        db.add(
            ExtractionIssue(
                run_id=run.id,
                source_id=source.id,
                scope=ISSUE_SOURCE,
                reason="source_unavailable",
                detail=str(exc),
                raw=source.url,
                location=f"{strategy} fetch",
            )
        )
        db.flush()
        return

    _record_issues(db, run=run, source=source, parsed=parsed)

    diff = diff_source(
        existing,
        extracted,
        compare_publish_date=source.publish_date_policy == POLICY_SOURCE_DATE,
    )

    for incoming in diff.added:
        standard = _create_standard(
            db, source=source, incoming=incoming, run=run, moment=moment, first=is_first_capture
        )
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=CHANGE_ADDED,
            old_status=None,
            new_status=STATUS_CURRENT,
            session_id=session_id,
            action="standard_create",
            before=None,
            after=training.standard_state(standard),
        )

    for standard in diff.withdrawn:
        before = training.standard_state(standard)
        standard.status = STATUS_WITHDRAWN
        standard.withdrawn_at = moment
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=CHANGE_WITHDRAWN,
            old_status=STATUS_CURRENT,
            new_status=STATUS_WITHDRAWN,
            session_id=session_id,
            action="standard_withdraw",
            before=before,
            after=training.standard_state(standard),
        )

    for standard, incoming in diff.reinstated:
        before = training.standard_state(standard)
        standard.status = STATUS_CURRENT
        standard.withdrawn_at = None
        standard.last_seen_run_id = run.id
        _apply_incoming_fields(standard, incoming)
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=CHANGE_REINSTATED,
            old_status=STATUS_WITHDRAWN,
            new_status=STATUS_CURRENT,
            session_id=session_id,
            action="standard_reinstate",
            before=before,
            after=training.standard_state(standard),
        )

    for standard, incoming, field_changes in diff.modified:
        before = training.standard_state(standard)
        for change in field_changes:
            setattr(standard, change.field, change.new)
        standard.last_seen_run_id = run.id
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=CHANGE_MODIFIED,
            old_status=standard.status,
            new_status=standard.status,
            session_id=session_id,
            action="standard_edit",
            before=before,
            after=training.standard_state(standard),
            details={"fields": [c.field for c in field_changes]},
        )

    for standard, _ in diff.unchanged:
        standard.last_seen_run_id = run.id

    result.status = "success"
    result.extracted_count = len(extracted)
    result.added_count = len(diff.added)
    result.withdrawn_count = len(diff.withdrawn)
    result.reinstated_count = len(diff.reinstated)
    result.modified_count = len(diff.modified)
    result.failed_record_count = parsed.failed_count
    result.duration_ms = int((time.perf_counter() - started) * 1000)

    run.extracted_count += result.extracted_count
    run.added_count += result.added_count
    run.withdrawn_count += result.withdrawn_count
    run.reinstated_count += result.reinstated_count
    run.modified_count += result.modified_count
    run.failed_record_count += result.failed_record_count
    source.last_run_at = moment
    db.flush()

    _emit_training_records(db, run=run, source=source, result=result, session_id=session_id)


def _record_issues(db: Session, *, run: ExtractionRun, source: Source, parsed) -> None:
    """Persist everything the parser could not capture from this source."""
    for issue in parsed.issues:
        db.add(
            ExtractionIssue(
                run_id=run.id,
                source_id=source.id,
                scope=issue.scope,
                reason=issue.reason,
                detail=issue.detail,
                raw=issue.raw or None,
                standard_id=issue.standard_id,
                location=issue.location,
            )
        )
    if parsed.issues:
        db.flush()


def _create_standard(
    db: Session,
    *,
    source: Source,
    incoming: ExtractedStandard,
    run: ExtractionRun,
    moment: datetime,
    first: bool,
) -> Standard:
    standard = Standard(
        source_id=source.id,
        standard_id=incoming.standard_id,
        standard_id_norm=incoming.key,
        tc_code=incoming.tc_code or source.tc_code,
        title_zh=incoming.title_zh,
        title_en=incoming.title_en,
        publish_date=resolve_publish_date(
            incoming.publish_date,
            policy=source.publish_date_policy,
            is_first_capture=first,
            moment=moment,
        ),
        implement_date=incoming.implement_date,
        status=STATUS_CURRENT,
        source_url=incoming.source_url or source.url,
        first_seen_run_id=run.id,
        last_seen_run_id=run.id,
    )
    standard.source = source
    db.add(standard)
    db.flush()
    return standard


def _apply_incoming_fields(standard: Standard, incoming: ExtractedStandard) -> None:
    for field in ("title_en", "title_zh", "implement_date"):
        value = getattr(incoming, field, None)
        if value:
            setattr(standard, field, value)


def _log_change(
    db: Session,
    *,
    run: ExtractionRun,
    source: Source,
    standard: Standard,
    change_type: str,
    old_status: str | None,
    new_status: str | None,
    session_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    details: dict | None = None,
) -> None:
    db.add(
        ChangeRecord(
            run_id=run.id,
            source_id=source.id,
            standard_pk=standard.id,
            standard_id=standard.standard_id,
            tc_code=standard.tc_code,
            title_en=standard.title_en,
            change_type=change_type,
            old_status=old_status,
            new_status=new_status,
            details=details or {},
        )
    )
    training.record_event(
        session_id,
        source.project_name,
        training.build_event(
            action=action,
            session_id=session_id,
            source_key=source.key,
            standard_id=standard.standard_id,
            before=before,
            after=after,
            metadata={
                "inputMethod": "extraction",
                "runId": run.id,
                "trigger": run.trigger,
                "mode": run.mode,
                **(details or {}),
            },
        ),
    )


def _emit_training_records(
    db: Session,
    *,
    run: ExtractionRun,
    source: Source,
    result: SourceRunResult,
    session_id: str,
) -> None:
    """One run-level event plus a full AnnotationCollection snapshot (§3.1)."""
    training.record_event(
        session_id,
        source.project_name,
        training.build_event(
            action="extraction_run",
            session_id=session_id,
            source_key=source.key,
            metadata={
                "runId": run.id,
                "trigger": run.trigger,
                "mode": run.mode,
                "extracted": result.extracted_count,
                "added": result.added_count,
                "withdrawn": result.withdrawn_count,
                "reinstated": result.reinstated_count,
                "modified": result.modified_count,
                "durationMs": result.duration_ms,
            },
        ),
    )
    standards = list(
        db.scalars(
            select(Standard).where(Standard.source_id == source.id).order_by(Standard.standard_id)
        )
    )
    training.record_snapshot(
        session_id,
        source.project_name,
        training.build_snapshot(source, standards, session_id=session_id),
    )


def normalize(standard_id: str) -> str:
    """Re-exported for callers that need the comparison key."""
    return normalize_standard_id(standard_id)
