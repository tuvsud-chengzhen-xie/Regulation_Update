"""Subscription matching and email alerting.

Authorised employees subscribe per standard, per committee (TC/SC), per
standards body or per keyword; when a monitoring run produces a change in a
subscribed area they receive one digest email.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractors.base import normalize_standard_id
from app.models import (
    SUBSCRIPTION_BODY,
    SUBSCRIPTION_COMMITTEE,
    SUBSCRIPTION_KEYWORD,
    SUBSCRIPTION_STANDARD,
    AlertLog,
    ChangeRecord,
    ExtractionRun,
    Source,
    Subscription,
    User,
)
from app.services import training

logger = logging.getLogger(__name__)

CHANGE_LABELS = {
    "added": "New / updated standard",
    "withdrawn": "Withdrawn",
    "reinstated": "Reinstated",
    "modified": "Details changed",
}


# --- matching ---------------------------------------------------------------

def subscription_matches(subscription: Subscription, change: ChangeRecord, source: Source) -> bool:
    value = (subscription.value or "").strip()
    if not value:
        return False

    if subscription.type == SUBSCRIPTION_STANDARD:
        wanted = normalize_standard_id(value)
        actual = normalize_standard_id(change.standard_id or "")
        # "EN 581-1" also matches "EN 581-1:2026" so that subscribers follow a
        # standard across its versions.
        return actual == wanted or actual.startswith((f"{wanted}:", f"{wanted}-", f"{wanted}/"))

    if subscription.type == SUBSCRIPTION_COMMITTEE:
        wanted = value.upper()
        candidates = [(change.tc_code or "").upper(), (source.tc_code or "").upper()]
        return any(c == wanted or c.startswith(f"{wanted}/") for c in candidates if c)

    if subscription.type == SUBSCRIPTION_BODY:
        return (source.body or "").upper() == value.upper()

    if subscription.type == SUBSCRIPTION_KEYWORD:
        needle = value.lower()
        haystack = " ".join(
            filter(None, [change.standard_id, change.title_en, change.tc_code, source.title])
        ).lower()
        return needle in haystack

    return False


def matches_for_user(
    user: User, changes: list[tuple[ChangeRecord, Source]]
) -> list[tuple[ChangeRecord, Source, Subscription]]:
    matched: list[tuple[ChangeRecord, Source, Subscription]] = []
    subscriptions = [s for s in user.subscriptions if s.is_active]
    for change, source in changes:
        for subscription in subscriptions:
            if subscription_matches(subscription, change, source):
                matched.append((change, source, subscription))
                break
    return matched


# --- delivery ---------------------------------------------------------------

def dispatch_alerts(db: Session, run: ExtractionRun, *, session_id: str | None = None) -> int:
    """Send one digest per subscriber with matching changes. Returns the count."""
    session_id = session_id or training.SERVER_SESSION_ID

    rows = list(
        db.execute(
            select(ChangeRecord, Source)
            .join(Source, Source.id == ChangeRecord.source_id)
            .where(ChangeRecord.run_id == run.id)
        )
    )
    changes = [(change, source) for change, source in rows]
    if not changes:
        return 0

    users = list(
        db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.email))
    )

    sent = 0
    for user in users:
        matched = matches_for_user(user, changes)
        if not matched:
            continue
        subject = (
            f"[Standards Monitor] {len(matched)} update(s) in your subscribed areas "
            f"(run #{run.id})"
        )
        body = render_digest(user, run, matched)
        status, error = send_email(user.email, subject, body)
        db.add(
            AlertLog(
                run_id=run.id,
                user_id=user.id,
                email=user.email,
                subject=subject,
                body=body,
                change_count=len(matched),
                backend=settings.email_backend,
                status=status,
                error=error,
            )
        )
        if status == "sent":
            sent += 1
        training.record_event(
            session_id,
            "Subscriptions",
            training.build_event(
                action="alert_send",
                session_id=session_id,
                metadata={
                    "runId": run.id,
                    "recipient": user.email,
                    "changeCount": len(matched),
                    "backend": settings.email_backend,
                    "status": status,
                },
            ),
        )
    db.flush()
    return sent


def render_digest(
    user: User, run: ExtractionRun, matched: list[tuple[ChangeRecord, Source, Subscription]]
) -> str:
    lines = [
        f"Hello {user.full_name or user.email},",
        "",
        f"Monitoring run #{run.id} ({run.mode} mode, finished "
        f"{(run.finished_at or datetime.now(timezone.utc)):%Y-%m-%d %H:%M UTC}) "
        f"found {len(matched)} update(s) in areas you subscribe to.",
        "",
    ]
    by_type: dict[str, list[str]] = {}
    for change, source, subscription in matched:
        entry = (
            f"  - {change.standard_id}  [{source.body} {change.tc_code or source.tc_code}]"
            f"\n      {change.title_en or '(no English title)'}"
            f"\n      matched your {subscription.type} subscription '{subscription.value}'"
        )
        by_type.setdefault(change.change_type, []).append(entry)

    for change_type, entries in by_type.items():
        lines.append(f"{CHANGE_LABELS.get(change_type, change_type)} ({len(entries)}):")
        lines.extend(entries)
        lines.append("")

    lines += [
        "You are receiving this because of your subscriptions in the Automated",
        "Standard Update Monitoring database. Manage them at /api/subscriptions.",
        "",
        "-- TUV SUD Standards Monitor",
    ]
    return "\n".join(lines)


def send_email(to_address: str, subject: str, body: str) -> tuple[str, str | None]:
    """Deliver a message. Returns (status, error)."""
    if settings.email_backend == "console":
        settings.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe = to_address.replace("@", "_at_").replace("/", "_")
        path = settings.outbox_dir / f"{stamp}_{safe}.txt"
        path.write_text(
            f"To: {to_address}\nFrom: {settings.email_from}\nSubject: {subject}\n\n{body}\n",
            encoding="utf-8",
        )
        logger.info("alert written to %s", path)
        return "sent", None

    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
        return "sent", None
    except Exception as exc:  # noqa: BLE001 - a failed alert must not fail the run
        logger.error("failed to send alert to %s: %s", to_address, exc)
        return "failed", str(exc)
