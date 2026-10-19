"""Unified Annotation Training-Data Framework capture layer.

Implements the mandatory contract of §9.1 of the framework document for this
application:

* W3C body/target/selector pattern for every record.
* UUID-based identifiers for sessions, annotations, events and snapshots.
* Events carry before/after state for every mutation.
* Snapshots are full `AnnotationCollection` documents.
* Every record is stamped with `_serverTimestamp` and `_schemaVersion`.
* Storage is append-only JSONL at `AI_Training_Data/{session}/{project}.jsonl`.

Domain mapping
--------------
This application annotates *standards catalogues*, not images, so the spatial
`PointSelector` of the reference implementation is replaced by a
``RecordSelector`` — an extension declared under §9.2 "Extension Points", which
allows new selector types provided the base fields are preserved. Adding the
domain action enums is a MINOR change under §9.3, hence schema version 1.1.0.

  Annotation -> one standard captured from one source
  Body       -> the catalogue record (Standard ID, titles, dates, status)
  Target     -> the source catalogue (URL) + RecordSelector identifying the row
  Motivation -> "identifying" when a TC/SC code is known, else "tagging"
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

SCHEMA_VERSION = settings.training_schema_version
CONTEXT = "http://www.w3.org/ns/anno.jsonld"

#: Deterministic namespace so a standard keeps one annotation id for its whole
#: lifecycle (§7.1 "Track lifecycle").
ANNOTATION_NS = uuid.UUID("6f2f6c8c-6a4e-5f2a-9c33-2f0b7d2f9a10")

ACTIONS = {
    "standard_create": "A standard entered the database as Current",
    "standard_withdraw": "A standard disappeared from its source and became Withdrawn",
    "standard_reinstate": "A withdrawn standard reappeared in its source",
    "standard_edit": "Catalogue metadata (title, dates, committee) changed",
    "extraction_run": "A monitoring run completed for one source",
    "subscription_create": "A user subscribed to a standard, committee, body or keyword",
    "subscription_delete": "A user removed a subscription",
    "alert_send": "An update alert was delivered to a subscriber",
    "export_generate": "An administrator exported the database",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)

_write_lock = threading.Lock()

#: Session used for records the backend generates itself (scheduled runs, etc.).
SERVER_SESSION_ID = str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, fallback: str) -> str:
    """Sanitise a path component.

    Session id and project name arrive from HTTP requests and are used as
    directory and file names, so anything outside `[A-Za-z0-9._-]` is replaced
    and traversal segments are rejected outright.
    """
    cleaned = _SAFE_NAME_RE.sub("_", (value or "").strip())
    cleaned = cleaned.strip("._") or fallback
    return cleaned[:120]


def jsonl_path(session_id: str, project_name: str) -> Path:
    directory = settings.training_data_dir / safe_name(session_id, "unknown-session")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_name(project_name, 'project')}.jsonl"


def annotation_id(source_key: str, standard_id: str) -> str:
    return f"ann-{uuid.uuid5(ANNOTATION_NS, f'{source_key}|{standard_id}')}"


# --- record construction ----------------------------------------------------

def standard_state(standard: Any) -> dict[str, Any] | None:
    """Flat before/after state object (§6.4 equivalent for catalogue records)."""
    if standard is None:
        return None
    source_key = getattr(getattr(standard, "source", None), "key", None) or ""
    return {
        "_aid": annotation_id(source_key, standard.standard_id),
        "standardId": standard.standard_id,
        "tcCode": standard.tc_code,
        "titleEn": standard.title_en,
        "titleZh": standard.title_zh,
        "publishDate": standard.publish_date,
        "implementDate": standard.implement_date,
        "status": standard.status,
    }


def build_event(
    *,
    action: str,
    session_id: str,
    source_key: str | None = None,
    standard_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """An event record following §6.1, with domain fields added."""
    return {
        "id": f"evt-{uuid.uuid4()}",
        "timestamp": now_iso(),
        "sessionId": session_id,
        "action": action,
        "annotationId": annotation_id(source_key or "", standard_id) if standard_id else None,
        "sourceKey": source_key,
        "standardId": standard_id,
        "before": before,
        "after": after,
        "metadata": metadata or {},
    }


def build_annotation(standard: Any, *, source: Any, session_id: str) -> dict[str, Any]:
    """One W3C Annotation for one standard."""
    return {
        "id": annotation_id(source.key, standard.standard_id),
        "type": "Annotation",
        "created": _iso(standard.created_at),
        "modified": _iso(standard.updated_at),
        "motivation": "identifying" if standard.tc_code else "tagging",
        "creator": {"type": "Software", "sessionId": session_id},
        "body": {
            "type": "TextualBody",
            "value": standard.standard_id,
            "purpose": "identifying" if standard.tc_code else "tagging",
            "standardId": standard.standard_id,
            "tcCode": standard.tc_code,
            "titleEn": standard.title_en,
            "titleZh": standard.title_zh,
            "publishDate": standard.publish_date,
            "implementDate": standard.implement_date,
            "status": standard.status,
        },
        "target": {
            "source": source.key,
            "sourceUrl": standard.source_url or source.url,
            "selector": {
                # Extension point (§9.2): a record-level selector for tabular
                # catalogue sources, replacing the image PointSelector.
                "type": "RecordSelector",
                "standardId": standard.standard_id,
                "normalizedId": standard.standard_id_norm,
                "committee": standard.tc_code,
                "body": source.body,
            },
        },
        "rendering": {"status": standard.status},
    }


def build_snapshot(source: Any, standards: Iterable[Any], *, session_id: str) -> dict[str, Any]:
    """A full `AnnotationCollection` for one monitored source (§5)."""
    rows = list(standards)
    return {
        "@context": CONTEXT,
        "type": "AnnotationCollection",
        "id": f"snap-{uuid.uuid4()}",
        "generated": now_iso(),
        "generator": {
            "type": "Software",
            "name": settings.app_name,
            "version": settings.app_version,
        },
        "project": {
            "name": source.project_name,
            "body": source.body,
            "committee": source.tc_code,
            "sourceUrl": source.url,
        },
        "sources": [
            {
                "id": source.key,
                "index": 0,
                "body": source.body,
                "committee": source.tc_code,
                "url": source.url,
                "description": source.title,
            }
        ],
        "total": len(rows),
        "annotations": [build_annotation(s, source=source, session_id=session_id) for s in rows],
    }


# --- persistence ------------------------------------------------------------

def append_record(
    record_type: str, session_id: str, project_name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Stamp, envelope and append one record. Returns the stored envelope."""
    payload = dict(payload)
    payload["_serverTimestamp"] = now_iso()
    payload["_schemaVersion"] = SCHEMA_VERSION

    envelope = {
        "recordType": record_type,
        "sessionId": session_id,
        "projectName": project_name,
        record_type: payload,
    }

    if settings.training_capture_enabled:
        path = jsonl_path(session_id, project_name)
        line = json.dumps(envelope, ensure_ascii=False, default=str)
        with _write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    return envelope


