"""
Evaluate all enabled alert rules and fire notifications.

Usage (from the backend/ directory):
    poetry run python scripts/evaluate_alerts.py

Designed to be triggered by cron.  Example crontab:
    # Every 15 minutes
    */15 * * * *  cd /opt/frammer-backend && poetry run python scripts/evaluate_alerts.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# ── Make app importable ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.notifications.alert_rules import evaluate_rule
from app.notifications.delivery_log import log_delivery
from app.notifications.ses_client import send_html_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Evaluating alert rules")

    async with AsyncSessionLocal() as db:
        rules = (await db.execute(text(
            "SELECT * FROM alert_rules WHERE is_enabled = true"
        ))).mappings().all()

        if not rules:
            logger.info("No enabled alert rules")
            return

        now = int(time.time())
        triggered_count, skipped, errors = 0, 0, 0

        for rule in rules:
            rule_dict = dict(rule)
            rule_dict["filters"] = json.loads(rule_dict.pop("filters_json", "null"))

            # Cooldown check
            last = rule_dict.get("last_triggered_at")
            cooldown_sec = (rule_dict.get("cooldown_minutes") or 360) * 60
            if last and (now - last) < cooldown_sec:
                skipped += 1
                continue

            try:
                fired, value = await evaluate_rule(db, rule_dict)
                if fired:
                    recipients = json.loads(rule["recipients_json"])
                    subject = f"Frammer Alert – {rule_dict['name']}"
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
                    triggered_count += 1
                    logger.info("  ⚡ Rule %s triggered (value=%s)", rule["id"], value)
                else:
                    logger.debug("  ○ Rule %s ok (value=%s)", rule["id"], value)
            except Exception:
                errors += 1
                logger.exception("  ✗ Rule %s error", rule["id"])

        await db.commit()
        logger.info(
            "Done: %d triggered, %d skipped (cooldown), %d errors, %d total",
            triggered_count, skipped, errors, len(rules),
        )


if __name__ == "__main__":
    asyncio.run(main())
