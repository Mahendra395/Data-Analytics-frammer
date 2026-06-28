"""Add funnel timestamps, billable flag, lag fields, and quality flags

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-07 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Funnel timestamps ──────────────────────────────────────────────────────
    op.add_column("fact_video", sa.Column("processed_at",  sa.Integer(), nullable=True))
    op.add_column("fact_video", sa.Column("published_at",  sa.Integer(), nullable=True))

    # ── Billable flag ──────────────────────────────────────────────────────────
    op.add_column("fact_video", sa.Column("billable_flag", sa.Boolean(), nullable=False, server_default="false"))

    # ── Derived lag fields ─────────────────────────────────────────────────────
    op.add_column("fact_video", sa.Column("processing_lag_sec",   sa.Integer(), nullable=True))
    op.add_column("fact_video", sa.Column("publishing_lag_sec",   sa.Integer(), nullable=True))
    op.add_column("fact_video", sa.Column("total_cycle_lag_sec",  sa.Integer(), nullable=True))

    # ── Quality flags ─────────────────────────────────────────────────────────
    op.add_column("fact_video", sa.Column("missing_team_flag",       sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("fact_video", sa.Column("missing_platform_flag",   sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("fact_video", sa.Column("invalid_url_flag",        sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("fact_video", sa.Column("duplicate_video_id_flag", sa.Boolean(), nullable=False, server_default="false"))

    # ── Indexes ────────────────────────────────────────────────────────────────
    op.create_index("ix_fact_video_processed_at",  "fact_video", ["processed_at"])
    op.create_index("ix_fact_video_published_at",  "fact_video", ["published_at"])
    op.create_index("ix_fact_video_billable_flag", "fact_video", ["billable_flag"])


def downgrade() -> None:
    op.drop_index("ix_fact_video_billable_flag", table_name="fact_video")
    op.drop_index("ix_fact_video_published_at",  table_name="fact_video")
    op.drop_index("ix_fact_video_processed_at",  table_name="fact_video")

    op.drop_column("fact_video", "duplicate_video_id_flag")
    op.drop_column("fact_video", "invalid_url_flag")
    op.drop_column("fact_video", "missing_platform_flag")
    op.drop_column("fact_video", "missing_team_flag")
    op.drop_column("fact_video", "total_cycle_lag_sec")
    op.drop_column("fact_video", "publishing_lag_sec")
    op.drop_column("fact_video", "processing_lag_sec")
    op.drop_column("fact_video", "billable_flag")
    op.drop_column("fact_video", "published_at")
    op.drop_column("fact_video", "processed_at")
