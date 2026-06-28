"""
Run scheduled email digests.

Usage (from the backend/ directory):
    poetry run python scripts/run_digest.py leadership
    poetry run python scripts/run_digest.py ops
    poetry run python scripts/run_digest.py dq
    poetry run python scripts/run_digest.py client_health

Designed to be triggered by cron.  Example crontab:
    # Every weekday at 09:00
    0 9 * * 1-5  cd /opt/frammer-backend && poetry run python scripts/run_digest.py leadership
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
from app.notifications import email_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"leadership", "ops", "dq", "client_health"}


async def main(digest_type: str) -> None:
    if digest_type not in ALLOWED_TYPES:
        logger.error("Unknown digest type: %s. Allowed: %s", digest_type, ALLOWED_TYPES)
        sys.exit(1)

    logger.info("Running digest type: %s", digest_type)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT * FROM email_subscriptions "
            "WHERE report_type = :rt AND is_enabled = true"
        ), {"rt": digest_type})).mappings().all()

        if not rows:
            logger.info("No enabled subscriptions for %s", digest_type)
            return

        sent, failed = 0, 0
        for sub in rows:
            recipients = json.loads(sub["recipients_json"])
            filters_json = json.loads(sub["filters_json"]) if sub.get("filters_json") else None
            try:
                await email_service.send_digest(
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
                sent += 1
                logger.info("  ✓ Subscription %s sent", sub["id"])
            except Exception:
                failed += 1
                logger.exception("  ✗ Subscription %s failed", sub["id"])

        await db.commit()
        logger.info("Done: %d sent, %d failed out of %d subscriptions", sent, failed, len(rows))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <{'|'.join(sorted(ALLOWED_TYPES))}>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
