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
    summary="Download the database as Excel (administrators only)",
)
def export_standards(
    db: DbSession,
    admin: AdminUser,
    q: str | None = Query(default=None, description="Same free-text filter as the list"),
    status: str | None = Query(default=None, description="Current or Withdrawn"),
    body: str | None = None,
    tc_code: str | None = None,
    publication_type: str | None = None,
    change_type: str | None = Query(default=None, description="Last change applied"),
    include_change_log: bool = True,
):
    """The workbook is re-read after writing and compared with the database;
    the outcome is in the `Export Check` sheet and the `X-Export-*` headers."""
    path, report = export_workbook(
        db,
        q=q,
        status=status,
        body=body,
        tc_code=tc_code,
        publication_type=publication_type,
        change_type=change_type,
        include_change_log=include_change_log,
    )
    _record(admin, report)
    return FileResponse(
        path,
        media_type=XLSX_MEDIA_TYPE,
        filename=path.name,
        headers={
            "X-Export-Verified": "pass" if report["ok"] else "fail",
            "X-Export-Rows": str(report["exported_rows"]),
            "X-Export-Expected-Rows": str(report["database_rows"]),
        },
    )


@router.get("/verify", summary="Build an export and check it against the standards list")
def verify_standards_export(
    db: DbSession,
    admin: AdminUser,
    q: str | None = None,
    status: str | None = None,
    body: str | None = None,
    tc_code: str | None = None,
    publication_type: str | None = None,
    change_type: str | None = None,
) -> dict:
    _, report = export_workbook(
        db, q=q, status=status, body=body, tc_code=tc_code,
        publication_type=publication_type, change_type=change_type, include_change_log=False,
    )
    return report


def _record(admin, report: dict) -> None:
    training.record_event(
        training.SERVER_SESSION_ID,
        "Exports",
        training.build_event(
            action="export_generate",
            session_id=training.SERVER_SESSION_ID,
            metadata={
                "inputMethod": "ui_button",
                "requestedBy": admin.email,
                "filters": report["filters"],
                "file": report["file"],
                "rows": report["exported_rows"],
                "verified": report["ok"],
            },
        ),
    )
