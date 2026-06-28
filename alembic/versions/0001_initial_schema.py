"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-06 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── dim_client ─────────────────────────────────────────────────────────────
    op.create_table(
        "dim_client",
        sa.Column("id",   sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("slug"),
    )

    # ── dim_channel ────────────────────────────────────────────────────────────
    op.create_table(
        "dim_channel",
        sa.Column("id",               sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name",             sa.String(255), nullable=False),
        sa.Column("obfuscated_code",  sa.String(10),  nullable=True),
        sa.Column("client_id",        sa.Integer(),   nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "client_id", name="uq_channel_client"),
    )

    # ── dim_user ───────────────────────────────────────────────────────────────
    op.create_table(
        "dim_user",
        sa.Column("id",        sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name",      sa.String(255), nullable=False),
        sa.Column("email",     sa.String(255), nullable=True),
        sa.Column("team_name", sa.String(255), nullable=True),
        sa.Column("client_id", sa.Integer(),   nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "client_id", name="uq_user_client"),
    )

    # ── dim_language ───────────────────────────────────────────────────────────
    op.create_table(
        "dim_language",
        sa.Column("id",           sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("iso_code",     sa.String(10),  nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("iso_code"),
    )

    # ── dim_input_type ─────────────────────────────────────────────────────────
    op.create_table(
        "dim_input_type",
        sa.Column("id",   sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ── dim_output_type ────────────────────────────────────────────────────────
    op.create_table(
        "dim_output_type",
        sa.Column("id",   sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ── dim_date ───────────────────────────────────────────────────────────────
    op.create_table(
        "dim_date",
        sa.Column("id",          sa.Integer(),     autoincrement=True, nullable=False),
        sa.Column("date",        sa.Date(),         nullable=False),
        sa.Column("year",        sa.SmallInteger(), nullable=False),
        sa.Column("month",       sa.SmallInteger(), nullable=False),
        sa.Column("quarter",     sa.SmallInteger(), nullable=False),
        sa.Column("week",        sa.SmallInteger(), nullable=False),
        sa.Column("month_label", sa.String(10),     nullable=False),
        sa.Column("is_weekend",  sa.Boolean(),      nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date"),
    )

    # ── fact_video ─────────────────────────────────────────────────────────────
    op.create_table(
        "fact_video",
        sa.Column("id",                    postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id",              sa.String(100), nullable=True),
        sa.Column("headline",              sa.Text(),      nullable=True),
        sa.Column("source_url",            sa.Text(),      nullable=True),
        sa.Column("client_id",             sa.Integer(),   nullable=True),
        sa.Column("channel_id",            sa.Integer(),   nullable=True),
        sa.Column("user_id",               sa.Integer(),   nullable=True),
        sa.Column("language_id",           sa.Integer(),   nullable=True),
        sa.Column("input_type_id",         sa.Integer(),   nullable=True),
        sa.Column("uploaded_at",           sa.Integer(),   nullable=True),
        sa.Column("published",             sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("published_platform",    sa.String(100), nullable=True),
        sa.Column("published_url",         sa.Text(),      nullable=True),
        sa.Column("uploaded_duration_sec", sa.Integer(),   nullable=True),
        sa.Column("created_duration_sec",  sa.Integer(),   nullable=True),
        sa.Column("published_duration_sec",sa.Integer(),   nullable=True),
        sa.ForeignKeyConstraint(["client_id"],       ["dim_client.id"],     ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["channel_id"],      ["dim_channel.id"],    ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"],         ["dim_user.id"],       ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["language_id"],     ["dim_language.id"],   ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["input_type_id"],   ["dim_input_type.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fact_video_channel_id",   "fact_video", ["channel_id"])
    op.create_index("ix_fact_video_user_id",      "fact_video", ["user_id"])
    op.create_index("ix_fact_video_language_id",  "fact_video", ["language_id"])
    op.create_index("ix_fact_video_uploaded_at",  "fact_video", ["uploaded_at"])
    op.create_index("ix_fact_video_published",    "fact_video", ["published"])
    op.create_index("ix_fact_video_video_id",     "fact_video", ["video_id"])

    # ── fact_video_output_type ─────────────────────────────────────────────────
    op.create_table(
        "fact_video_output_type",
        sa.Column("video_id",       postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("output_type_id", sa.Integer(),   nullable=False),
        sa.Column("created_count",  sa.Integer(),   nullable=False, server_default="0"),
        sa.Column("published_count",sa.Integer(),   nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["video_id"],       ["fact_video.id"],     ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["output_type_id"], ["dim_output_type.id"],ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("video_id", "output_type_id"),
    )

    # ── Convenience analytics views ────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE VIEW v_monthly_summary AS
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int  AS year,
            EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int  AS month,
            COUNT(*)                                             AS total_uploaded,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)          AS total_published,
            COALESCE(SUM(uploaded_duration_sec),  0)/3600.0     AS uploaded_duration_hrs,
            COALESCE(SUM(created_duration_sec),   0)/3600.0     AS created_duration_hrs,
            COALESCE(SUM(published_duration_sec), 0)/3600.0     AS published_duration_hrs
        FROM fact_video
        WHERE uploaded_at IS NOT NULL
        GROUP BY year, month
    """)

    op.execute("""
        CREATE OR REPLACE VIEW v_channel_summary AS
        SELECT
            dc.name                                              AS channel,
            dc.obfuscated_code,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)       AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        GROUP BY dc.name, dc.obfuscated_code
    """)

    op.execute("""
        CREATE OR REPLACE VIEW v_user_summary AS
        SELECT
            du.name                                              AS "user",
            du.team_name,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)       AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        GROUP BY du.name, du.team_name
    """)

    op.execute("""
        CREATE OR REPLACE VIEW v_language_summary AS
        SELECT
            dl.iso_code, dl.display_name,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)       AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        GROUP BY dl.iso_code, dl.display_name
    """)

    op.execute("""
        CREATE OR REPLACE VIEW v_input_type_summary AS
        SELECT
            dit.name                                             AS input_type,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)       AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_input_type dit ON dit.id = fv.input_type_id
        GROUP BY dit.name
    """)

    op.execute("""
        CREATE OR REPLACE VIEW v_output_type_summary AS
        SELECT
            dot.name                                             AS output_type,
            COALESCE(SUM(fvot.created_count),  0)               AS total_created,
            COALESCE(SUM(fvot.published_count),0)               AS total_published
        FROM fact_video_output_type fvot
        JOIN dim_output_type dot ON dot.id = fvot.output_type_id
        GROUP BY dot.name
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_output_type_summary")
    op.execute("DROP VIEW IF EXISTS v_input_type_summary")
    op.execute("DROP VIEW IF EXISTS v_language_summary")
    op.execute("DROP VIEW IF EXISTS v_user_summary")
    op.execute("DROP VIEW IF EXISTS v_channel_summary")
    op.execute("DROP VIEW IF EXISTS v_monthly_summary")
    op.drop_table("fact_video_output_type")
    op.drop_table("fact_video")
    op.drop_table("dim_date")
    op.drop_table("dim_output_type")
    op.drop_table("dim_input_type")
    op.drop_table("dim_language")
    op.drop_table("dim_user")
    op.drop_table("dim_channel")
    op.drop_table("dim_client")
