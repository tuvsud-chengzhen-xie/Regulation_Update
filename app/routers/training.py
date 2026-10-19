"""Unified Annotation Training-Data Framework endpoints.

Paths follow the framework document verbatim (`/ai_training_event`,
`/ai_training_snapshot`, `/ai_training_schema`) so that pipelines written
against any application in the family work unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.config import settings
from app.models import Source, Standard
from app.schemas import TrainingAck, TrainingEventIn, TrainingSnapshotIn
from app.security import AdminUser, CurrentUser, DbSession
from app.services import training
from app.services.quality import quality_report

router = APIRouter(tags=["AI training data"])


def _ack(envelope: dict, record_type: str) -> TrainingAck:
    payload = envelope[record_type]
    return TrainingAck(
        stored=settings.training_capture_enabled,
        recordType=record_type,
        sessionId=envelope["sessionId"],
        projectName=envelope["projectName"],
        schemaVersion=payload["_schemaVersion"],
        serverTimestamp=payload["_serverTimestamp"],
        path=str(
            training.jsonl_path(envelope["sessionId"], envelope["projectName"]).relative_to(
                settings.training_data_dir.parent
            )
        ),
    )


@router.post("/ai_training_event", response_model=TrainingAck, summary="Append an event record")
def post_event(payload: TrainingEventIn, user: CurrentUser) -> TrainingAck:
    event = dict(payload.event)
    if not event.get("action"):
        raise HTTPException(status_code=422, detail="event.action is required")
    event.setdefault("sessionId", payload.sessionId)
    envelope = training.record_event(payload.sessionId, payload.projectName, event)
    return _ack(envelope, "event")


@router.post(
    "/ai_training_snapshot", response_model=TrainingAck, summary="Append a snapshot record"
)
def post_snapshot(payload: TrainingSnapshotIn, user: CurrentUser) -> TrainingAck:
    snapshot = dict(payload.snapshot)
    snapshot.setdefault("@context", training.CONTEXT)
    snapshot.setdefault("type", "AnnotationCollection")
    envelope = training.record_snapshot(payload.sessionId, payload.projectName, snapshot)
    return _ack(envelope, "snapshot")


@router.get("/ai_training_schema", summary="Machine-readable schema of the collected data")
def training_schema() -> dict:
    return {
        "schemaVersion": training.SCHEMA_VERSION,
        "basedOn": {
            "model": "W3C Web Annotation Data Model",
            "url": "https://www.w3.org/TR/annotation-model/",
            "spatialCompatibility": "COCO-compatible selectors (extension points below)",
        },
        "application": {"name": settings.app_name, "version": settings.app_version,
                        "domain": "standards catalogue monitoring"},
        "fileFormat": {
            "encoding": "utf-8",
            "layout": "AI_Training_Data/{session_id}/{project}.jsonl",
            "recordsPerLine": 1,
            "ordering": "chronological, append-only",
            "envelope": {
                "recordType": '"event" | "snapshot"',
                "sessionId": "string",
                "projectName": "string",
                "event | snapshot": "object",
            },
        },
        "recordTypes": {
            "snapshot": {
                "@context": training.CONTEXT,
                "type": "AnnotationCollection",
                "id": "snap-{uuid4}",
                "generated": "ISO-8601 (client)",
                "_serverTimestamp": "ISO-8601 (server, ground truth)",
                "_schemaVersion": training.SCHEMA_VERSION,
                "generator": {"type": "Software", "name": "string", "version": "string"},
                "project": {"name": "string", "body": "string", "committee": "string"},
                "sources": "array of monitored-source objects",
                "annotations": "array of W3C Annotation objects",
            },
            "event": {
                "id": "evt-{uuid4}",
                "timestamp": "ISO-8601 (client)",
                "_serverTimestamp": "ISO-8601 (server, ground truth)",
                "_schemaVersion": training.SCHEMA_VERSION,
                "sessionId": "UUID",
                "action": sorted(training.ACTIONS),
                "annotationId": "ann-{uuid5} of the affected standard",
                "before": "flat state before the mutation (null on create)",
                "after": "flat state after the mutation (null on delete)",
                "metadata": {"inputMethod": '"extraction" | "ui_button" | "api"',
                             "runId": "int", "trigger": "string", "mode": "string"},
            },
        },
        "actions": training.ACTIONS,
        "annotationMapping": {
            "Annotation": "one standard captured from one source",
            "Body": "the catalogue record (Standard ID, titles, dates, status)",
            "Target": "the source catalogue URL plus a RecordSelector",
            "Selector": "RecordSelector { standardId, normalizedId, committee, body }",
            "Motivation": '"identifying" when a TC/SC code is known, else "tagging"',
        },
        "extensionPoints": {
            "RecordSelector": "tabular/catalogue rows (this application)",
            "PointSelector": "arrow annotations (origin, tip, vector)",
            "FragmentSelector": "bounding boxes (xywh)",
            "SvgSelector": "polygons",
            "TextPositionSelector": "text spans",
        },
        "mlUsage": {
            "standard_id_normalisation": "seq2seq over body.standardId vs selector.normalizedId",
            "committee_classification": "predict body.tcCode from titles (class label)",
            "change_prediction": "event stream per source -> next withdrawal/publication",
            "title_translation": "body.titleEn -> body.titleZh pairs once curated",
            "active_learning": "standards with repeated standard_edit events are hard examples",
        },
        "governance": {
            "retentionYears": 2,
            "appendOnly": True,
            "pii": "none; session ids are random UUIDs",
        },
    }


@router.get("/ai_training_quality", summary="Run the §10.1 data-quality checks")
def training_quality(
    admin: AdminUser,
    session_id: str | None = Query(default=None),
    project: str | None = Query(default=None),
) -> dict:
    return quality_report(training.read_records(session_id, project))


@router.get("/ai_training_sessions", summary="Sessions and project files on disk")
def training_sessions(admin: AdminUser) -> dict:
    root = settings.training_data_dir
    sessions = []
    if root.exists():
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            files = []
            for path in sorted(directory.glob("*.jsonl")):
                with path.open("r", encoding="utf-8") as handle:
                    lines = sum(1 for line in handle if line.strip())
                files.append({"project": path.stem, "records": lines,
                              "bytes": path.stat().st_size})
            sessions.append({"sessionId": directory.name, "files": files})
    return {"root": str(root), "sessions": sessions}


@router.post(
    "/ai_training_snapshot/rebuild",
    summary="Re-emit a snapshot for every source from current database state",
)
def rebuild_snapshots(db: DbSession, admin: AdminUser) -> dict:
    session_id = training.SERVER_SESSION_ID
    written = []
    for source in db.scalars(select(Source).order_by(Source.body, Source.tc_code)):
        standards = list(
            db.scalars(
                select(Standard)
                .where(Standard.source_id == source.id)
                .order_by(Standard.standard_id)
            )
        )
        training.record_snapshot(
            session_id,
            source.project_name,
            training.build_snapshot(source, standards, session_id=session_id),
        )
        written.append({"project": source.project_name, "annotations": len(standards)})
    return {"sessionId": session_id, "snapshots": written}
