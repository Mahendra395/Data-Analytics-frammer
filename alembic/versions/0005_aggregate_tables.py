"""Add aggregate tables for modified CSV bundle.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agg_monthly_stat",
        sa.Column("month_label", sa.String(length=20), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("uploaded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_duration_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_duration_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_duration_sec", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_agg_monthly_stat_year", "agg_monthly_stat", ["year"])
    op.create_index("ix_agg_monthly_stat_month", "agg_monthly_stat", ["month"])

    for table_name, fk_table, fk_col in (
        ("agg_channel_stat", "dim_channel", "channel_id"),
        ("agg_user_stat", "dim_user", "user_id"),
        ("agg_input_type_stat", "dim_input_type", "input_type_id"),
        ("agg_language_stat", "dim_language", "language_id"),
        ("agg_output_type_stat", "dim_output_type", "output_type_id"),
        ("agg_channel_publishing", "dim_channel", "channel_id"),
        ("agg_channel_publishing_duration", "dim_channel", "channel_id"),
    ):
        extra_cols = []
        if table_name == "agg_channel_publishing":
            extra_cols = [
                sa.Column("facebook", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("instagram", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("linkedin", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("reels", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("shorts", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("x", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("youtube", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("threads", sa.Integer(), nullable=False, server_default="0"),
            ]
        elif table_name == "agg_channel_publishing_duration":
            extra_cols = [
                sa.Column("facebook_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("instagram_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("linkedin_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("reels_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("shorts_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("x_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("youtube_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("threads_duration_sec", sa.Integer(), nullable=False, server_default="0"),
            ]
        else:
            extra_cols = [
                sa.Column("uploaded_count", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("published_count", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("uploaded_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("created_duration_sec", sa.Integer(), nullable=False, server_default="0"),
                sa.Column("published_duration_sec", sa.Integer(), nullable=False, server_default="0"),
            ]
        op.create_table(
            table_name,
            sa.Column(
                fk_col,
                sa.Integer(),
                sa.ForeignKey(f"{fk_table}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            *extra_cols,
        )

    op.create_table(
        "agg_channel_user_stat",
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("dim_channel.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("dim_user.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("uploaded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_duration_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_duration_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_duration_sec", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    for table_name in (
        "agg_channel_user_stat",
        "agg_channel_publishing_duration",
        "agg_channel_publishing",
        "agg_output_type_stat",
        "agg_language_stat",
        "agg_input_type_stat",
        "agg_user_stat",
        "agg_channel_stat",
    ):
        op.drop_table(table_name)
    op.drop_index("ix_agg_monthly_stat_month", table_name="agg_monthly_stat")
    op.drop_index("ix_agg_monthly_stat_year", table_name="agg_monthly_stat")
    op.drop_table("agg_monthly_stat")
