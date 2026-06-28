"""Thin wrapper around AWS SES for sending emails."""
from __future__ import annotations

import logging
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _get_client():
    settings = get_settings()
    return boto3.client(
        "ses",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def _validate_recipients(recipients: list[str]) -> None:
    """Validate email format and check against allowlist if configured."""
    settings = get_settings()
    for email in recipients:
        if not _EMAIL_RE.match(email):
            raise ValueError(f"Invalid email address: {email}")

    allowlist = settings.ses_recipient_allowlist
    if allowlist:
        blocked = [e for e in recipients if e.strip().lower() not in allowlist]
        if blocked:
            raise ValueError(
                f"Recipients not in allowlist: {', '.join(blocked)}. "
                f"Allowed: {', '.join(allowlist)}"
            )


def send_html_email(
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> dict[str, Any]:
    """Send an HTML email via SES.

    Returns dict with ``message_id`` and ``status`` on success.
    Raises ``RuntimeError`` if SES is disabled or send fails.
    """
    settings = get_settings()

    if not settings.SES_ENABLED:
        raise RuntimeError(
            "SES sending is disabled (SES_ENABLED=false). "
            "Enable it in your environment to send emails."
        )

    _validate_recipients(to)

    body: dict[str, Any] = {"Html": {"Charset": "UTF-8", "Data": html_body}}
    if text_body:
        body["Text"] = {"Charset": "UTF-8", "Data": text_body}

    try:
        client = _get_client()
        response = client.send_email(
            Source=settings.SES_FROM_EMAIL,
            Destination={"ToAddresses": to},
            Message={
                "Subject": {"Charset": "UTF-8", "Data": subject},
                "Body": body,
            },
        )
        message_id = response["MessageId"]
        logger.info("SES email sent: message_id=%s, to=%s", message_id, to)
        return {"message_id": message_id, "status": "sent"}

    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error("SES send failed: %s – %s", error_code, error_msg)
        raise RuntimeError(f"SES send failed: {error_code} – {error_msg}") from exc
