"""Monitored sources (CEN/TCs, ISO TCs and SCs, ASTM subcommittees, ...)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.extractors.registry import SUPPORTED_BODIES
from app.models import STATUS_CURRENT, Source, Standard
from app.schemas import SourceIn, SourceOut, SourceUpdate
from app.security import AdminUser, CurrentUser, DbSession

router = APIRouter(prefix="/api/sources", tags=["sources"])


def _to_out(db, source: Source) -> SourceOut:
    total = db.scalar(
        select(func.count()).select_from(Standard).where(Standard.source_id == source.id)
    )
    current = db.scalar(
        select(func.count())
        .select_from(Standard)
        .where(Standard.source_id == source.id, Standard.status == STATUS_CURRENT)
    )
    out = SourceOut.model_validate(source)
    out.standard_count = total or 0
    out.current_count = current or 0
    return out


@router.get("", response_model=list[SourceOut])
def list_sources(db: DbSession, user: CurrentUser, body: str | None = None,
                 active_only: bool = False) -> list[SourceOut]:
    query = select(Source).order_by(Source.body, Source.tc_code)
    if body:
        query = query.where(Source.body == body)
    if active_only:
        query = query.where(Source.is_active.is_(True))
    return [_to_out(db, source) for source in db.scalars(query)]


@router.get("/bodies", response_model=list[str], summary="Standards bodies with an extractor")
def list_bodies(user: CurrentUser) -> list[str]:
    return SUPPORTED_BODIES


@router.post("", response_model=SourceOut, status_code=201)
def create_source(payload: SourceIn, db: DbSession, admin: AdminUser) -> SourceOut:
    if payload.body.upper() not in SUPPORTED_BODIES:
        raise HTTPException(
            status_code=422,
            detail=f"No extractor for body '{payload.body}'. Supported: {SUPPORTED_BODIES}",
        )
    existing = db.scalar(
        select(Source).where(Source.body == payload.body.upper(), Source.tc_code == payload.tc_code)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Source already exists")

    source = Source(**{**payload.model_dump(), "body": payload.body.upper()})
    db.add(source)
    db.commit()
    db.refresh(source)
    return _to_out(db, source)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceUpdate, db: DbSession,
                  admin: AdminUser) -> SourceOut:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    db.commit()
    db.refresh(source)
    return _to_out(db, source)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, db: DbSession, admin: AdminUser) -> None:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    db.delete(source)
    db.commit()
