"""Link CEN publications to the ones they supersede, across committees.

The update rule only sees one committee's list at a time and can pair an old
and a new edition only when they share a family (EN 581-1:2017 -> 2026). CEN
also merges and renumbers standards (EN 12790:2026 supersedes EN 12790-1:2023,
EN 12790-2:2023 and EN 16232:2013+A2:2023), which only the project page says.
After a run, each changed CEN publication is looked up there and:

* the new publication records what it supersedes (one-to-many);
* an old publication withdrawn in the run becomes "superseded", linked back;
* an old publication CEN still lists (in its transition period) only gets the
  link — its status is left to the update rule.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractors import cen_relations
from app.extractors.base import normalize_standard_id
from app.models import (
    CHANGE_ADDED,
    CHANGE_SUPERSEDED,
    CHANGE_UPDATED,
    CHANGE_WITHDRAWN,
    ISSUE_FIELD,
    ChangeRecord,
    ExtractionIssue,
    ExtractionRun,
    Source,
    SourceRunResult,
    Standard,
)

logger = logging.getLogger(__name__)

_COUNTER = {
    CHANGE_ADDED: "added_count",
    CHANGE_UPDATED: "updated_count",
    CHANGE_WITHDRAWN: "withdrawn_count",
    CHANGE_SUPERSEDED: "superseded_count",
}


def link_cen_relations(db: Session, run: ExtractionRun, ctx) -> int:
    """Look up this run's CEN changes on their project pages. Returns links made."""
    rows = list(
        db.execute(
            select(ChangeRecord, Standard)
            .join(Source, Source.id == ChangeRecord.source_id)
            .join(Standard, Standard.id == ChangeRecord.standard_pk)
            .where(
                ChangeRecord.run_id == run.id,
                Source.body == "CEN",
                ChangeRecord.change_type.in_(tuple(_COUNTER)),
            )
            .order_by(ChangeRecord.id)
        )
    )
    # A committee's first capture is its whole catalogue, not news.
    rows = [(c, s) for c, s in rows if not (c.details or {}).get("baseline")]
    if not rows:
        return 0

    new = [(c, s) for c, s in rows if c.change_type in (CHANGE_ADDED, CHANGE_UPDATED)]
    old = [(c, s) for c, s in rows if c.change_type in (CHANGE_WITHDRAWN, CHANGE_SUPERSEDED)]
    budget = settings.cen_relations_max_lookups
    links = 0

    for change, standard in new:
        if budget <= 0:
            break
        budget -= 1
        found = _lookup(db, run, standard, ctx)
        for reference in found.supersedes if found else []:
            links += _link(db, run, new=standard, new_change=change, old_reference=reference)

    for change, standard in old:
        if change.change_type == CHANGE_SUPERSEDED and standard.superseded_by:
            continue  # already linked by the update rule or a lookup above
        if budget <= 0:
            break
        budget -= 1
        found = _lookup(db, run, standard, ctx)
        for reference in found.superseded_by if found else []:
            links += _link_from_old(db, run, old=standard, old_change=change, new_reference=reference)

    db.flush()
    if links:
        logger.info("run #%s: linked %d CEN supersession relation(s)", run.id, links)
    return links


def _lookup(db: Session, run: ExtractionRun, standard: Standard, ctx):
    try:
        found = cen_relations.lookup(ctx, standard.standard_id)
    except Exception as exc:  # noqa: BLE001 - a missing relation must not fail the run
        logger.warning("CEN relations lookup for %s failed: %s", standard.standard_id, exc)
        _issue(db, run, standard, f"The CEN project page could not be read: {exc}"[:2000])
        return None
    if found is None:
        _issue(db, run, standard, "CEN's standards search did not return this reference.")
    return found


def _issue(db: Session, run: ExtractionRun, standard: Standard, detail: str) -> None:
    db.add(
        ExtractionIssue(
            run_id=run.id,
            source_id=standard.source_id,
            scope=ISSUE_FIELD,
            reason="relations_unavailable",
            detail=detail,
            standard_id=standard.standard_id,
            location="CEN project page",
        )
    )


def _link(db, run, *, new: Standard, new_change: ChangeRecord, old_reference: str) -> int:
    _add(new, "supersedes", old_reference)
    details = dict(new_change.details or {})
    details["supersedes"] = _merged(details.get("supersedes"), old_reference)
    details["relationSource"] = "CEN project page"
    if new_change.change_type == CHANGE_ADDED:
        # It replaces an earlier publication, so it is an update, not a new standard.
        _move(db, run, new_change, CHANGE_UPDATED)
        new.last_change_type = CHANGE_UPDATED
        details["relation"] = "supersedes"
    new_change.details = details

    links = 0
    for old in _standards(db, old_reference):
        _add(old, "superseded_by", new.standard_id)
        links += 1
        _mark_superseded(db, run, old, new.standard_id)
    return links or 1


def _link_from_old(db, run, *, old: Standard, old_change: ChangeRecord, new_reference: str) -> int:
    _add(old, "superseded_by", new_reference)
    _mark_superseded(db, run, old, new_reference, change=old_change)
    for new in _standards(db, new_reference):
        _add(new, "supersedes", old.standard_id)
    return 1


def _mark_superseded(db, run, old: Standard, new_reference: str, change=None) -> None:
    """An old publication withdrawn in this run is superseded, not just withdrawn."""
    change = change or db.scalar(
        select(ChangeRecord).where(
            ChangeRecord.run_id == run.id, ChangeRecord.standard_pk == old.id,
            ChangeRecord.change_type.in_((CHANGE_WITHDRAWN, CHANGE_SUPERSEDED)),
        )
    )
    if change is None:
        return
    details = dict(change.details or {})
    details["supersededBy"] = _merged(details.get("supersededBy"), new_reference)
    details["relation"] = "superseded"
    details["relationSource"] = "CEN project page"
    change.details = details
    if change.change_type == CHANGE_WITHDRAWN:
        _move(db, run, change, CHANGE_SUPERSEDED)
        old.last_change_type = CHANGE_SUPERSEDED


def _move(db, run, change: ChangeRecord, to_type: str) -> None:
    from_attr, to_attr = _COUNTER[change.change_type], _COUNTER[to_type]
    result = db.scalar(
        select(SourceRunResult).where(
            SourceRunResult.run_id == change.run_id, SourceRunResult.source_id == change.source_id
        )
    )
    target = run if run.id == change.run_id else db.get(ExtractionRun, change.run_id)
    for row in (target, result):
        if row is not None:
            setattr(row, from_attr, max((getattr(row, from_attr) or 0) - 1, 0))
            setattr(row, to_attr, (getattr(row, to_attr) or 0) + 1)
    change.change_type = to_type


def _standards(db: Session, reference: str) -> list[Standard]:
    return list(
        db.scalars(
            select(Standard)
            .join(Source)
            .where(Source.body == "CEN", Standard.standard_id_norm == normalize_standard_id(reference))
        )
    )


def _add(standard: Standard, attr: str, reference: str) -> None:
    listed = [v.strip() for v in (getattr(standard, attr) or "").split(",") if v.strip()]
    setattr(standard, attr, ", ".join(_merged(listed, reference)))


def _merged(values, reference: str) -> list[str]:
    values = list(values or [])
    keys = {normalize_standard_id(v) for v in values}
    if normalize_standard_id(reference) not in keys:
        values.append(reference)
    return values
