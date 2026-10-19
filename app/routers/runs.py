"""Monitoring runs and the resulting change log."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models import ChangeRecord, ExtractionIssue, ExtractionRun, Source
from app.schemas import ChangeOut, IssueOut, RunOut, RunRequest
from app.security import AdminUser, CurrentUser, DbSession
from app.services.ingest import run_extraction
from app.services.scheduler import next_run_time

router = APIRouter(prefix="/api/runs", tags=["monitoring runs"])


@router.post("", response_model=RunOut, summary="Trigger a monitoring run (administrators only)")
def trigger_run(payload: RunRequest, db: DbSession, admin: AdminUser) -> ExtractionRun:
    return run_extraction(
        db,
        trigger="manual",
        source_ids=payload.source_ids,
        send_alerts=payload.send_alerts,
    )


@router.get("", response_model=list[RunOut])
def list_runs(db: DbSession, user: CurrentUser, limit: int = Query(default=20, ge=1, le=200)):
    query = (
        select(ExtractionRun)
        .options(selectinload(ExtractionRun.results))
        .order_by(ExtractionRun.id.desc())
        .limit(limit)
    )
    return list(db.scalars(query))


@router.get("/schedule", summary="Configured monitoring frequency")
def schedule(user: CurrentUser) -> dict:
    from app.config import settings

    return {
        "enabled": settings.scheduler_enabled,
        "cron": {
            "day": settings.schedule_cron_day,
            "hour": settings.schedule_cron_hour,
            "minute": settings.schedule_cron_minute,
            "timezone": "UTC",
        },
        "next_run_time": next_run_time(),
    }


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
