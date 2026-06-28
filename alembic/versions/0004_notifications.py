"""Add email_subscriptions, alert_rules, email_delivery_logs tables.

Notification system tables for scheduled digests, threshold-based alerts,
and email delivery audit logging.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── email_subscriptions ────────────────────────────────────────────────
    op.create_table(
        "email_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column("recipients_json", sa.Text, nullable=False),
        sa.Column("frequency", sa.String(20), nullable=False),
        sa.Column("cron_expression", sa.String(100), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="UTC"),
        sa.Column("filters_json", sa.Text, nullable=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("dim_client.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("last_run_at", sa.Integer, nullable=True),
        sa.Column("next_run_at", sa.Integer, nullable=True),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )

    # ── alert_rules ────────────────────────────────────────────────────────
    op.create_table(
        "alert_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rule_type", sa.String(50), nullable=False),
        sa.Column("filters_json", sa.Text, nullable=True),
        sa.Column("threshold_value", sa.Float, nullable=False),
        sa.Column("comparison_operator", sa.String(10), nullable=False),
        sa.Column("recipients_json", sa.Text, nullable=False),
        sa.Column("cooldown_minutes", sa.Integer, nullable=False, server_default=sa.text("360")),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_triggered_at", sa.Integer, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_alert_rules_rule_type", "alert_rules", ["rule_type"])

    # ── email_delivery_logs ────────────────────────────────────────────────
    op.create_table(
        "email_delivery_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("email_subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("alert_rule_id", UUID(as_uuid=True), sa.ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column("recipients_json", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="ses"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("error_text", sa.Text, nullable=True),
        sa.Column("payload_snapshot_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_delivery_log_created_at", "email_delivery_logs", ["created_at"])
    op.create_index("ix_delivery_log_subscription_id", "email_delivery_logs", ["subscription_id"])


def downgrade() -> None:
    op.drop_table("email_delivery_logs")
    op.drop_table("alert_rules")
    op.drop_table("email_subscriptions")
