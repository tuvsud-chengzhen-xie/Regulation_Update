"""Subscription and alert management.

Subscriptions can be per standard, per committee (TC/SC), per standards body or
per keyword. Users manage their own; administrators may act for anyone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.config import settings
from app.models import ROLE_ADMIN, AlertLog, ExtractionRun, Subscription, User
from app.schemas import AlertOut, SubscriptionIn, SubscriptionOut
from app.security import CurrentUser, DbSession
from app.services import training
from app.services.notify import send_email

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions & alerts"])


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(db: DbSession, user: CurrentUser, all_users: bool = False):
    query = select(Subscription).order_by(Subscription.id)
    if not (all_users and user.role == ROLE_ADMIN):
        query = query.where(Subscription.user_id == user.id)
    return list(db.scalars(query))


@router.post("", response_model=SubscriptionOut, status_code=201)
def create_subscription(payload: SubscriptionIn, db: DbSession, user: CurrentUser):
    target_id = user.id
    if payload.user_id is not None and payload.user_id != user.id:
        if user.role != ROLE_ADMIN:
            raise HTTPException(
                status_code=403, detail="Only administrators can subscribe on behalf of others"
            )
        if db.get(User, payload.user_id) is None:
            raise HTTPException(status_code=404, detail="Target user not found")
        target_id = payload.user_id

    existing = db.scalar(
        select(Subscription).where(
            Subscription.user_id == target_id,
            Subscription.type == payload.type,
            Subscription.value == payload.value,
        )
    )
    if existing is not None:
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return existing

    subscription = Subscription(user_id=target_id, type=payload.type, value=payload.value)
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    training.record_event(
        training.SERVER_SESSION_ID,
        "Subscriptions",
        training.build_event(
            action="subscription_create",
            session_id=training.SERVER_SESSION_ID,
            after={"type": subscription.type, "value": subscription.value},
            metadata={"inputMethod": "ui_button", "userId": target_id},
        ),
    )
    return subscription


@router.delete("/{subscription_id}", status_code=204)
def delete_subscription(subscription_id: int, db: DbSession, user: CurrentUser) -> None:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if subscription.user_id != user.id and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Not your subscription")

    before = {"type": subscription.type, "value": subscription.value}
    db.delete(subscription)
    db.commit()
    training.record_event(
        training.SERVER_SESSION_ID,
        "Subscriptions",
        training.build_event(
            action="subscription_delete",
            session_id=training.SERVER_SESSION_ID,
            before=before,
            metadata={"inputMethod": "ui_button", "userId": user.id},
        ),
    )


@router.get("/alerts", response_model=list[AlertOut], summary="Alerts sent to me")
def list_alerts(
    db: DbSession,
    user: CurrentUser,
    all_users: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
):
    query = select(AlertLog).order_by(AlertLog.id.desc()).limit(limit)
    if not (all_users and user.role == ROLE_ADMIN):
        query = query.where(AlertLog.user_id == user.id)
    return list(db.scalars(query))


@router.get("/alerts/{alert_id}", summary="Full text of one alert")
def get_alert(alert_id: int, db: DbSession, user: CurrentUser) -> dict:
    alert = db.get(AlertLog, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.user_id != user.id and user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Not your alert")
    return {
        "id": alert.id,
        "run_id": alert.run_id,
        "email": alert.email,
        "subject": alert.subject,
        "body": alert.body,
        "status": alert.status,
        "error": alert.error,
        "sent_at": alert.sent_at,
    }


@router.post("/alerts/resend-failed", summary="Retry alerts whose delivery failed")
def resend_failed_alerts(db: DbSession, user: CurrentUser, all_users: bool = False) -> dict:
    """Resend failed alerts oldest first, stopping at the first failure.

    A failure usually means the mail server is unreachable, and each further
    attempt would only wait out another SMTP timeout.
    """
    query = select(AlertLog).where(AlertLog.status == "failed").order_by(AlertLog.id)
    if not (all_users and user.role == ROLE_ADMIN):
        query = query.where(AlertLog.user_id == user.id)
    pending = list(db.scalars(query))

    sent, error = 0, None
    for alert in pending:
        status, error = send_email(alert.email, alert.subject, alert.body)
        alert.status = status
        alert.error = error
        alert.backend = settings.email_backend
        alert.sent_at = datetime.now(timezone.utc)
        if status == "sent":
            sent += 1
            run = db.get(ExtractionRun, alert.run_id) if alert.run_id else None
            if run is not None:
                run.alerts_sent = (run.alerts_sent or 0) + 1
        db.commit()
        if status != "sent":
            break

    return {
        "pending": len(pending),
        "sent": sent,
        "failed": len(pending) - sent,
        "error": error if sent < len(pending) else None,
    }
