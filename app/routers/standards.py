"""Standards database: query, inspect and curate."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select

from app.models import ChangeRecord, ExtractionRun, Source, Standard
from app.schemas import StandardOut, StandardPage, StandardUpdate
from app.security import AdminUser, CurrentUser, DbSession
from app.services import training

router = APIRouter(prefix="/api/standards", tags=["standards"])


@router.get("", response_model=StandardPage, summary="Search the standards database")
def list_standards(
    db: DbSession,
    user: CurrentUser,
    q: str | None = Query(default=None, description="Free text over ID and titles"),
    status: str | None = None,
    body: str | None = None,
    tc_code: str | None = None,
    source_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> StandardPage:
    query = select(Standard).join(Source, Source.id == Standard.source_id)
    if q:
        needle = f"%{q}%"
        query = query.where(
            or_(
                Standard.standard_id.ilike(needle),
                Standard.title_en.ilike(needle),
                Standard.title_zh.ilike(needle),
            )
        )
    if status:
        query = query.where(Standard.status == status)
    if body:
        query = query.where(Source.body == body)
    if tc_code:
        query = query.where(Standard.tc_code == tc_code)
    if source_id:
        query = query.where(Standard.source_id == source_id)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(Source.body, Standard.tc_code, Standard.standard_id)
        .limit(limit)
        .offset(offset)
    ).all()
    return StandardPage(total=total, limit=limit, offset=offset, items=rows)


@router.get("/statistics", summary="Counts by status, body and committee")
def statistics(db: DbSession, user: CurrentUser) -> dict:
    by_status = dict(
        db.execute(select(Standard.status, func.count()).group_by(Standard.status)).all()
    )
    by_body = dict(
        db.execute(
            select(Source.body, func.count())
            .join(Standard, Standard.source_id == Source.id)
            .group_by(Source.body)
        ).all()
    )
    by_committee = [
        {"tc_code": tc, "count": count}
        for tc, count in db.execute(
            select(Standard.tc_code, func.count())
            .group_by(Standard.tc_code)
            .order_by(func.count().desc())
            .limit(25)
        ).all()
    ]
    last_run = db.scalar(
        select(ExtractionRun).order_by(ExtractionRun.id.desc()).limit(1)
    )
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_body": by_body,
        "top_committees": by_committee,
        "sources": {
            "total": db.scalar(select(func.count()).select_from(Source)) or 0,
            "active": db.scalar(
                select(func.count()).select_from(Source).where(Source.is_active.is_(True))
            )
            or 0,
        },
        # Cumulative number of catalogue updates the monitor has caught — the
        # metric behind the topbar chip.
        "changes_total": db.scalar(select(func.count()).select_from(ChangeRecord)) or 0,
        "last_run": None
        if last_run is None
        else {
            "id": last_run.id,
            "status": last_run.status,
            "mode": last_run.mode,
            "finished_at": last_run.finished_at,
        },
    }


@router.get("/{standard_pk}", response_model=StandardOut)
def get_standard(standard_pk: int, db: DbSession, user: CurrentUser) -> Standard:
    standard = db.get(Standard, standard_pk)
    if standard is None:
        raise HTTPException(status_code=404, detail="Standard not found")
    return standard


@router.patch(
    "/{standard_pk}",
    response_model=StandardOut,
    summary="Curate a record (administrators only)",
)
def update_standard(
    standard_pk: int, payload: StandardUpdate, db: DbSession, admin: AdminUser
) -> Standard:
    standard = db.get(Standard, standard_pk)
    if standard is None:
        raise HTTPException(status_code=404, detail="Standard not found")

    before = training.standard_state(standard)
    changed = payload.model_dump(exclude_unset=True)
    for field, value in changed.items():
        setattr(standard, field, value)
    db.flush()

    training.record_event(
        training.SERVER_SESSION_ID,
        standard.source.project_name,
        training.build_event(
            action="standard_edit",
            session_id=training.SERVER_SESSION_ID,
            source_key=standard.source.key,
            standard_id=standard.standard_id,
            before=before,
            after=training.standard_state(standard),
            metadata={"inputMethod": "ui_button", "editor": admin.email,
                      "fields": sorted(changed)},
        ),
    )
    db.commit()
    db.refresh(standard)
    return standard
