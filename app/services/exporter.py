"""Excel export.

Reproduces the layout of `Database Format.xlsx`: the seven bilingual columns in
their original order, followed by operational columns so the export carries the
complete database ("The Database should be able to be exported into an excel
with all columns by administrator roles").
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ChangeRecord, Source, Standard

#: (header, model attribute, column width) — the first seven match the
#: bilingual headers of Database Format.xlsx exactly.
COLUMNS: list[tuple[str, str, int]] = [
    ("标准号+年份\nStandard ID + Version", "standard_id", 34),
    ("归口单位代码\nTC / SC Code", "tc_code", 20),
    ("标准中文名称\nStandard Chinese Title", "title_zh", 30),
    ("标准英文名称\nStandard English Title", "title_en", 70),
    ("发布日期\nPublish Date", "publish_date", 16),
    ("实施日期\nImplement Date", "implement_date", 16),
    ("标准状态\nStatus", "status", 14),
    ("标准组织\nStandards Body", "_body", 16),
    ("数据来源\nSource URL", "source_url", 46),
    ("首次登记\nFirst Captured", "_created", 20),
    ("最后更新\nLast Updated", "_updated", 20),
]

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def export_workbook(
    db: Session,
    *,
    status: str | None = None,
    body: str | None = None,
    tc_code: str | None = None,
    include_change_log: bool = True,
) -> Path:
    query = (
        select(Standard, Source)
        .join(Source, Source.id == Standard.source_id)
        .order_by(Source.body, Standard.tc_code, Standard.standard_id)
    )
    if status:
        query = query.where(Standard.status == status)
    if body:
        query = query.where(Source.body == body)
    if tc_code:
        query = query.where(Standard.tc_code == tc_code)
    rows = list(db.execute(query))

    workbook = Workbook()
    _write_standards(workbook.active, rows)
    _write_sources(workbook.create_sheet("Sources"), db)
    if include_change_log:
        _write_change_log(workbook.create_sheet("Change Log"), db)

    settings.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = settings.export_dir / f"standards_database_{stamp}.xlsx"
    workbook.save(path)
    return path


def _write_standards(sheet, rows) -> None:
    sheet.title = "Standards"
    sheet.append([header for header, _, _ in COLUMNS])
    for index, (_, _, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
        cell = sheet.cell(row=1, column=index)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[1].height = 34

    for standard, source in rows:
        sheet.append([_value(standard, source, attribute) for _, attribute, _ in COLUMNS])

    if rows:
        reference = f"A1:{get_column_letter(len(COLUMNS))}{len(rows) + 1}"
        table = Table(displayName="StandardsDatabase", ref=reference)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        sheet.add_table(table)
    sheet.freeze_panes = "A2"


def _value(standard: Standard, source: Source, attribute: str):
    if attribute == "_body":
        return source.body
    if attribute == "_created":
        return standard.created_at.strftime("%Y-%m-%d %H:%M") if standard.created_at else None
    if attribute == "_updated":
        return standard.updated_at.strftime("%Y-%m-%d %H:%M") if standard.updated_at else None
    return getattr(standard, attribute, None)


def _write_sources(sheet, db: Session) -> None:
    headers = ["Standards Body", "TC / SC Code", "Group Name", "Standard list (Source URL)",
               "Publish date policy", "Active", "Last run"]
    sheet.append(headers)
    for index, width in enumerate((16, 24, 46, 80, 22, 10, 20), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
        cell = sheet.cell(row=1, column=index)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    for source in db.scalars(select(Source).order_by(Source.body, Source.tc_code)):
        sheet.append(
            [
                source.body,
                source.tc_code,
                source.title,
                source.url,
                source.publish_date_policy,
                "Yes" if source.is_active else "No",
                source.last_run_at.strftime("%Y-%m-%d %H:%M") if source.last_run_at else "",
            ]
        )
    sheet.freeze_panes = "A2"


def _write_change_log(sheet, db: Session) -> None:
    headers = ["Run", "Date (UTC)", "Change", "Standard ID + Version", "TC / SC Code",
               "English Title", "Old status", "New status"]
    sheet.append(headers)
    for index, width in enumerate((8, 20, 14, 34, 20, 70, 14, 14), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
        cell = sheet.cell(row=1, column=index)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    query = select(ChangeRecord).order_by(ChangeRecord.id.desc()).limit(5000)
    for change in db.scalars(query):
        sheet.append(
            [
                change.run_id,
                change.created_at.strftime("%Y-%m-%d %H:%M") if change.created_at else "",
                change.change_type,
                change.standard_id,
                change.tc_code,
                change.title_en,
                change.old_status,
                change.new_status,
            ]
        )
    sheet.freeze_panes = "A2"
