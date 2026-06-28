"""Delivery log persistence helpers."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def log_delivery(
    db: AsyncSession,
    *,
    report_type: str,
    recipients: list[str],
    status: str,
    subscription_id: uuid.UUID | None = None,
    alert_rule_id: uuid.UUID | None = None,
    provider: str = "ses",
    provider_message_id: str | None = None,
    error_text: str | None = None,
    payload_snapshot: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert a delivery log row and return its id."""
    log_id = uuid.uuid4()
    now = int(time.time())

    # Truncate payload snapshot to prevent large blobs
    snapshot_json = None
    if payload_snapshot:
        raw = json.dumps(payload_snapshot, default=str)
        snapshot_json = raw[:4096] if len(raw) > 4096 else raw

    await db.execute(
        text("""
            INSERT INTO email_delivery_logs
                (id, subscription_id, alert_rule_id, report_type, recipients_json,
                 status, provider, provider_message_id, error_text,
                 payload_snapshot_json, created_at)
            VALUES
                (:id, :subscription_id, :alert_rule_id, :report_type, :recipients_json,
                 :status, :provider, :provider_message_id, :error_text,
                 :payload_snapshot_json, :created_at)
        """),
        {
            "id": log_id,
            "subscription_id": subscription_id,
            "alert_rule_id": alert_rule_id,
            "report_type": report_type,
            "recipients_json": json.dumps(recipients),
            "status": status,
            "provider": provider,
            "provider_message_id": provider_message_id,
            "error_text": error_text,
            "payload_snapshot_json": snapshot_json,
            "created_at": now,
        },
    )
    return log_id


async def get_delivery_history(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    subscription_id: uuid.UUID | None = None,
    alert_rule_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return recent delivery logs and total count.

    Returns ``(rows, total_count)``."""
    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if subscription_id:
        where_parts.append("subscription_id = :sub_id")
        params["sub_id"] = subscription_id
    if alert_rule_id:
        where_parts.append("alert_rule_id = :alert_id")
        params["alert_id"] = alert_rule_id

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM email_delivery_logs {where_sql}"), params
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        text(f"""
            SELECT id, subscription_id, alert_rule_id, report_type,
                   recipients_json, status, provider, provider_message_id,
                   error_text, created_at
            FROM email_delivery_logs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    rows = []
    for row in result.mappings():
        r = dict(row)
        r["recipients"] = json.loads(r.pop("recipients_json", "[]"))
        rows.append(r)

    return rows, total