def record_event(session_id: str, project_name: str, event: dict[str, Any]) -> dict[str, Any]:
    # A record with no identifier cannot be deduplicated (§10.1), so the server
    # backfills one when a client omits it.
    event = dict(event)
    event.setdefault("id", f"evt-{uuid.uuid4()}")
    event.setdefault("timestamp", now_iso())
    event.setdefault("sessionId", session_id)
    return append_record("event", session_id, project_name, event)


def record_snapshot(session_id: str, project_name: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(snapshot)
    snapshot.setdefault("id", f"snap-{uuid.uuid4()}")
    snapshot.setdefault("generated", now_iso())
    return append_record("snapshot", session_id, project_name, snapshot)


def read_records(session_id: str | None = None, project_name: str | None = None) -> list[dict]:
    """Read stored envelopes (used by the quality report and the API)."""
    root = settings.training_data_dir
    if not root.exists():
        return []
    if session_id:
        directories = [root / safe_name(session_id, "unknown-session")]
    else:
        directories = [p for p in root.iterdir() if p.is_dir()]

    records: list[dict] = []
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if project_name and path.stem != safe_name(project_name, "project"):
                continue
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return records


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return str(value)


def is_uuid_like(value: str | None) -> bool:
    if not value:
        return False
    text = value.split("-", 1)[1] if value[:4] in ("ann-", "evt-") else value
    text = text[5:] if text.startswith("snap-") else text
    return bool(_UUID_RE.match(text))
