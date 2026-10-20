"""Periodic monitoring.

The specification leaves the monitoring frequency as "TBD". The schedule is
therefore editable from the portal (Runs > Schedule) and stored in the
database; the `SCHEDULE_*` environment variables only supply the defaults
until an administrator saves one.

A schedule can be bounded by a monitoring window (start/end date), so a trial
period - e.g. "weekly for the next three months" - collects enough examples of
new, updated and withdrawn standards and then stops by itself.

Run the scheduler on a single App Service instance only; with several
instances each would fire its own run.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.db import SessionLocal

logger = logging.getLogger(__name__)

JOB_ID = "standards-monitoring-run"
SETTING_KEY = "schedule"
_scheduler: BackgroundScheduler | None = None

Frequency = Literal["daily", "weekly", "biweekly", "monthly", "cron"]
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleConfig(BaseModel):
    enabled: bool = False
    frequency: Frequency = "monthly"
    hour: int = Field(default=2, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    day_of_week: str = Field(default="mon", description="weekly/biweekly: mon..sun")
    day_of_month: int = Field(default=1, ge=1, le=28)
    cron: str | None = Field(default=None, description="5-field crontab, frequency=cron only")
    start_date: date | None = Field(default=None, description="Monitoring window start (UTC)")
    end_date: date | None = Field(default=None, description="Monitoring window end (UTC)")

    @field_validator("day_of_week")
    @classmethod
    def _valid_day(cls, value: str) -> str:
        value = value.strip().lower()[:3]
        if value not in _DAYS:
            raise ValueError(f"day_of_week must be one of {', '.join(_DAYS)}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ScheduleConfig":
        if self.frequency == "cron":
            if not self.cron:
                raise ValueError("a crontab expression is required when frequency is 'cron'")
            try:
                CronTrigger.from_crontab(self.cron, timezone="UTC")
            except ValueError as exc:
                raise ValueError(f"invalid crontab expression: {exc}") from exc
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


def default_config() -> ScheduleConfig:
    def as_int(value: str, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    return ScheduleConfig(
        enabled=settings.scheduler_enabled,
        frequency="monthly",
        hour=as_int(settings.schedule_cron_hour, 2),
        minute=as_int(settings.schedule_cron_minute, 0),
        day_of_month=min(28, max(1, as_int(settings.schedule_cron_day, 1))),
    )


def load_config(db) -> ScheduleConfig:
    from app.models import AppSetting

    row = db.get(AppSetting, SETTING_KEY)
    if row is None or not row.value:
        return default_config()
    try:
        return ScheduleConfig.model_validate(row.value)
    except ValueError:
        logger.warning("stored schedule is invalid; using the defaults")
        return default_config()


def save_config(db, config: ScheduleConfig) -> ScheduleConfig:
    from app.models import AppSetting

    row = db.get(AppSetting, SETTING_KEY)
    payload = config.model_dump(mode="json")
    if row is None:
        db.add(AppSetting(key=SETTING_KEY, value=payload))
    else:
        row.value = payload
    db.commit()
    apply_config(config)
    return config


def build_trigger(config: ScheduleConfig) -> CronTrigger:
    start = (
        datetime.combine(config.start_date, time.min, tzinfo=timezone.utc)
        if config.start_date else None
    )
    end = (
        datetime.combine(config.end_date, time.max, tzinfo=timezone.utc)
        if config.end_date else None
    )
    window = {"start_date": start, "end_date": end, "timezone": "UTC"}
    if config.frequency == "cron":
        return CronTrigger.from_crontab(config.cron, **window)
    fields: dict = {"hour": config.hour, "minute": config.minute}
    if config.frequency == "weekly":
        fields["day_of_week"] = config.day_of_week
    elif config.frequency == "biweekly":
        # Every other week: the chosen weekday in odd ISO weeks.
        fields.update(day_of_week=config.day_of_week, week="*/2")
    elif config.frequency == "monthly":
        fields["day"] = config.day_of_month
    return CronTrigger(**fields, **window)


def describe(config: ScheduleConfig) -> str:
    at = f"{config.hour:02d}:{config.minute:02d} UTC"
    text = {
        "daily": f"Every day at {at}",
        "weekly": f"Every {config.day_of_week.title()} at {at}",
        "biweekly": f"Every other {config.day_of_week.title()} at {at}",
        "monthly": f"Day {config.day_of_month} of each month at {at}",
        "cron": f"Crontab '{config.cron}' (UTC)",
    }[config.frequency]
    if config.start_date or config.end_date:
        text += f", from {config.start_date or 'now'} until {config.end_date or 'further notice'}"
    return text


def scheduled_run() -> None:
    from app.services.ingest import RunInProgress, run_extraction

    db = SessionLocal()
    try:
        run = run_extraction(db, trigger="scheduled")
        logger.info(
            "scheduled run #%s finished: %s (+%s new, %s updated, -%s withdrawn, %s superseded)",
            run.id,
            run.status,
            run.added_count,
            run.updated_count,
            run.withdrawn_count,
            run.superseded_count,
        )
    except RunInProgress:
        logger.warning("scheduled run skipped: another run is still in progress")
    except Exception:  # noqa: BLE001 - a failed run must not kill the scheduler
        logger.exception("scheduled monitoring run failed")
        db.rollback()
    finally:
        db.close()


def apply_config(config: ScheduleConfig) -> None:
    """(Re)schedule the monitoring job to match `config`, live."""
    global _scheduler
    if not config.enabled:
        if _scheduler is not None and _scheduler.get_job(JOB_ID):
            _scheduler.remove_job(JOB_ID)
        logger.info("scheduled monitoring disabled")
        return

    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.start()
    _scheduler.add_job(
        scheduled_run,
        build_trigger(config),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("scheduled monitoring: %s (next: %s)", describe(config), next_run_time())


def start_scheduler() -> BackgroundScheduler | None:
    db = SessionLocal()
    try:
        config = load_config(db)
    finally:
        db.close()
    apply_config(config)
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
