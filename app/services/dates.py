"""Publish-date rules.

The specification defines two behaviours:

* CEN carries a real publication date in its export and it is stored verbatim.
* ISO / ASTM / ASME / BIFMA / ALI / WSTDA expose only a year. On the first run
  that year is stored; standards first captured during a later monitoring run
  are stamped with the extraction year and month ("2026/08"), which is also the
  rule for any source that exposes no date at all
  ("发布日期改为首次登记到内部数据库年+月").

The stamp is written once, when the standard enters the database; it is never
rewritten on subsequent runs.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.models import POLICY_SOURCE_DATE

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def extraction_stamp(moment: datetime) -> str:
    """Year + extraction month, e.g. "2026/08"."""
    return f"{moment.year}/{moment.month:02d}"


def year_of(value: str | None) -> str | None:
    if not value:
        return None
    match = _YEAR_RE.search(value)
    return match.group(0) if match else None


def resolve_publish_date(
    extracted_value: str | None,
    *,
    policy: str,
    is_first_capture: bool,
    moment: datetime,
) -> str:
    """Publish date to store for a standard entering the database."""
    if policy == POLICY_SOURCE_DATE:
        # CEN: keep the source date; fall back to the stamp when absent.
        return extracted_value or extraction_stamp(moment)

    if is_first_capture:
        return year_of(extracted_value) or extracted_value or extraction_stamp(moment)

    # Captured during a later monitoring run: record when we first saw it.
    return extraction_stamp(moment)
