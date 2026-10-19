"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import (
    FETCH_BROWSER,
    FETCH_HTTP,
    ISSUE_FIELD,
    ISSUE_RECORD,
    ISSUE_SOURCE,
    POLICY_SOURCE_DATE,
    POLICY_YEAR_THEN_STAMP,
    ROLE_ADMIN,
    ROLE_USER,
    STATUS_CURRENT,
    STATUS_WITHDRAWN,
)

StatusLiteral = Literal[STATUS_CURRENT, STATUS_WITHDRAWN]
RoleLiteral = Literal[ROLE_ADMIN, ROLE_USER]
PolicyLiteral = Literal[POLICY_SOURCE_DATE, POLICY_YEAR_THEN_STAMP]
SubscriptionType = Literal["standard", "committee", "body", "keyword"]
FetchStrategy = Literal[FETCH_HTTP, FETCH_BROWSER]
IssueScope = Literal[ISSUE_RECORD, ISSUE_FIELD, ISSUE_SOURCE]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- standards --------------------------------------------------------------

class StandardOut(ORMModel):
    id: int
    standard_id: str = Field(description="标准号+年份 / Standard ID + Version")
    tc_code: str | None = Field(default=None, description="归口单位代码 / TC / SC Code")
    title_zh: str | None = Field(default=None, description="标准中文名称")
    title_en: str | None = Field(default=None, description="标准英文名称")
    publish_date: str | None = Field(default=None, description="发布日期")
    implement_date: str | None = Field(default=None, description="实施日期")
    status: str = Field(description="标准状态 / Status")
    source_id: int
    source_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StandardPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[StandardOut]


class StandardUpdate(BaseModel):
    """Manual curation (administrators only), e.g. filling the Chinese title."""

    title_zh: str | None = None
    title_en: str | None = None
    implement_date: str | None = None
    publish_date: str | None = None
    status: StatusLiteral | None = None


# --- sources ----------------------------------------------------------------

class SourceIn(BaseModel):
    body: str
    tc_code: str
    title: str | None = None
    url: str
    publish_date_policy: PolicyLiteral = POLICY_YEAR_THEN_STAMP
    id_prefix: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    fetch_strategy: FetchStrategy | None = Field(
        default=None,
        description="Transport override. Omit to use the body's default "
        "(browser for ISO/BIFMA/ALI/WSTDA, http for the rest).",
    )
    is_active: bool = True


class SourceUpdate(BaseModel):
    title: str | None = None
    url: str | None = None
    publish_date_policy: PolicyLiteral | None = None
    options: dict[str, Any] | None = None
    fetch_strategy: FetchStrategy | None = None
    is_active: bool | None = None


class SourceOut(ORMModel):
    id: int
    body: str
    tc_code: str
    title: str | None
    url: str
    publish_date_policy: str
    options: dict[str, Any]
    fetch_strategy: str
    is_active: bool
    last_run_at: datetime | None
    standard_count: int = 0
    current_count: int = 0


# --- runs -------------------------------------------------------------------

class RunRequest(BaseModel):
    source_ids: list[int] | None = Field(
        default=None, description="Restrict the run to these sources; omit for all active sources."
    )
    send_alerts: bool = True


class SourceRunResultOut(ORMModel):
    source_id: int
    status: str
    extracted_count: int
    added_count: int
    withdrawn_count: int
    reinstated_count: int
    modified_count: int
    failed_record_count: int
    fetch_strategy: str
    duration_ms: int
    error: str | None


class RunOut(ORMModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    trigger: str
    mode: str
    is_first_run: bool
    extracted_count: int
    added_count: int
    withdrawn_count: int
    reinstated_count: int
    modified_count: int
    failed_record_count: int
    alerts_sent: int
    message: str | None
    results: list[SourceRunResultOut] = []


class IssueOut(ORMModel):
    """One catalogue entry (or source) that could not be captured."""

    id: int
    run_id: int
    source_id: int
    scope: str = Field(description="record = lost entirely | field = captured with a gap | source")
    reason: str
    detail: str
    raw: str | None = Field(default=None, description="Verbatim source text that failed")
    standard_id: str | None
    location: str | None
    created_at: datetime
    body: str | None = None
    tc_code: str | None = None


class ChangeOut(ORMModel):
    id: int
    run_id: int
    source_id: int
    standard_id: str
    tc_code: str | None
    title_en: str | None
    change_type: str
    old_status: str | None
    new_status: str | None
    details: dict[str, Any]
    created_at: datetime


# --- users & subscriptions --------------------------------------------------

class UserIn(BaseModel):
    email: EmailStr
    full_name: str | None = None
    role: RoleLiteral = ROLE_USER
    api_key: str | None = Field(default=None, description="Generated when omitted.")


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: RoleLiteral | None = None
    is_active: bool | None = None


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime


class UserWithKey(UserOut):
    api_key: str


class SubscriptionIn(BaseModel):
    type: SubscriptionType
    value: str = Field(min_length=1, max_length=255)
    user_id: int | None = Field(
        default=None, description="Administrators may subscribe on behalf of another user."
    )


class SubscriptionOut(ORMModel):
    id: int
    user_id: int
    type: str
    value: str
    is_active: bool
    created_at: datetime


class AlertOut(ORMModel):
    id: int
    run_id: int | None
    email: str
    subject: str
    change_count: int
    backend: str
    status: str
    error: str | None
    sent_at: datetime


# --- training data ----------------------------------------------------------

class TrainingEnvelopeIn(BaseModel):
    sessionId: str = Field(min_length=1, max_length=200)
    projectName: str = Field(min_length=1, max_length=200)


class TrainingEventIn(TrainingEnvelopeIn):
    event: dict[str, Any]


class TrainingSnapshotIn(TrainingEnvelopeIn):
    snapshot: dict[str, Any]


class TrainingAck(BaseModel):
    stored: bool
    recordType: str
    sessionId: str
    projectName: str
    schemaVersion: str
    serverTimestamp: str
    path: str
