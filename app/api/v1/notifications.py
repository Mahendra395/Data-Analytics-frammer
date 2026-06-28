"""Public notification API routes.

All routes require Supabase JWT auth (via get_current_user dependency
applied at the router level in router.py).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_current_user, get_db
from app.notifications import email_service
from app.notifications.delivery_log import get_delivery_history
from app.schemas.notifications import (
    AlertRuleCreate,
    AlertRuleResponse,
    AlertRuleUpdate,
    DeliveryLogResponse,
    EmailSendResponse,
    SendReportRequest,
    SendTestEmailRequest,
    SubscriptionCreate,
    SubscriptionResponse,
    SubscriptionUpdate,
)

router = APIRouter(tags=["Notifications"])

# ═══════════════════════════════════════════════════════════════════════════════
# Manual send
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/email/send-report", response_model=EmailSendResponse)
async def send_report(
    body: SendReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> EmailSendResponse:
    """Send a named digest or manual report immediately."""
    try:
        result = await email_service.send_digest(
            db,
            report_type=body.report_type,
            recipients=body.recipients,
            filters_json=body.filters,
            client_id=body.client_id,
            subject=body.subject,
        )
        return EmailSendResponse(message_id=result.get("message_id"), status="sent")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/email/send-test", response_model=EmailSendResponse)
async def send_test_email(
    body: SendTestEmailRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> EmailSendResponse:
    """Send a simple test email to a single address."""
    from app.notifications.ses_client import send_html_email
    from app.notifications.delivery_log import log_delivery

    try:
        html = "<h2>Frammer Test Email</h2><p>This is a test from your Frammer instance.</p>"
        result = send_html_email([body.recipient], "Frammer – Test Email", html, "Frammer Test Email")
        await log_delivery(
            db, report_type="test", recipients=[body.recipient],
            status="sent", provider_message_id=result.get("message_id"),
        )
        return EmailSendResponse(message_id=result.get("message_id"), status="sent")
    except RuntimeError as exc:
        await log_delivery(
            db, report_type="test", recipients=[body.recipient],
            status="failed", error_text=str(exc)[:1000],
        )
        raise HTTPException(status_code=502, detail=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# Subscription CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/subscriptions", response_model=List[SubscriptionResponse])
async def list_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> List[SubscriptionResponse]:
    result = await db.execute(text(
        "SELECT * FROM email_subscriptions ORDER BY created_at DESC"
    ))
    rows = result.mappings().all()
    return [_sub_from_row(r) for r in rows]


@router.post("/subscriptions", response_model=SubscriptionResponse, status_code=201)
async def create_subscription(
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> SubscriptionResponse:
    now = int(time.time())
    sub_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO email_subscriptions
                (id, name, report_type, recipients_json, frequency, timezone,
                 filters_json, client_id, is_enabled, created_by, created_at, updated_at)
            VALUES
                (:id, :name, :report_type, :recipients_json, :frequency, :timezone,
                 :filters_json, :client_id, true, :created_by, :created_at, :updated_at)
        """),
        {
            "id": sub_id,
            "name": body.name,
            "report_type": body.report_type,
            "recipients_json": json.dumps(body.recipients),
            "frequency": body.frequency,
            "timezone": body.timezone,
            "filters_json": json.dumps(body.filters) if body.filters else None,
            "client_id": body.client_id,
            "created_by": current_user.get("email", "unknown"),
            "created_at": now,
            "updated_at": now,
        },
    )
    row = (await db.execute(
        text("SELECT * FROM email_subscriptions WHERE id = :id"), {"id": sub_id}
    )).mappings().one()
    return _sub_from_row(row)


