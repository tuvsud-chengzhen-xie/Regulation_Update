"""Monitoring run orchestration: extract -> reconcile -> record -> alert.

Only one run executes at a time (a scheduled run and a manual one would
otherwise race on the same rows and share one headless browser). A run can be
executed inline or in a background thread; the latter lets the portal start a
run that outlives Azure App Service's 230-second request timeout and poll for
its progress, which is committed source by source.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractors.base import ExtractedStandard, dedupe, normalize_standard_id
from app.extractors.fetching import CAUSE_LABELS, FetchError, check_content
from app.extractors.references import KIND_CORRIGENDUM, ReferenceInfo, describe_reference
from app.extractors.registry import ExtractorError, build_context, extract_source, get_extractor
from app.models import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_REINSTATED,
    CHANGE_SUPERSEDED,
    CHANGE_UPDATED,
    CHANGE_WITHDRAWN,
    FETCH_HTTP,
    FETCH_MANUAL,
    ISSUE_SOURCE,
    POLICY_SOURCE_DATE,
    STATUS_CURRENT,
    STATUS_WITHDRAWN,
    ChangeRecord,
    ExtractionIssue,
    ExtractionRun,
    FetchAttempt,
    Source,
    SourceRunResult,
    Standard,
)
from app.services import training
from app.services.dates import resolve_publish_date
from app.services.diff import diff_source

logger = logging.getLogger(__name__)

_run_lock = threading.Lock()


class RunInProgress(RuntimeError):
    """Another monitoring run is still executing."""


class SourceRejected(ExtractorError):
    """The source was fetched, but its result failed validation."""

    def __init__(self, message: str, *, cause: str, reason: str = "source_unavailable") -> None:
        super().__init__(message)
        self.cause = cause
        self.reason = reason


def run_in_progress() -> bool:
    return _run_lock.locked()


def run_extraction(
    db: Session,
    *,
    trigger: str = "manual",
    source_ids: list[int] | None = None,
    send_alerts: bool = True,
    session_id: str | None = None,
    allow_mass_withdrawal: bool = False,
) -> ExtractionRun:
    """Execute one monitoring run inline: every source is fetched live."""
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A monitoring run is already in progress.")
    try:
        run = create_run(db, trigger=trigger)
        return execute_run(
            db,
            run,
            source_ids=source_ids,
            send_alerts=send_alerts,
            session_id=session_id,
            allow_mass_withdrawal=allow_mass_withdrawal,
        )
    finally:
        _run_lock.release()


def start_background_run(
    *,
    trigger: str = "manual",
    source_ids: list[int] | None = None,
    send_alerts: bool = True,
    allow_mass_withdrawal: bool = False,
) -> int:
    """Create a run and execute it in a worker thread. Returns the run id."""
    from app.db import SessionLocal

    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A monitoring run is already in progress.")
    try:
        db = SessionLocal()
        try:
            run_id = create_run(db, trigger=trigger).id
        finally:
            db.close()
    except Exception:
        _run_lock.release()
        raise

    def worker() -> None:
        session = SessionLocal()
        try:
            run = session.get(ExtractionRun, run_id)
            execute_run(
                session,
                run,
                source_ids=source_ids,
                send_alerts=send_alerts,
                allow_mass_withdrawal=allow_mass_withdrawal,
            )
        except Exception:  # noqa: BLE001 - execute_run records its own failure
            logger.exception("background monitoring run #%s crashed", run_id)
        finally:
            session.close()
            _run_lock.release()

    threading.Thread(target=worker, name=f"monitoring-run-{run_id}", daemon=True).start()
    return run_id


def run_upload(
    db: Session,
    *,
    source_id: int,
    payload: bytes,
    allow_mass_withdrawal: bool = False,
    send_alerts: bool = True,
) -> ExtractionRun:
    """Apply a catalogue page an administrator saved from their own browser.

    For sites that refuse automated access: the page goes through the same
    parser, validation and update rule as a fetched one.
    """
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A monitoring run is already in progress.")
    try:
        run = create_run(db, trigger="upload")
        return execute_run(
            db,
            run,
            source_ids=[source_id],
            send_alerts=send_alerts,
            allow_mass_withdrawal=allow_mass_withdrawal,
            uploads={source_id: payload},
        )
    finally:
        _run_lock.release()


def create_run(db: Session, *, trigger: str) -> ExtractionRun:
    run = ExtractionRun(
        started_at=datetime.now(timezone.utc),
        status="running",
        trigger=trigger,
        mode="live",
        is_first_run=db.scalar(select(Standard.id).limit(1)) is None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def recover_interrupted_runs(db: Session) -> int:
    """Close runs left 'running' by a restart, so the portal never shows a ghost run."""
    stale = list(db.scalars(select(ExtractionRun).where(ExtractionRun.status == "running")))
    for run in stale:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.message = (
            "Interrupted: the application restarted before this run finished. Sources "
            "completed before the restart were kept; the rest were not checked."
        )
    if stale:
        db.commit()
        logger.warning("marked %d interrupted run(s) as failed", len(stale))
    return len(stale)


def backfill_reference_metadata(db: Session) -> int:
    """Fill family/publication type on rows captured before they existed."""
    rows = list(
        db.scalars(
            select(Standard).join(Source).where(Standard.family.is_(None)).limit(20000)
        )
    )
    for standard in rows:
        _apply_reference(standard, describe_reference(standard.standard_id, standard.source.body))
    if rows:
        db.commit()
    return len(rows)


_ISO_YEAR_RE = re.compile(r":\s*\d{4}")
_TITLE_REAFFIRMED_RE = re.compile(r"^\(\s*R\s*((?:19|20)\d{2})\s*\)\s*(.*)$", re.IGNORECASE)
_COUNTERS = {
    CHANGE_ADDED: "added_count",
    CHANGE_UPDATED: "updated_count",
    CHANGE_WITHDRAWN: "withdrawn_count",
    CHANGE_SUPERSEDED: "superseded_count",
    CHANGE_REINSTATED: "reinstated_count",
    CHANGE_MODIFIED: "modified_count",
}


def repair_misread_references(db: Session) -> dict[str, int]:
    """Correct rows stored by earlier parser versions. Idempotent.

    * ISO references without an edition year were drafts at stage 60.00 that
      got stored under the extraction stamp; they are not publications, so the
      rows and their change-log entries are removed.
    * BIFMA/ALI reaffirmations were left in the title ("(R2022) Office
      Chairs"); the reaffirmation moves into the reference and publish year.
    """
    yearless = [
        s
        for s in db.scalars(select(Standard).join(Source).where(Source.body == "ISO"))
        if not _ISO_YEAR_RE.search(s.standard_id)
    ]
    for standard in yearless:
        _remove_standard(db, standard)

    reaffirmed = 0
    candidates = db.scalars(
        select(Standard)
        .join(Source)
        .where(Source.body.in_(("BIFMA", "ALI")), Standard.title_en.like("(%"))
    )
    for standard in list(candidates):
        match = _TITLE_REAFFIRMED_RE.match(standard.title_en or "")
        if not match or "(" in standard.standard_id:
            continue
        year, title = match.groups()
        new_id = f"{standard.standard_id}(R{year})"
        new_norm = normalize_standard_id(new_id)
        clash = db.scalar(
            select(Standard.id).where(
                Standard.source_id == standard.source_id, Standard.standard_id_norm == new_norm
            )
        )
        if clash:
            continue
        edition = describe_reference(standard.standard_id, standard.source.body).edition
        if standard.publish_date == edition:
            standard.publish_date = year
        for change in db.scalars(select(ChangeRecord).where(ChangeRecord.standard_pk == standard.id)):
            change.standard_id = new_id
            change.title_en = title or change.title_en
        standard.standard_id = new_id
        standard.standard_id_norm = new_norm
        standard.title_en = title or standard.title_en
        reaffirmed += 1

    if yearless or reaffirmed:
        db.commit()
    return {"removed_yearless_iso": len(yearless), "reaffirmations_read": reaffirmed}


def _remove_standard(db: Session, standard: Standard) -> None:
    """Delete a row that should never have been stored, with its change history."""
    reference = standard.standard_id
    for change in list(db.scalars(select(ChangeRecord).where(ChangeRecord.standard_pk == standard.id))):
        attr = _COUNTERS.get(change.change_type)
        if attr:
            run = db.get(ExtractionRun, change.run_id)
            if run is not None:
                setattr(run, attr, max((getattr(run, attr) or 0) - 1, 0))
            result = db.scalar(
                select(SourceRunResult).where(
                    SourceRunResult.run_id == change.run_id,
                    SourceRunResult.source_id == change.source_id,
                )
            )
            if result is not None:
                setattr(result, attr, max((getattr(result, attr) or 0) - 1, 0))
        db.delete(change)

    # Drop the reference from the relationships of the rows around it.
    for other in db.scalars(
        select(ChangeRecord).where(
            ChangeRecord.source_id == standard.source_id, ChangeRecord.standard_pk != standard.id
        )
    ):
        details = dict(other.details or {})
        touched = False
        for key in ("supersedes", "supersededBy", "relatedStandards"):
            values = details.get(key)
            if isinstance(values, list) and reference in values:
                details[key] = [v for v in values if v != reference]
                touched = True
        if touched:
            other.details = details
    for other in db.scalars(select(Standard).where(Standard.source_id == standard.source_id)):
        for attr in ("supersedes", "superseded_by"):
            listed = [v.strip() for v in (getattr(other, attr) or "").split(",") if v.strip()]
            if reference in listed:
                setattr(other, attr, ", ".join(v for v in listed if v != reference) or None)
    db.delete(standard)
    db.flush()


def execute_run(
    db: Session,
    run: ExtractionRun,
    *,
    source_ids: list[int] | None = None,
    send_alerts: bool = True,
    session_id: str | None = None,
    allow_mass_withdrawal: bool = False,
    uploads: dict[int, bytes] | None = None,
) -> ExtractionRun:
    session_id = session_id or training.SERVER_SESSION_ID
    moment = datetime.now(timezone.utc)
    run_id = run.id

    query = select(Source).order_by(Source.body, Source.tc_code)
    if not uploads:
        query = query.where(Source.is_active.is_(True))
    if source_ids:
        query = query.where(Source.id.in_(source_ids))
    sources = list(db.scalars(query))
    run.source_count = len(sources)
    db.commit()

    try:
        # One context for the whole run: a shared HTTP client, plus a single
        # headless browser started lazily the first time a browser-backed
        # source is reached.
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
                    allow_mass_withdrawal=allow_mass_withdrawal,
                    upload=(uploads or {}).get(source.id),
                )
                # Commit per source: progress is visible while the run is
                # going, and a crash later in the run keeps what was done.
                db.commit()

        statuses = list(
            db.scalars(select(SourceRunResult.status).where(SourceRunResult.run_id == run_id))
        )
        attempted = [s for s in statuses if s != "skipped"]
        run.failed_source_count = attempted.count("failed")
        if not attempted:
            run.status = "success"
            run.message = "no active sources"
        elif run.failed_source_count == len(attempted):
            run.status = "failed"
        elif run.failed_source_count:
            run.status = "partial"
        else:
            run.status = "success"
        if run.failed_source_count and not run.message:
            run.message = (
                f"{run.failed_source_count} of {len(attempted)} source(s) could not be "
                f"extracted; their standards were left untouched. See Issues."
            )
        run.finished_at = datetime.now(timezone.utc)
        db.flush()

        if send_alerts:
            from app.services.notify import dispatch_alerts

            run.alerts_sent = dispatch_alerts(db, run, session_id=session_id)

        db.commit()
    except Exception as exc:  # noqa: BLE001 - a run must always end in a final state
        logger.exception("monitoring run #%s aborted", run_id)
        db.rollback()
        run = db.get(ExtractionRun, run_id)
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.message = f"Run aborted: {exc}"[:2000]
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
    allow_mass_withdrawal: bool = False,
    upload: bytes | None = None,
) -> None:
    started = time.perf_counter()
    strategy = "upload" if upload is not None else (source.fetch_strategy or FETCH_HTTP)
    result = SourceRunResult(run_id=run.id, source_id=source.id, fetch_strategy=strategy)
    db.add(result)

    if upload is None and strategy == FETCH_MANUAL:
        result.status = "skipped"
        result.error = "Manual source: updated only by uploading its catalogue page."
        db.flush()
        return

    existing = list(db.scalars(select(Standard).where(Standard.source_id == source.id)))
    current_count = sum(1 for s in existing if s.status == STATUS_CURRENT)
    is_first_capture = not existing

    try:
        if upload is not None:
            check_content(upload, url=source.url, body=source.body, transport="upload")
            parsed = get_extractor(source).parse(upload)
            parsed.rows = dedupe(parsed.rows)
        else:
            strategy = get_extractor(source).strategy()
            result.fetch_strategy = strategy
            parsed = extract_source(source, ctx)
        extracted = parsed.rows
        if not extracted and existing:
            # A source that suddenly returns nothing is far more likely to be
            # broken than to have withdrawn its whole catalogue. Refuse to
            # apply the withdrawal rule on no evidence.
            raise SourceRejected(
                "extraction returned 0 standards while the database holds "
                f"{len(existing)}; the withdrawal rule was not applied",
                cause="empty_result",
            )
    except Exception as exc:  # noqa: BLE001 - any failure must be contained per source
        result.attempts = _record_attempts(db, run=run, source=source, ctx=ctx)
        _fail_source(db, run=run, source=source, result=result, exc=exc,
                     strategy=strategy, started=started)
        return

    result.attempts = _record_attempts(db, run=run, source=source, ctx=ctx)
    _record_issues(db, run=run, source=source, parsed=parsed)

    diff = diff_source(
        existing,
        extracted,
        compare_publish_date=source.publish_date_policy == POLICY_SOURCE_DATE,
    )

    if (
        not allow_mass_withdrawal
        and current_count >= settings.withdrawal_guard_min_current
        and len(diff.withdrawn) > settings.max_withdrawal_ratio * current_count
    ):
        _fail_source(
            db,
            run=run,
            source=source,
            result=result,
            strategy=strategy,
            started=started,
            exc=SourceRejected(
                f"{len(diff.withdrawn)} of {current_count} current standards would be "
                f"withdrawn ({len(diff.withdrawn) / current_count:.0%}, limit "
                f"{settings.max_withdrawal_ratio:.0%}). The extraction looks incomplete, so "
                f"nothing was changed for this source. If the catalogue really shrank, run "
                f"again with 'Allow mass withdrawals'.",
                cause="suspicious_withdrawal",
                reason="suspicious_withdrawal",
            ),
        )
        return

    body = source.body
    families: dict[str, list[Standard]] = {}
    for row in existing:
        family = row.family or describe_reference(row.standard_id, body).family
        families.setdefault(family, []).append(row)
    added = [(incoming, describe_reference(incoming.standard_id, body)) for incoming in diff.added]
    added_by_family: dict[str, list[str]] = {}
    for incoming, info in added:
        added_by_family.setdefault(info.family, []).append(incoming.standard_id)
    withdrawn_keys = {row.standard_id_norm for row in diff.withdrawn}
    counts = dict.fromkeys(
        (CHANGE_ADDED, CHANGE_UPDATED, CHANGE_WITHDRAWN, CHANGE_SUPERSEDED), 0
    )

    for incoming, info in added:
        related = families.get(info.family, [])
        supersedes = [r.standard_id for r in related if r.standard_id_norm in withdrawn_keys]
        change_type = CHANGE_UPDATED if related else CHANGE_ADDED
        standard = _create_standard(
            db, source=source, incoming=incoming, run=run, moment=moment, first=is_first_capture
        )
        _apply_reference(standard, info)
        standard.supersedes = ", ".join(supersedes) or None
        standard.last_change_type = change_type
        standard.last_change_run_id = run.id
        counts[change_type] += 1
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=change_type,
            old_status=None,
            new_status=STATUS_CURRENT,
            session_id=session_id,
            action="standard_create",
            before=None,
            after=training.standard_state(standard),
            details={
                "publicationType": info.kind,
                "family": info.family,
                "relation": _relation(info, related, body) if related else "new_standard",
                "amends": info.amends,
                "supersedes": supersedes,
                "relatedStandards": sorted(r.standard_id for r in related)[:25],
                # First capture of this source: a catalogue snapshot, not news.
                "baseline": is_first_capture,
            },
        )

    for standard in diff.withdrawn:
        before = training.standard_state(standard)
        if standard.family is None:
            _apply_reference(standard, describe_reference(standard.standard_id, body))
        family = standard.family
        replacements = added_by_family.get(family, [])
        change_type = CHANGE_SUPERSEDED if replacements else CHANGE_WITHDRAWN
        last_seen = standard.last_seen_run_id
        standard.status = STATUS_WITHDRAWN
        standard.withdrawn_at = moment
        standard.withdrawn_run_id = run.id
        standard.superseded_by = ", ".join(replacements) or None
        standard.last_change_type = change_type
        standard.last_change_run_id = run.id
        counts[change_type] += 1
        _log_change(
            db,
            run=run,
            source=source,
            standard=standard,
            change_type=change_type,
            old_status=STATUS_CURRENT,
            new_status=STATUS_WITHDRAWN,
            session_id=session_id,
            action="standard_withdraw",
            before=before,
            after=training.standard_state(standard),
            details={
                "publicationType": standard.publication_type,
                "family": family,
                "relation": "superseded" if replacements else "removed_from_source",
                "supersededBy": replacements,
                "lastSeenRunId": last_seen,
            },
        )

    for standard, incoming in diff.reinstated:
        before = training.standard_state(standard)
        previous_withdrawal = standard.withdrawn_run_id
        standard.status = STATUS_CURRENT
        standard.withdrawn_at = None
        standard.withdrawn_run_id = None
        standard.superseded_by = None
        standard.last_seen_run_id = run.id
        standard.last_change_type = CHANGE_REINSTATED
        standard.last_change_run_id = run.id
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
            details={
                "publicationType": standard.publication_type,
                "family": standard.family,
                "relation": "reappeared_in_source",
                "withdrawnInRunId": previous_withdrawal,
            },
        )

    for standard, incoming, field_changes in diff.modified:
        before = training.standard_state(standard)
        for change in field_changes:
            setattr(standard, change.field, change.new)
        standard.last_seen_run_id = run.id
        standard.last_change_type = CHANGE_MODIFIED
        standard.last_change_run_id = run.id
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
            details={
                "fields": [c.field for c in field_changes],
                "changes": [{"field": c.field, "old": c.old, "new": c.new} for c in field_changes],
            },
        )

    for standard, _ in diff.unchanged:
        standard.last_seen_run_id = run.id
        if standard.family is None:
            _apply_reference(standard, describe_reference(standard.standard_id, body))

    result.status = "success"
    result.extracted_count = len(extracted)
    result.added_count = counts[CHANGE_ADDED]
    result.updated_count = counts[CHANGE_UPDATED]
    result.withdrawn_count = counts[CHANGE_WITHDRAWN]
    result.superseded_count = counts[CHANGE_SUPERSEDED]
    result.reinstated_count = len(diff.reinstated)
    result.modified_count = len(diff.modified)
    result.failed_record_count = parsed.failed_count
    result.duration_ms = int((time.perf_counter() - started) * 1000)

    run.extracted_count = (run.extracted_count or 0) + result.extracted_count
    run.added_count = (run.added_count or 0) + result.added_count
    run.updated_count = (run.updated_count or 0) + result.updated_count
    run.withdrawn_count = (run.withdrawn_count or 0) + result.withdrawn_count
    run.superseded_count = (run.superseded_count or 0) + result.superseded_count
    run.reinstated_count = (run.reinstated_count or 0) + result.reinstated_count
    run.modified_count = (run.modified_count or 0) + result.modified_count
    run.failed_record_count = (run.failed_record_count or 0) + result.failed_record_count
    source.last_run_at = moment
    db.flush()

    _emit_training_records(db, run=run, source=source, result=result, session_id=session_id)


def _relation(info: ReferenceInfo, related: list[Standard], body: str) -> str:
    """Why a new row belongs to a standard the database already knows."""
    if info.amends:
        return "corrigendum" if info.kind == KIND_CORRIGENDUM else "amendment"
    editions = {describe_reference(r.standard_id, body).edition for r in related}
    if info.reaffirmed and info.edition in editions:
        return "reaffirmation"
    if info.edition and info.edition in editions:
        return "consolidated_amendment"
    return "new_edition"


def _apply_reference(standard: Standard, info: ReferenceInfo) -> None:
    standard.family = info.family[:255]
    standard.publication_type = info.kind


def _failure_cause(exc: Exception) -> tuple[str, int | None]:
    if isinstance(exc, FetchError):
        return exc.cause, exc.http_status
    if isinstance(exc, SourceRejected):
        return exc.cause, None
    if isinstance(exc, (ExtractorError, ValueError, KeyError)):
        return "parse_error", None
    return "unexpected_error", None


def _fail_source(
    db: Session,
    *,
    run: ExtractionRun,
    source: Source,
    result: SourceRunResult,
    exc: Exception,
    strategy: str,
    started: float,
) -> None:
    cause, http_status = _failure_cause(exc)
    message = str(exc) or exc.__class__.__name__
    if cause == "unexpected_error":
        logger.exception("extraction failed for %s [%s]: %s", source.key, cause, message)
    else:
        logger.warning(
            "extraction failed for %s via %s [%s%s] after %d attempt(s): %s",
            source.key, strategy, cause, f", HTTP {http_status}" if http_status else "",
            result.attempts or 0, message,
        )
    result.status = "failed"
    result.error = message
    result.error_cause = cause
    result.http_status = http_status
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    # The whole source is a gap in the database; record it alongside the
    # per-record issues so one screen shows everything that was missed.
    db.add(
        ExtractionIssue(
            run_id=run.id,
            source_id=source.id,
            scope=ISSUE_SOURCE,
            reason=getattr(exc, "reason", None) or "source_unavailable",
            detail=f"{CAUSE_LABELS.get(cause, cause)}: {message}",
            raw=source.url,
            location=f"{strategy} · {cause}"[:64],
        )
    )
    db.flush()


def _record_attempts(db: Session, *, run: ExtractionRun, source: Source, ctx) -> int:
    attempts = ctx.drain_attempts() if hasattr(ctx, "drain_attempts") else []
    for attempt in attempts:
        db.add(
            FetchAttempt(
                run_id=run.id,
                source_id=source.id,
                url=attempt.url,
                transport=attempt.transport,
                attempt=attempt.attempt,
                outcome=attempt.outcome,
                cause=attempt.cause,
                http_status=attempt.http_status,
                duration_ms=attempt.duration_ms,
                bytes=attempt.bytes,
                message=attempt.message,
            )
        )
    return len(attempts)


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
                "changeType": change_type,
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
                "updated": result.updated_count,
                "withdrawn": result.withdrawn_count,
                "superseded": result.superseded_count,
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
