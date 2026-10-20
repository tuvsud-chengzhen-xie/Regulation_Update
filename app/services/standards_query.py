"""The one filter used by both the Standards list and the Excel export.

Sharing it is what guarantees that an export taken from the Standards page
holds exactly the rows the page shows, in the same order.
"""

from __future__ import annotations

from sqlalchemy import Select, or_, select

from app.models import Source, Standard


def standards_query(
    *,
    q: str | None = None,
    status: str | None = None,
    body: str | None = None,
    tc_code: str | None = None,
    source_id: int | None = None,
    publication_type: str | None = None,
    change_type: str | None = None,
) -> Select:
    query = select(Standard, Source).join(Source, Source.id == Standard.source_id)
    if q:
        needle = f"%{q.strip()}%"
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
        query = query.where(Source.body == body.upper())
    if tc_code:
        query = query.where(Standard.tc_code == tc_code)
    if source_id:
        query = query.where(Standard.source_id == source_id)
    if publication_type:
        query = query.where(Standard.publication_type == publication_type)
    if change_type:
        query = query.where(Standard.last_change_type == change_type)
    return query.order_by(Source.body, Standard.tc_code, Standard.standard_id, Standard.id)
