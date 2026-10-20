"""Application settings.

All values can be overridden with environment variables or a local `.env` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Automated Standard Update Monitoring"
    app_version: str = "1.1.0"

    # --- Storage -----------------------------------------------------------
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'standards.db'}"
    export_dir: Path = BASE_DIR / "data" / "exports"

    # --- Extraction --------------------------------------------------------
    # Extraction is always live: every run fetches the real standards-body URLs.
    http_timeout_seconds: float = 60.0
    http_user_agent: str = (
        "Mozilla/5.0 (compatible; TUVSUD-StandardMonitor/1.0; +https://www.tuvsud.com)"
    )
    live_fetch_delay_seconds: float = 1.5  # be polite between requests

    # --- Fetch reliability -------------------------------------------------
    # Transient failures (timeouts, connection resets, 429, 5xx, browser
    # crashes) are retried with exponential backoff; permanent ones (404, 403,
    # an interstitial check that never clears) fail at once.
    fetch_max_attempts: int = 3
    fetch_retry_backoff_seconds: float = 3.0
    # When the headless browser cannot start on this host, try plain HTTPS
    # before failing a browser-backed source.
    browser_http_fallback: bool = True

    # --- Withdrawal validation ---------------------------------------------
    # A run that would withdraw more than this share of a source's current
    # standards is treated as a suspect extraction and not applied, unless the
    # administrator explicitly allows it for that run.
    max_withdrawal_ratio: float = 0.5
    withdrawal_guard_min_current: int = 10

    # --- Browser-assisted fetching -----------------------------------------
    # Used by sources whose listing is built client-side or served behind an
    # interstitial check (ISO, BIFMA, ALI, WSTDA).
    browser_headless: bool = True
    browser_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    browser_timeout_seconds: float = 60.0
    # How long to let a site's own interstitial finish before giving up.
    browser_challenge_wait_seconds: float = 25.0
    # How long to wait for the listing selector once the page is through. This,
    # not a network-idle wait, is what proves the listing actually rendered.
    browser_content_wait_seconds: float = 20.0

    # --- Scheduling --------------------------------------------------------
    # These are the defaults until an administrator saves a schedule in the
    # portal (Runs > Schedule); the saved schedule then takes precedence.
    scheduler_enabled: bool = False
    # Monitoring frequency is "TBD" in the spec; monthly on the 1st at 02:00 is
    # the default and can be changed without touching code.
    schedule_cron_day: str = "1"
    schedule_cron_hour: str = "2"
    schedule_cron_minute: str = "0"
    # Coverage targets: how many examples of each change type the monitoring
    # period should collect before it is considered sufficient.
    coverage_target_added: int = 5
    coverage_target_updated: int = 5
    coverage_target_withdrawn: int = 5

    # --- Email alerts ------------------------------------------------------
    # "console" writes alerts to data/outbox/ instead of sending them.
    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "standards-monitor@tuvsud.com"
    outbox_dir: Path = BASE_DIR / "data" / "outbox"
    # Portal address used in alert emails; on App Service WEBSITE_HOSTNAME is used if unset.
    public_base_url: str = ""

    # --- Access control ----------------------------------------------------
    # Bootstrap administrator created on first startup.
    bootstrap_admin_email: str = "chengzhen.xie@tuvsud.com"
    bootstrap_admin_api_key: str = "admin-demo-key"

    # --- AI training-data framework (Unified Annotation Framework v1) ------
    training_data_dir: Path = BASE_DIR / "AI_Training_Data"
    training_schema_version: str = "1.1.0"
    training_capture_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for directory in (
        settings.export_dir,
        settings.outbox_dir,
        settings.training_data_dir,
        BASE_DIR / "data",
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