@router.patch("/subscriptions/{sub_id}", response_model=SubscriptionResponse)
async def update_subscription(
    sub_id: uuid.UUID,
    body: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> SubscriptionResponse:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_parts: list[str] = []
    params: dict[str, Any] = {"id": sub_id, "updated_at": int(time.time())}

    if "recipients" in updates:
        set_parts.append("recipients_json = :recipients_json")
        params["recipients_json"] = json.dumps(updates["recipients"])
    if "filters" in updates:
        set_parts.append("filters_json = :filters_json")
        params["filters_json"] = json.dumps(updates["filters"]) if updates["filters"] else None
    for field in ("name", "frequency", "timezone", "is_enabled"):
        if field in updates:
            set_parts.append(f"{field} = :{field}")
            params[field] = updates[field]

    set_parts.append("updated_at = :updated_at")
    set_sql = ", ".join(set_parts)

    result = await db.execute(
        text(f"UPDATE email_subscriptions SET {set_sql} WHERE id = :id RETURNING *"), params
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _sub_from_row(row)


@router.delete("/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    result = await db.execute(
        text("DELETE FROM email_subscriptions WHERE id = :id"), {"id": sub_id}
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Subscription not found")


# ═══════════════════════════════════════════════════════════════════════════════
# Alert Rule CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/alerts", response_model=List[AlertRuleResponse])
async def list_alert_rules(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> List[AlertRuleResponse]:
    result = await db.execute(text(
        "SELECT * FROM alert_rules ORDER BY created_at DESC"
    ))
    return [_alert_from_row(r) for r in result.mappings().all()]


@router.post("/alerts", response_model=AlertRuleResponse, status_code=201)
async def create_alert_rule(
    body: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> AlertRuleResponse:
    now = int(time.time())
    rule_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO alert_rules
                (id, name, rule_type, filters_json, threshold_value, comparison_operator,
                 recipients_json, cooldown_minutes, is_enabled, created_by, created_at, updated_at)
            VALUES
                (:id, :name, :rule_type, :filters_json, :threshold_value, :comparison_operator,
                 :recipients_json, :cooldown_minutes, true, :created_by, :created_at, :updated_at)
        """),
        {
            "id": rule_id,
            "name": body.name,
            "rule_type": body.rule_type,
            "filters_json": json.dumps(body.filters) if body.filters else None,
            "threshold_value": body.threshold_value,
            "comparison_operator": body.comparison_operator,
            "recipients_json": json.dumps(body.recipients),
            "cooldown_minutes": body.cooldown_minutes,
            "created_by": current_user.get("email", "unknown"),
            "created_at": now,
            "updated_at": now,
        },
    )
    row = (await db.execute(
        text("SELECT * FROM alert_rules WHERE id = :id"), {"id": rule_id}
    )).mappings().one()
    return _alert_from_row(row)


@router.patch("/alerts/{rule_id}", response_model=AlertRuleResponse)
async def update_alert_rule(
    rule_id: uuid.UUID,
    body: AlertRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> AlertRuleResponse:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_parts: list[str] = []
    params: dict[str, Any] = {"id": rule_id, "updated_at": int(time.time())}

    if "recipients" in updates:
        set_parts.append("recipients_json = :recipients_json")
        params["recipients_json"] = json.dumps(updates["recipients"])
    if "filters" in updates:
        set_parts.append("filters_json = :filters_json")
        params["filters_json"] = json.dumps(updates["filters"]) if updates["filters"] else None
    for field in ("name", "threshold_value", "comparison_operator", "cooldown_minutes", "is_enabled"):
        if field in updates:
            set_parts.append(f"{field} = :{field}")
            params[field] = updates[field]

    set_parts.append("updated_at = :updated_at")
    set_sql = ", ".join(set_parts)

    result = await db.execute(
        text(f"UPDATE alert_rules SET {set_sql} WHERE id = :id RETURNING *"), params
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _alert_from_row(row)


@router.delete("/alerts/{rule_id}", status_code=204)
async def delete_alert_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    result = await db.execute(
        text("DELETE FROM alert_rules WHERE id = :id"), {"id": rule_id}
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert rule not found")


# ═══════════════════════════════════════════════════════════════════════════════
# Delivery logs
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/delivery-logs")
async def list_delivery_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    subscription_id: uuid.UUID | None = None,
    alert_rule_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    offset = (page - 1) * page_size
    rows, total = await get_delivery_history(
        db, limit=page_size, offset=offset,
        subscription_id=subscription_id, alert_rule_id=alert_rule_id,
    )
    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Row mapper helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _sub_from_row(row) -> SubscriptionResponse:
    r = dict(row)
    r["recipients"] = json.loads(r.pop("recipients_json", "[]"))
    fj = r.pop("filters_json", None)
    r["filters"] = json.loads(fj) if fj else None
    return SubscriptionResponse(**r)


def _alert_from_row(row) -> AlertRuleResponse:
    r = dict(row)
    r["recipients"] = json.loads(r.pop("recipients_json", "[]"))
    fj = r.pop("filters_json", None)
    r["filters"] = json.loads(fj) if fj else None
    return AlertRuleResponse(**r)
