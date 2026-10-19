"""The update rule.

From the specification:

  * Compare the new extraction with the existing database on the full
    "Standard ID + Version".
  * A standard in the database that no longer appears in the extraction —
    whether it disappeared or was replaced by a new version — becomes
    "Withdrawn".
  * A standard in the extraction that is not in the database — whether it is a
    new version of an existing standard or a brand new one — is added as
    "Current".

Worked example from the specification: the database holds EN 581-1:1998; the
new run returns EN 581-1:2026 only. EN 581-1:1998 becomes Withdrawn and
EN 581-1:2026 is added as Current — two rows, no in-place edit.

This module is pure: it decides, `ingest` applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.extractors.base import ExtractedStandard
from app.models import STATUS_CURRENT, Standard

#: Fields compared to detect a metadata-only change (never a status change).
COMPARABLE_FIELDS = ("title_en", "title_zh", "implement_date", "tc_code")


@dataclass(slots=True)
class FieldChange:
    field: str
    old: str | None
    new: str | None


@dataclass(slots=True)
class DiffResult:
    added: list[ExtractedStandard] = field(default_factory=list)
    withdrawn: list[Standard] = field(default_factory=list)
    reinstated: list[tuple[Standard, ExtractedStandard]] = field(default_factory=list)
    modified: list[tuple[Standard, ExtractedStandard, list[FieldChange]]] = field(
        default_factory=list
    )
    unchanged: list[tuple[Standard, ExtractedStandard]] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.withdrawn) + len(self.reinstated) + len(self.modified)


def diff_source(
    existing: Sequence[Standard],
    extracted: Iterable[ExtractedStandard],
    *,
    compare_publish_date: bool = False,
) -> DiffResult:
    """Reconcile one source's extraction against its database rows."""
    by_key = {row.standard_id_norm: row for row in existing}
    extracted_by_key = {row.key: row for row in extracted}
    result = DiffResult()

    for key, incoming in extracted_by_key.items():
        current = by_key.get(key)
        if current is None:
            result.added.append(incoming)
            continue
        if current.status != STATUS_CURRENT:
            # The standard is back in the published list: make it Current again
            # rather than creating a duplicate row.
            result.reinstated.append((current, incoming))
            continue
        changes = _field_changes(current, incoming, compare_publish_date=compare_publish_date)
        if changes:
            result.modified.append((current, incoming, changes))
        else:
            result.unchanged.append((current, incoming))

    for key, current in by_key.items():
        if key not in extracted_by_key and current.status == STATUS_CURRENT:
            result.withdrawn.append(current)

    return result


def _field_changes(
    current: Standard, incoming: ExtractedStandard, *, compare_publish_date: bool
) -> list[FieldChange]:
    fields = list(COMPARABLE_FIELDS)
    if compare_publish_date:
        fields.append("publish_date")

    changes: list[FieldChange] = []
    for name in fields:
        new_value = getattr(incoming, name, None)
        old_value = getattr(current, name, None)
        # A source that stops publishing an optional field must not blank it.
        if new_value is None or new_value == old_value:
            continue
        changes.append(FieldChange(field=name, old=old_value, new=new_value))
    return changes
