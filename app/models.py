"""SQLAlchemy models.

The `Standard` table is a direct mapping of the seven columns in
`Database Format.xlsx`; column comments carry the original bilingual headers so
the export can be regenerated from the model alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- Vocabularies (kept as plain strings so they round-trip to Excel) --------

STATUS_CURRENT = "Current"
STATUS_WITHDRAWN = "Withdrawn"

ROLE_ADMIN = "admin"
ROLE_USER = "user"

# Change types recorded per run. "Standard ID + Version" stays the comparison
# key; the family ("EN 16121") only labels *why* a row appeared or vanished.
#   added      - a standard the source had never published before
#   updated    - a new publication of a known standard: new edition,
#                amendment (A1) or corrigendum (AC)
#   withdrawn  - removed from the source with no replacement in the same run
#   superseded - removed from the source and replaced in the same run
#   reinstated - a withdrawn standard reappeared
#   modified   - metadata (title, dates, committee) changed, status unchanged
CHANGE_ADDED = "added"
CHANGE_UPDATED = "updated"
CHANGE_WITHDRAWN = "withdrawn"
CHANGE_SUPERSEDED = "superseded"
CHANGE_REINSTATED = "reinstated"
CHANGE_MODIFIED = "modified"
CHANGE_TYPES = (
    CHANGE_ADDED,
    CHANGE_UPDATED,
    CHANGE_WITHDRAWN,
    CHANGE_SUPERSEDED,
    CHANGE_REINSTATED,
    CHANGE_MODIFIED,
)

SUBSCRIPTION_STANDARD = "standard"
SUBSCRIPTION_COMMITTEE = "committee"
SUBSCRIPTION_BODY = "body"
SUBSCRIPTION_KEYWORD = "keyword"

# Publish-date policies, per the extraction rules in the specification.
#   source_date      - CEN: the source file carries a real date (2012-07-18).
#   year_then_stamp  - ISO/ASTM/ASME/BIFMA/ALI/WSTDA: first run stores the year
#                      published by the source; standards first captured on a
#                      later run are stamped "YYYY/MM" of the extraction month.
POLICY_SOURCE_DATE = "source_date"
POLICY_YEAR_THEN_STAMP = "year_then_stamp"

# Transports. "http" is a plain HTTPS request; "browser" renders the page in a
# real headless Chromium first, which ISO, BIFMA, ALI and WSTDA require.
FETCH_HTTP = "http"
FETCH_BROWSER = "browser"

# What an extraction issue cost us.
#   record - the entry is NOT in the database
#   field  - the entry was captured, but one of its fields could not be read
#   source - a whole source was missed this run
ISSUE_RECORD = "record"
ISSUE_FIELD = "field"
ISSUE_SOURCE = "source"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Source(Base):
    """One monitored scope: a CEN/TC, an ISO TC or SC, an ASTM subcommittee,
    an ASME designator group, or a whole publisher site (BIFMA/ALI/WSTDA)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(32), index=True)  # CEN, ISO, ASTM, ...
    tc_code: Mapped[str] = mapped_column(String(64), index=True)  # 归口单位代码
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text)
    publish_date_policy: Mapped[str] = mapped_column(String(32), default=POLICY_YEAR_THEN_STAMP)
    id_prefix: Mapped[str | None] = mapped_column(String(32))  # "ASTM", "ASME"
    options: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"designator": "PASE"}
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: "http" or "browser" — see FETCH_* above.
    fetch_strategy: Mapped[str] = mapped_column(String(16), default=FETCH_HTTP)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    standards: Mapped[list["Standard"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("body", "tc_code", name="uq_source_body_tc"),)

    @property
    def key(self) -> str:
        return f"{self.body}:{self.tc_code}"

    @property
    def project_name(self) -> str:
        """Filename stem used for AI_Training_Data/{session}/{project}.jsonl."""
        safe = self.tc_code.replace("/", "_").replace(" ", "_").replace(".", "_")
        return f"{self.body}_{safe}"


class Standard(Base):
    __tablename__ = "standards"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    # 标准号+年份 / Standard ID + Version
    standard_id: Mapped[str] = mapped_column(String(255), index=True)
    # Normalised form used as the comparison key for the update rule.
    standard_id_norm: Mapped[str] = mapped_column(String(255), index=True)
    # 归口单位代码 / TC / SC Code
    tc_code: Mapped[str | None] = mapped_column(String(64), index=True)
    # 标准中文名称 / Standard Chinese Title
    title_zh: Mapped[str | None] = mapped_column(Text)
    # 标准英文名称 / Standard English Title
    title_en: Mapped[str | None] = mapped_column(Text)
    # 发布日期 / Publish Date (free text: "2012-07-18", "2000" or "2026/08")
    publish_date: Mapped[str | None] = mapped_column(String(32))
    # 实施日期 / Implement Date
    implement_date: Mapped[str | None] = mapped_column(String(32))
    # 标准状态 / Status
    status: Mapped[str] = mapped_column(String(32), default=STATUS_CURRENT, index=True)

    source_url: Mapped[str | None] = mapped_column(Text)
    # Reference without edition/amendment/corrigendum, e.g. "EN 16121".
    family: Mapped[str | None] = mapped_column(String(255), index=True)
    # Standard, Amendment, Corrigendum, Technical Report, ...
    publication_type: Mapped[str | None] = mapped_column(String(48))
    # Traceability between runs (comma-separated Standard IDs).
    supersedes: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    first_seen_run_id: Mapped[int | None] = mapped_column(Integer)
    last_seen_run_id: Mapped[int | None] = mapped_column(Integer)
    withdrawn_run_id: Mapped[int | None] = mapped_column(Integer)
    last_change_type: Mapped[str | None] = mapped_column(String(16))
    last_change_run_id: Mapped[int | None] = mapped_column(Integer)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    source: Mapped[Source] = relationship(back_populates="standards")

    __table_args__ = (
        UniqueConstraint("source_id", "standard_id_norm", name="uq_standard_per_source"),
        Index("ix_standard_status_body", "status", "tc_code"),
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/success/partial/failed
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual/scheduled
    mode: Mapped[str] = mapped_column(String(16), default="live")
    is_first_run: Mapped[bool] = mapped_column(Boolean, default=False)
    extracted_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    withdrawn_count: Mapped[int] = mapped_column(Integer, default=0)
    superseded_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reinstated_count: Mapped[int] = mapped_column(Integer, default=0)
    modified_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Catalogue entries seen but not capturable — see ExtractionIssue.
    failed_record_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_source_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)

    results: Mapped[list["SourceRunResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    changes: Mapped[list["ChangeRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    issues: Mapped[list["ExtractionIssue"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SourceRunResult(Base):
    __tablename__ = "source_run_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="success")  # success/failed/skipped
    extracted_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    withdrawn_count: Mapped[int] = mapped_column(Integer, default=0)
    superseded_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reinstated_count: Mapped[int] = mapped_column(Integer, default=0)
    modified_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_record_count: Mapped[int] = mapped_column(Integer, default=0)
    fetch_strategy: Mapped[str] = mapped_column(String(16), default=FETCH_HTTP)
    error: Mapped[str | None] = mapped_column(Text)
    #: Machine-readable failure cause: timeout, dns, http_5xx, challenge, ...
    error_cause: Mapped[str | None] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[ExtractionRun] = relationship(back_populates="results")
    source: Mapped[Source] = relationship()


class ChangeRecord(Base):
    __tablename__ = "change_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    standard_pk: Mapped[int | None] = mapped_column(Integer)
    standard_id: Mapped[str] = mapped_column(String(255), index=True)
    tc_code: Mapped[str | None] = mapped_column(String(64), index=True)
    title_en: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(String(16), index=True)
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ExtractionRun] = relationship(back_populates="changes")
    source: Mapped[Source] = relationship()


class ExtractionIssue(Base):
    """A catalogue entry the extractor saw but could not turn into a record.

    Every one of these is a known gap in the database, so they are recorded
    rather than logged and forgotten: `raw` keeps the source text verbatim so a
    user can see what the site actually said and judge whether the parser or
    the site is at fault.
    """

    __tablename__ = "extraction_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16), default=ISSUE_RECORD, index=True)
    reason: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text)
    #: Verbatim source text for the entry that failed.
    raw: Mapped[str | None] = mapped_column(Text)
    #: Partial identifier, where the parser recognised one before failing.
    standard_id: Mapped[str | None] = mapped_column(String(255), index=True)
    #: Where in the payload it was (row 42, line 118, ...).
    location: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ExtractionRun] = relationship(back_populates="issues")
    source: Mapped[Source] = relationship()


class FetchAttempt(Base):
    """One network request made for a source during a run, successful or not."""

    __tablename__ = "fetch_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(24))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    outcome: Mapped[str] = mapped_column(String(16))  # ok / failed
    cause: Mapped[str | None] = mapped_column(String(32), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Runtime configuration editable from the portal (e.g. the run schedule)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(16))  # standard/committee/body/keyword
    value: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="subscriptions")

    __table_args__ = (
        UniqueConstraint("user_id", "type", "value", name="uq_subscription"),
    )


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    change_count: Mapped[int] = mapped_column(Integer, default=0)
    backend: Mapped[str] = mapped_column(String(16), default="console")
    status: Mapped[str] = mapped_column(String(16), default="sent")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
