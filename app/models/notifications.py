"""SQLAlchemy ORM models – Notification tables."""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class EmailSubscription(Base):
    """Recurring email digest subscription."""

    __tablename__ = "email_subscriptions"

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    recipients_json: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC")
    filters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_client.id", ondelete="SET NULL"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class AlertRule(Base):
    """Threshold-based alert rule."""

    __tablename__ = "alert_rules"

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    filters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold_value: Mapped[float] = mapped_column(Float, nullable=False)
    comparison_operator: Mapped[str] = mapped_column(String(10), nullable=False)
    recipients_json: Mapped[str] = mapped_column(Text, nullable=False)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=360)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_triggered_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EmailDeliveryLog(Base):
    """Persisted email delivery result."""

    __tablename__ = "email_delivery_logs"

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    subscription_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("email_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    alert_rule_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    recipients_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="ses")
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
