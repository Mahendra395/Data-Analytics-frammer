"""Email orchestration – build, render, send, log."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications import ses_client, delivery_log, template_renderer
from app.notifications.digest_builders import (
    build_client_health_digest,
    build_dq_digest,
    build_leadership_digest,
    build_ops_digest,
)

logger = logging.getLogger(__name__)

_TEMPLATE_MAP = {
    "leadership": "leadership_digest.html",
    "ops": "ops_digest.html",
    "dq": "dq_digest.html",
    "client_health": "client_health_digest.html",
    "manual": "manual_report.html",
}

_SUBJECT_MAP = {
    "leadership": "Frammer – Weekly Leadership Digest",
    "ops": "Frammer – Daily Operations Digest",
    "dq": "Frammer – Data Quality Digest",
    "client_health": "Frammer – Client Health Digest",
    "manual": "Frammer – Report",
}


async def _build_digest(
    db: AsyncSession,
    report_type: str,
    filters_json: dict[str, Any] | None = None,
    client_id: int | None = None,
) -> dict[str, Any]:
    """Route to the correct digest builder."""
    if report_type == "leadership":
        return await build_leadership_digest(db, filters_json)
    if report_type == "ops":
        return await build_ops_digest(db, filters_json)
    if report_type == "dq":
        return await build_dq_digest(db, filters_json)
    if report_type == "client_health":
        if client_id is None:
            raise ValueError("client_id is required for client_health digest")
        return await build_client_health_digest(db, client_id, filters_json)
    if report_type == "manual":
        # For manual reports, build a basic KPI snapshot
        return await build_leadership_digest(db, filters_json)
    raise ValueError(f"Unknown report_type: {report_type}")


async def send_digest(
    db: AsyncSession,
    *,
    report_type: str,
    recipients: list[str],
    filters_json: dict[str, Any] | None = None,
    client_id: int | None = None,
    subject: str | None = None,
    subscription_id=None,
    alert_rule_id=None,
) -> dict[str, Any]:
    """End-to-end: build data → render template → send via SES → log result."""
    final_subject = subject or _SUBJECT_MAP.get(report_type, "Frammer – Report")
    template_name = _TEMPLATE_MAP.get(report_type, "manual_report.html")

    try:
        # 1. Build data
        digest_data = await _build_digest(db, report_type, filters_json, client_id)

        # For manual template, add extra context
        if report_type == "manual":
            digest_data["subject"] = final_subject
            digest_data["filters_applied"] = filters_json or {}

        # 2. Render HTML
        html, plaintext = template_renderer.render_template(template_name, digest_data)

        # 3. Send
        result = ses_client.send_html_email(recipients, final_subject, html, plaintext)

        # 4. Log success
        await delivery_log.log_delivery(
            db,
            report_type=report_type,
            recipients=recipients,
            status="sent",
            subscription_id=subscription_id,
            alert_rule_id=alert_rule_id,
            provider_message_id=result.get("message_id"),
            payload_snapshot={"subject": final_subject, "report_type": report_type},
        )

        return result

    except Exception as exc:
        logger.exception("Failed to send %s digest", report_type)
        # Log failure
        await delivery_log.log_delivery(
            db,
            report_type=report_type,
            recipients=recipients,
            status="failed",
            subscription_id=subscription_id,
            alert_rule_id=alert_rule_id,
            error_text=str(exc)[:1000],
        )
        raise
