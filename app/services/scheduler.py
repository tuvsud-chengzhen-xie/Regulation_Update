"""Periodic monitoring.

The specification leaves the monitoring frequency as "TBD", so the cron
expression is configuration (default: 02:00 UTC on the 1st of each month) and
the scheduler is off unless `SCHEDULER_ENABLED=true`.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal

logger = logging.getLogger(__name__)

JOB_ID = "standards-monitoring-run"
_scheduler: BackgroundScheduler | None = None


def scheduled_run() -> None:
    from app.services.ingest import run_extraction

    db = SessionLocal()
    try:
        run = run_extraction(db, trigger="scheduled")
        logger.info(
            "scheduled run #%s finished: %s (+%s / -%s)",
            run.id,
            run.status,
            run.added_count,
            run.withdrawn_count,
        )
    except Exception:  # noqa: BLE001 - a failed run must not kill the scheduler
        logger.exception("scheduled monitoring run failed")
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=false)")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        scheduled_run,
        CronTrigger(
            day=settings.schedule_cron_day,
            hour=settings.schedule_cron_hour,
            minute=settings.schedule_cron_minute,
            timezone="UTC",
        ),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "scheduler started: day=%s hour=%s minute=%s (UTC)",
        settings.schedule_cron_day,
        settings.schedule_cron_hour,
        settings.schedule_cron_minute,
    )
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
