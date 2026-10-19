"""Data-quality checks for collected training data (framework §10.1).

Adapted to catalogue records: the "coordinate bounds" check becomes a
required-field check on the Standard ID, and the "orphan" check verifies that
every annotation points at a declared source.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.services.training import SCHEMA_VERSION, is_uuid_like


def quality_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {
        "schema_version": {"rule": "record has _schemaVersion", "action": "reject", "failures": 0},
        "uuid_validity": {"rule": "ids are UUID-shaped", "action": "flag", "failures": 0},
        "required_fields": {"rule": "annotation carries a Standard ID", "action": "discard", "failures": 0},
        "temporal_order": {"rule": "events ordered per session", "action": "re-sort", "failures": 0},
        "orphan_check": {"rule": "annotations reference a declared source", "action": "discard", "failures": 0},
        "duplicate_events": {"rule": "no duplicate event id per session", "action": "dedupe", "failures": 0},
    }

    seen_event_ids: dict[str, set[str]] = defaultdict(set)
    last_timestamp: dict[str, str] = {}
    counts = Counter()
    versions = Counter()

    for record in records:
        record_type = record.get("recordType")
        payload = record.get(record_type) if record_type else None
        counts[record_type or "unknown"] += 1
        if not isinstance(payload, dict):
            checks["schema_version"]["failures"] += 1
            continue

        version = payload.get("_schemaVersion")
        versions[version or "missing"] += 1
        if not version:
            checks["schema_version"]["failures"] += 1

        session = record.get("sessionId") or "unknown"

        if record_type == "event":
            event_id = payload.get("id")
            if not is_uuid_like(event_id):
                checks["uuid_validity"]["failures"] += 1
            if event_id in seen_event_ids[session]:
                checks["duplicate_events"]["failures"] += 1
            else:
                seen_event_ids[session].add(event_id)

            stamp = payload.get("_serverTimestamp") or payload.get("timestamp") or ""
            if session in last_timestamp and stamp < last_timestamp[session]:
                checks["temporal_order"]["failures"] += 1
            last_timestamp[session] = max(stamp, last_timestamp.get(session, ""))

        elif record_type == "snapshot":
            if not is_uuid_like(payload.get("id")):
                checks["uuid_validity"]["failures"] += 1
            declared = {s.get("id") for s in payload.get("sources") or []}
            for annotation in payload.get("annotations") or []:
                body = annotation.get("body") or {}
                if not body.get("standardId"):
                    checks["required_fields"]["failures"] += 1
                target_source = (annotation.get("target") or {}).get("source")
                if declared and target_source not in declared:
                    checks["orphan_check"]["failures"] += 1

    total_failures = sum(c["failures"] for c in checks.values())
    return {
        "schemaVersion": SCHEMA_VERSION,
        "recordsInspected": sum(counts.values()),
        "recordCounts": dict(counts),
        "schemaVersionsSeen": dict(versions),
        "checks": checks,
        "passed": total_failures == 0,
        "totalFailures": total_failures,
    }
