"""Internal notification routes – called by cron or admin tools.

Auth: ``X-Internal-Token`` header checked against
``settings.INTERNAL_NOTIFICATIONS_TOKEN``.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings
from app.notifications import email_service
from app.notifications.alert_rules import evaluate_rule
from app.notifications.delivery_log import log_delivery

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Internal Notifications"])


# ── Auth dependency ────────────────────────────────────────────────────────────


async def verify_internal_token(
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
) -> None:
    """Validate the internal token from the request header."""
    settings = get_settings()
    if not settings.INTERNAL_NOTIFICATIONS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Internal notifications token not configured",
        )
    if x_internal_token != settings.INTERNAL_NOTIFICATIONS_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid internal token")


# ── Digest routes ──────────────────────────────────────────────────────────────


@router.post("/run-digest/{digest_type}")
async def run_digest(
    digest_type: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_token),
) -> Dict[str, Any]:
    """Run all enabled subscriptions of a given digest type."""
    allowed = {"leadership", "ops", "dq", "client_health"}
    if digest_type not in allowed:
        raise HTTPException(status_code=422, detail=f"digest_type must be one of {allowed}")

    subs = (await db.execute(text(
        "SELECT * FROM email_subscriptions WHERE report_type = :rt AND is_enabled = true"
    ), {"rt": digest_type})).mappings().all()

    results: list[dict] = []
    for sub in subs:
        recipients = json.loads(sub["recipients_json"])
        filters_json = json.loads(sub["filters_json"]) if sub.get("filters_json") else None
        try:
            result = await email_service.send_digest(
                db,
                report_type=digest_type,
                recipients=recipients,
                filters_json=filters_json,
                client_id=sub.get("client_id"),
                subscription_id=sub["id"],
            )
            await db.execute(text(
                "UPDATE email_subscriptions SET last_run_at = :now WHERE id = :id"
            ), {"now": int(time.time()), "id": sub["id"]})
            results.append({"subscription_id": str(sub["id"]), "status": "sent"})
        except Exception as exc:
            logger.exception("Failed to run subscription %s", sub["id"])
            results.append({"subscription_id": str(sub["id"]), "status": "failed", "error": str(exc)[:200]})

    return {"digest_type": digest_type, "subscriptions_processed": len(subs), "results": results}


@router.post("/run-subscription/{subscription_id}")
async def run_subscription(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_token),
) -> Dict[str, Any]:
    """Run a single subscription by ID."""
    row = (await db.execute(text(
        "SELECT * FROM email_subscriptions WHERE id = :id"
    ), {"id": subscription_id})).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    recipients = json.loads(row["recipients_json"])
    filters_json = json.loads(row["filters_json"]) if row.get("filters_json") else None

    result = await email_service.send_digest(
        db,
        report_type=row["report_type"],
        recipients=recipients,
        filters_json=filters_json,
        client_id=row.get("client_id"),
        subscription_id=subscription_id,
    )
    await db.execute(text(
        "UPDATE email_subscriptions SET last_run_at = :now WHERE id = :id"
    ), {"now": int(time.time()), "id": subscription_id})

    return {"subscription_id": str(subscription_id), "status": "sent", **result}


# ── Alert evaluation ──────────────────────────────────────────────────────────


@router.post("/evaluate-alerts")
async def evaluate_alerts(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_token),
) -> Dict[str, Any]:
    """Evaluate all enabled alert rules, send emails for triggered ones."""
    rules = (await db.execute(text(
        "SELECT * FROM alert_rules WHERE is_enabled = true"
    ))).mappings().all()

    now = int(time.time())
    results: list[dict] = []

    for rule in rules:
        rule_dict = dict(rule)
        rule_dict["filters"] = json.loads(rule_dict.pop("filters_json") or "null")

        # Check cooldown
        last = rule_dict.get("last_triggered_at")
        cooldown_sec = (rule_dict.get("cooldown_minutes") or 360) * 60
        if last and (now - last) < cooldown_sec:
            results.append({"rule_id": str(rule["id"]), "status": "skipped", "reason": "cooldown"})
            continue

        try:
            triggered, value = await evaluate_rule(db, rule_dict)
            if triggered:
                recipients = json.loads(rule["recipients_json"])
                subject = f"Frammer Alert – {rule_dict['name']}"
                from app.notifications.ses_client import send_html_email
                from app.notifications.template_renderer import render_template

                html = (
                    f"<h2>Alert: {rule_dict['name']}</h2>"
                    f"<p>Rule type: <strong>{rule_dict['rule_type']}</strong></p>"
                    f"<p>Current value: <strong>{value}</strong></p>"
                    f"<p>Threshold: {rule_dict['comparison_operator']} "
                    f"<strong>{rule_dict['threshold_value']}</strong></p>"
                )
                send_html_email(recipients, subject, html)

                await log_delivery(
                    db, report_type=f"alert_{rule_dict['rule_type']}",
                    recipients=recipients, status="sent", alert_rule_id=rule["id"],
                    payload_snapshot={"value": value, "threshold": rule_dict["threshold_value"]},
                )
                await db.execute(text(
                    "UPDATE alert_rules SET last_triggered_at = :now WHERE id = :id"
                ), {"now": now, "id": rule["id"]})
                results.append({"rule_id": str(rule["id"]), "status": "triggered", "value": value})
            else:
                results.append({"rule_id": str(rule["id"]), "status": "ok", "value": value})
        except Exception as exc:
            logger.exception("Failed to evaluate rule %s", rule["id"])
            results.append({"rule_id": str(rule["id"]), "status": "error", "error": str(exc)[:200]})

    return {"rules_evaluated": len(rules), "results": results}
