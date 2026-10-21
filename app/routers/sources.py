"""Monitored sources (CEN/TCs, ISO TCs and SCs, ASTM subcommittees, ...)."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select

from app.extractors.registry import ALLOWED_HOSTS, DEFAULT_FETCH_STRATEGY, SUPPORTED_BODIES
from app.models import (
    FETCH_HTTP,
    POLICY_SOURCE_DATE,
    POLICY_YEAR_THEN_STAMP,
    STATUS_CURRENT,
    ExtractionRun,
    Source,
    SourceRunResult,
    Standard,
)
from app.schemas import RunOut, SourceImportResult, SourceIn, SourceOut, SourceUpdate
from app.security import AdminUser, CurrentUser, DbSession

router = APIRouter(prefix="/api/sources", tags=["sources"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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

    recent = list(
        db.execute(
            select(SourceRunResult, ExtractionRun.started_at)
            .join(ExtractionRun, ExtractionRun.id == SourceRunResult.run_id)
            .where(SourceRunResult.source_id == source.id)
            .order_by(SourceRunResult.id.desc())
            .limit(20)
        ).all()
    )
    if recent:
        last, _ = recent[0]
        out.last_status = last.status
        out.last_error = last.error
        out.last_error_cause = last.error_cause
        for result, _ in recent:
            if result.status != "failed":
                break
            out.consecutive_failures += 1
        out.last_success_at = next(
            (started for result, started in recent if result.status == "success"), None
        )
    return out


def _validate_url(body: str, url: str) -> None:
    """Only fetch the standards bodies' own sites (no SSRF via a source URL)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Source URL must be an https:// URL.")
    allowed = ALLOWED_HOSTS.get(body.upper(), set())
    if parsed.hostname.lower() not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{body.upper()} sources must point at {', '.join(sorted(allowed))}; "
            f"got {parsed.hostname}.",
        )


def _create(db, payload: SourceIn) -> Source:
    body = payload.body.upper()
    if body not in SUPPORTED_BODIES:
        raise HTTPException(
            status_code=422,
            detail=f"No extractor for body '{payload.body}'. Supported: {SUPPORTED_BODIES}",
        )
    _validate_url(body, payload.url)
    existing = db.scalar(
        select(Source).where(Source.body == body, Source.tc_code == payload.tc_code.strip())
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"{body} {payload.tc_code} already exists")

    values = payload.model_dump()
    values["body"] = body
    values["tc_code"] = payload.tc_code.strip()
    if "publish_date_policy" not in payload.model_fields_set:
        values["publish_date_policy"] = (
            POLICY_SOURCE_DATE if body == "CEN" else POLICY_YEAR_THEN_STAMP
        )
    values["fetch_strategy"] = payload.fetch_strategy or DEFAULT_FETCH_STRATEGY.get(body, FETCH_HTTP)
    values["id_prefix"] = payload.id_prefix or {"ASTM": "ASTM", "ASME": "ASME"}.get(body)
    source = Source(**values)
    db.add(source)
    db.flush()
    return source


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
    source = _create(db, payload)
    db.commit()
    db.refresh(source)
    return _to_out(db, source)


@router.post(
    "/import",
    response_model=SourceImportResult,
    summary="Add several committees at once (administrators only)",
)
def import_sources(payload: list[SourceIn], db: DbSession, admin: AdminUser) -> SourceImportResult:
    """Existing body + TC/SC pairs are skipped; invalid rows are reported."""
    created: list[Source] = []
    skipped: list[str] = []
    errors: list[str] = []
    for item in payload:
        label = f"{item.body.upper()} {item.tc_code}"
        try:
            created.append(_create(db, item))
        except HTTPException as exc:
            (skipped if exc.status_code == 409 else errors).append(f"{label}: {exc.detail}")
    db.commit()
    return SourceImportResult(
        created=[_to_out(db, source) for source in created], skipped=skipped, errors=errors
    )


@router.post(
    "/{source_id}/upload",
    response_model=RunOut,
    summary="Apply a catalogue page saved from a browser (administrators only)",
)
async def upload_source_page(
    source_id: int,
    db: DbSession,
    admin: AdminUser,
    file: UploadFile = File(description="The catalogue page saved as HTML (or the CEN export)"),
    allow_mass_withdrawal: bool = Form(default=False),
):
    """For sites that refuse automated access: open the source URL in a normal
    browser, wait for the list, save it (Ctrl+S, "Webpage, Complete" - which keeps
    the rendered list) and upload the .html file. It goes through the same
    parser, validation and update rule as a fetch."""
    from app.services.ingest import RunInProgress, run_upload

    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The uploaded file is larger than 20 MB.")
    try:
        return run_upload(
            db, source_id=source.id, payload=payload, allow_mass_withdrawal=allow_mass_withdrawal
        )
    except RunInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceUpdate, db: DbSession,
                  admin: AdminUser) -> SourceOut:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("url"):
        _validate_url(source.body, changes["url"])
    for field, value in changes.items():
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
