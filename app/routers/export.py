"""Excel export of the standards database (administrators only)."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app.security import AdminUser, DbSession
from app.services import training
from app.services.exporter import export_workbook

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get(
    "/standards.xlsx",
    response_class=FileResponse,
    summary="Download the full database as Excel (administrators only)",
)
def export_standards(
    db: DbSession,
    admin: AdminUser,
    status: str | None = Query(default=None, description="Current or Withdrawn"),
    body: str | None = None,
    tc_code: str | None = None,
    include_change_log: bool = True,
):
    path = export_workbook(
        db, status=status, body=body, tc_code=tc_code, include_change_log=include_change_log
    )
    training.record_event(
        training.SERVER_SESSION_ID,
        "Exports",
        training.build_event(
            action="export_generate",
            session_id=training.SERVER_SESSION_ID,
            metadata={
                "inputMethod": "ui_button",
                "requestedBy": admin.email,
                "filters": {"status": status, "body": body, "tcCode": tc_code},
                "file": path.name,
            },
        ),
    )
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename=path.name)
