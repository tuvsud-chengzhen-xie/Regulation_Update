"""Monitoring runs and the resulting change log."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.extractors.fetching import CAUSE_LABELS
from app.models import (
    CHANGE_TYPES,
    ChangeRecord,
    ExtractionIssue,
    ExtractionRun,
    FetchAttempt,
    Source,
    SourceRunResult,
)
from app.schemas import (
    ChangeOut,
    FetchAttemptOut,
    IssueOut,
    RunOut,
    RunRequest,
    SourceFailureOut,
)
from app.security import AdminUser, CurrentUser, DbSession
from app.services import scheduler
from app.services.ingest import RunInProgress, run_extraction, run_in_progress, start_background_run

router = APIRouter(prefix="/api/runs", tags=["monitoring runs"])


@router.post(
    "",
    response_model=RunOut,
    summary="Trigger a monitoring run (administrators only)",
    responses={202: {"description": "Started in the background"}, 409: {"description": "Busy"}},
)
def trigger_run(payload: RunRequest, db: DbSession, admin: AdminUser):
    try:
        if payload.background:
            run_id = start_background_run(
                trigger="manual",
                source_ids=payload.source_ids,
                send_alerts=payload.send_alerts,
                allow_mass_withdrawal=payload.allow_mass_withdrawal,
            )
            run = db.get(ExtractionRun, run_id)
            return JSONResponse(
                status_code=202, content=RunOut.model_validate(run).model_dump(mode="json")
            )
        return run_extraction(
            db,
            trigger="manual",
            source_ids=payload.source_ids,
            send_alerts=payload.send_alerts,
            allow_mass_withdrawal=payload.allow_mass_withdrawal,
        )
    except RunInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[RunOut])
def list_runs(db: DbSession, user: CurrentUser, limit: int = Query(default=20, ge=1, le=200)):
    query = (
        select(ExtractionRun)
        .options(selectinload(ExtractionRun.results))
        .order_by(ExtractionRun.id.desc())
        .limit(limit)
    )
    return list(db.scalars(query))


@router.get("/active", summary="The run currently executing, if any")
def active_run(db: DbSession, user: CurrentUser) -> dict:
    run = db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.status == "running")
        .order_by(ExtractionRun.id.desc())
        .limit(1)
    )
    done = 0
    if run is not None:
        done = db.scalar(
            select(func.count()).select_from(SourceRunResult)
            .where(SourceRunResult.run_id == run.id)
        ) or 0
    return {
        "busy": run_in_progress(),
        "run": None if run is None else RunOut.model_validate(run).model_dump(mode="json"),
        "sources_done": done,
    }


@router.get("/schedule", summary="Configured monitoring frequency")
def get_schedule(db: DbSession, user: CurrentUser) -> dict:
    return _schedule_out(scheduler.load_config(db))


@router.put("/schedule", summary="Change the monitoring frequency (administrators only)")
def put_schedule(config: scheduler.ScheduleConfig, db: DbSession, admin: AdminUser) -> dict:
    return _schedule_out(scheduler.save_config(db, config))


def _schedule_out(config: scheduler.ScheduleConfig) -> dict:
    return {
        **config.model_dump(mode="json"),
        "description": scheduler.describe(config),
        "next_run_time": scheduler.next_run_time() if config.enabled else None,
        "timezone": "UTC",
    }


@router.get("/coverage", summary="New / updated / withdrawn examples collected in a period")
def coverage(
    db: DbSession,
    user: CurrentUser,
    since: date | None = Query(default=None, description="Default: schedule window start, or 90 days ago"),
    until: date | None = Query(default=None, description="Default: today"),
) -> dict:
    """Whether monitoring has gathered enough examples of each kind of change.

    Targets come from `COVERAGE_TARGET_*`; `withdrawn` counts both plain
    withdrawals and supersessions, `updated` counts new editions, amendments
    and corrigenda of standards already in the database.
    """
    config = scheduler.load_config(db)
    today = datetime.now(timezone.utc).date()
    since = since or config.start_date or (today - timedelta(days=90))
    until = until or (min(config.end_date, today) if config.end_date else today)
    until = max(until, since)
    start = datetime.combine(since, time.min)
    end = datetime.combine(until, time.max)

    rows = db.execute(
        select(Source.body, ChangeRecord.change_type, ChangeRecord.details)
        .join(Source, Source.id == ChangeRecord.source_id)
        .join(ExtractionRun, ExtractionRun.id == ChangeRecord.run_id)
        .where(ExtractionRun.started_at >= start, ExtractionRun.started_at <= end)
    ).all()
    by_type = dict.fromkeys(CHANGE_TYPES, 0)
    by_body: dict[str, dict[str, int]] = {}
    baseline = 0
    for body, change_type, details in rows:
        if (details or {}).get("baseline"):
            baseline += 1
            continue
        by_type[change_type] = by_type.get(change_type, 0) + 1
        counts = by_body.setdefault(body, dict.fromkeys(CHANGE_TYPES, 0))
        counts[change_type] = counts.get(change_type, 0) + 1

    runs = db.execute(
        select(ExtractionRun.status, func.count())
        .where(ExtractionRun.started_at >= start, ExtractionRun.started_at <= end)
        .group_by(ExtractionRun.status)
    ).all()
    observed = {
        "added": by_type["added"],
        "updated": by_type["updated"],
        "withdrawn": by_type["withdrawn"] + by_type["superseded"],
    }
    targets = {
        "added": settings.coverage_target_added,
        "updated": settings.coverage_target_updated,
        "withdrawn": settings.coverage_target_withdrawn,
    }
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "runs": dict(runs),
        "run_count": sum(count for _, count in runs),
        "by_type": by_type,
        "by_body": by_body,
        "observed": observed,
        "targets": targets,
        "met": {key: observed[key] >= targets[key] for key in targets},
        "sufficient": all(observed[key] >= targets[key] for key in targets),
        "baseline_excluded": baseline,
    }


@router.get(
    "/source-failures",
    response_model=list[SourceFailureOut],
    summary="Sources that could not be fetched or parsed, with the cause and every attempt",
)
def source_failures(
    db: DbSession,
    user: CurrentUser,
    run_id: int | None = None,
    latest: bool = Query(default=False, description="Restrict to the most recent finished run"),
    source_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[SourceFailureOut]:
    if latest and run_id is None:
        run_id = db.scalar(
            select(func.max(SourceRunResult.run_id))
        )
    query = (
        select(SourceRunResult, Source, ExtractionRun.started_at)
        .join(Source, Source.id == SourceRunResult.source_id)
        .join(ExtractionRun, ExtractionRun.id == SourceRunResult.run_id)
        .where(SourceRunResult.status == "failed")
        .order_by(SourceRunResult.run_id.desc(), Source.body, Source.tc_code)
    )
    if run_id:
        query = query.where(SourceRunResult.run_id == run_id)
    if source_id:
        query = query.where(SourceRunResult.source_id == source_id)

    out: list[SourceFailureOut] = []
    for result, source, started_at in db.execute(query.limit(limit)).all():
        attempts = list(
            db.scalars(
                select(FetchAttempt)
                .where(FetchAttempt.run_id == result.run_id, FetchAttempt.source_id == source.id)
                .order_by(FetchAttempt.id)
            )
        )
        out.append(
            SourceFailureOut(
                run_id=result.run_id,
                run_started_at=started_at,
                source_id=source.id,
                body=source.body,
                tc_code=source.tc_code,
                url=source.url,
                transport=result.fetch_strategy,
                cause=result.error_cause,
                cause_label=CAUSE_LABELS.get(result.error_cause or ""),
                http_status=result.http_status,
                attempts=result.attempts or len(attempts),
                duration_ms=result.duration_ms,
                error=result.error,
                last_success_at=_last_success(db, source.id),
                attempt_log=[FetchAttemptOut.model_validate(a) for a in attempts],
            )
        )
    return out


def _last_success(db, source_id: int):
    return db.scalar(
        select(func.max(ExtractionRun.started_at))
        .join(SourceRunResult, SourceRunResult.run_id == ExtractionRun.id)
        .where(SourceRunResult.source_id == source_id, SourceRunResult.status == "success")
    )


@router.get("/changes", response_model=list[ChangeOut], summary="Change log")
def list_changes(
    db: DbSession,
    user: CurrentUser,
    run_id: int | None = None,
    change_type: str | None = None,
    standard_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query = select(ChangeRecord).order_by(ChangeRecord.id.desc())
    if run_id:
        query = query.where(ChangeRecord.run_id == run_id)
    if change_type:
        query = query.where(ChangeRecord.change_type == change_type)
    if standard_id:
        query = query.where(ChangeRecord.standard_id.ilike(f"%{standard_id}%"))
    return list(db.scalars(query.limit(limit).offset(offset)))


def _query_issues(
    db,
    *,
    run_id: int | None = None,
    scope: str | None = None,
    reason: str | None = None,
    source_id: int | None = None,
    body: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[IssueOut]:
    query = (
        select(ExtractionIssue, Source.body, Source.tc_code)
        .join(Source, Source.id == ExtractionIssue.source_id)
        .order_by(ExtractionIssue.id.desc())
    )
    if run_id:
        query = query.where(ExtractionIssue.run_id == run_id)
    if scope:
        query = query.where(ExtractionIssue.scope == scope)
    if reason:
        query = query.where(ExtractionIssue.reason == reason)
    if source_id:
        query = query.where(ExtractionIssue.source_id == source_id)
    if body:
        query = query.where(Source.body == body)

    return [
        IssueOut.model_validate(issue).model_copy(update={"body": src_body, "tc_code": tc_code})
        for issue, src_body, tc_code in db.execute(query.limit(limit).offset(offset)).all()
    ]


@router.get(
    "/issues",
    response_model=list[IssueOut],
    summary="Catalogue entries that could not be captured",
)
def list_issues(
    db: DbSession,
    user: CurrentUser,
    run_id: int | None = None,
    scope: str | None = Query(default=None, description="record | field | source"),
    reason: str | None = None,
    source_id: int | None = None,
    body: str | None = None,
    latest: bool = Query(
        default=False, description="Restrict to the most recent run."
    ),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
):
    """Every entry the extractors saw but could not turn into a record.

    A `record`-scope issue means the standard is **not** in the database; a
    `field`-scope issue means it is, but something on it could not be read; a
    `source`-scope issue means a whole committee was missed this run.
    """
    if latest and run_id is None:
        run_id = db.scalar(select(func.max(ExtractionRun.id)))
    return _query_issues(
        db,
        run_id=run_id,
        scope=scope,
        reason=reason,
        source_id=source_id,
        body=body,
        limit=limit,
        offset=offset,
    )


@router.get("/issues/summary", summary="Extraction-issue counts by scope, reason and body")
def issues_summary(
    db: DbSession,
    user: CurrentUser,
    run_id: int | None = None,
    latest: bool = Query(default=False, description="Restrict to the most recent run."),
) -> dict:
    if latest and run_id is None:
        run_id = db.scalar(select(func.max(ExtractionRun.id)))

    def grouped(column, join_source: bool = False):
        # select_from is explicit in both branches: when the grouping column
        # belongs to Source, SQLAlchemy would otherwise infer Source as the
        # left side and fail to join it to itself.
        query = (
            select(column, func.count())
            .select_from(ExtractionIssue)
            .group_by(column)
        )
        if join_source:
            query = query.join(Source, Source.id == ExtractionIssue.source_id)
        if run_id:
            query = query.where(ExtractionIssue.run_id == run_id)
        return {str(key): count for key, count in db.execute(query).all()}

    by_scope = grouped(ExtractionIssue.scope)
    return {
        "run_id": run_id,
        "total": sum(by_scope.values()),
        "by_scope": by_scope,
        "lost_records": by_scope.get("record", 0),
        "incomplete_records": by_scope.get("field", 0),
        "failed_sources": by_scope.get("source", 0),
        "by_reason": grouped(ExtractionIssue.reason),
        "by_body": grouped(Source.body, join_source=True),
    }


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: DbSession, user: CurrentUser) -> ExtractionRun:
    run = db.get(ExtractionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{run_id}/summary", summary="Per-change-type counts for one run")
def run_summary(run_id: int, db: DbSession, user: CurrentUser) -> dict:
    run = db.get(ExtractionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    counts = dict(
        db.execute(
            select(ChangeRecord.change_type, func.count())
            .where(ChangeRecord.run_id == run_id)
            .group_by(ChangeRecord.change_type)
        ).all()
    )
    return {"run_id": run_id, "status": run.status, "mode": run.mode, "changes": counts}


@router.get(
    "/{run_id}/issues",
    response_model=list[IssueOut],
    summary="Entries this run could not capture",
)
def run_issues(
    run_id: int,
    db: DbSession,
    user: CurrentUser,
    scope: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
):
    if db.get(ExtractionRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _query_issues(db, run_id=run_id, scope=scope, limit=limit)
