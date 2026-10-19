"""Unified Annotation Training-Data Framework conformance."""

from __future__ import annotations

import json

from app.config import settings
from app.services import training

ADMIN = {"X-API-Key": "test-admin-key"}


def test_schema_endpoint_is_public_and_describes_both_record_types(client):
    schema = client.get("/ai_training_schema").json()
    assert schema["schemaVersion"] == settings.training_schema_version
    assert set(schema["recordTypes"]) == {"event", "snapshot"}
    assert schema["fileFormat"]["layout"] == "AI_Training_Data/{session_id}/{project}.jsonl"
    assert "standard_create" in schema["actions"]
    assert "RecordSelector" in schema["extensionPoints"]


def test_runs_write_jsonl_events_and_snapshots(client):
    client.post("/api/runs", json={}, headers=ADMIN).raise_for_status()

    records = training.read_records()
    assert records, "monitoring runs must produce training records"

    events = [r for r in records if r["recordType"] == "event"]
    snapshots = [r for r in records if r["recordType"] == "snapshot"]
    assert events and snapshots

    for record in records:
        payload = record[record["recordType"]]
        assert payload["_schemaVersion"] == settings.training_schema_version
        assert payload["_serverTimestamp"]
        assert record["sessionId"] and record["projectName"]

    snapshot = snapshots[0]["snapshot"]
    assert snapshot["type"] == "AnnotationCollection"
    assert snapshot["@context"] == "http://www.w3.org/ns/anno.jsonld"
    assert snapshot["id"].startswith("snap-")

    annotation = snapshot["annotations"][0]
    assert annotation["type"] == "Annotation"
    assert annotation["id"].startswith("ann-")
    assert annotation["body"]["type"] == "TextualBody"
    assert annotation["motivation"] in {"identifying", "tagging"}
    assert annotation["target"]["selector"]["type"] == "RecordSelector"


def test_events_carry_before_and_after_state(client):
    events = [r["event"] for r in training.read_records() if r["recordType"] == "event"]
    creates = [e for e in events if e["action"] == "standard_create"]
    assert creates
    assert creates[0]["before"] is None
    assert creates[0]["after"]["status"] == "Current"
    assert creates[0]["annotationId"].startswith("ann-")


def test_annotation_ids_are_stable_across_snapshots():
    first = training.annotation_id("CEN:CEN/TC 207", "EN 16122:2012")
    second = training.annotation_id("CEN:CEN/TC 207", "EN 16122:2012")
    assert first == second
    assert first != training.annotation_id("CEN:CEN/TC 52", "EN 16122:2012")


def test_client_posted_records_are_stamped_and_stored(client):
    response = client.post(
        "/ai_training_event",
        json={
            "sessionId": "11111111-2222-4333-8444-555555555555",
            "projectName": "Manual_Curation",
            "event": {"action": "standard_edit", "before": None, "after": {"status": "Current"}},
        },
        headers=ADMIN,
    )
    assert response.status_code == 200
    ack = response.json()
    assert ack["stored"] is True
    assert ack["schemaVersion"] == settings.training_schema_version

    path = settings.training_data_dir / ack["sessionId"] / "Manual_Curation.jsonl"
    stored = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["recordType"] == "event"
    assert stored["event"]["_serverTimestamp"]


def test_event_without_an_action_is_rejected(client):
    response = client.post(
        "/ai_training_event",
        json={"sessionId": "s", "projectName": "p", "event": {}},
        headers=ADMIN,
    )
    assert response.status_code == 422


def test_path_components_cannot_escape_the_training_directory(client):
    response = client.post(
        "/ai_training_snapshot",
        json={
            "sessionId": "../../etc",
            "projectName": "../../passwd",
            "snapshot": {"annotations": []},
        },
        headers=ADMIN,
    )
    assert response.status_code == 200
    written = settings.training_data_dir / "etc" / "passwd.jsonl"
    assert written.exists()
    assert written.resolve().is_relative_to(settings.training_data_dir.resolve())


def test_quality_report_passes_on_generated_data(client):
    report = client.get("/ai_training_quality", headers=ADMIN).json()
    assert report["recordsInspected"] > 0
    assert report["checks"]["schema_version"]["failures"] == 0
    assert report["checks"]["uuid_validity"]["failures"] == 0
    assert report["checks"]["orphan_check"]["failures"] == 0


def test_training_files_are_listed_per_session_and_project(client):
    listing = client.get("/ai_training_sessions", headers=ADMIN).json()
    assert listing["sessions"]
    projects = {f["project"] for s in listing["sessions"] for f in s["files"]}
    assert any(p.startswith("CEN_") for p in projects)
