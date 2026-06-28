"""Add is_processed, sla_breach_flag, backlog_age_bucket to fact_video.

Phase 1 semantic correctness:
  - is_processed  : materialized Boolean (created_duration_sec > 0), replaces the
                    inline CASE expression scattered across every query.
  - sla_breach_flag : TRUE when publishing_lag_sec exceeds the SLA threshold
                      (default 7 days = 604 800 s).  Populated by ingest.
  - backlog_age_bucket : categorical bucket derived from publishing_lag_sec.
                         Populated by ingest.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# SLA threshold used to set sla_breach_flag (seconds)
_SLA_SEC = 7 * 24 * 3600  # 7 days


def upgrade() -> None:
    # ── New columns ────────────────────────────────────────────────────────────
    op.add_column(
        "fact_video",
        sa.Column(
            "is_processed",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "fact_video",
        sa.Column("sla_breach_flag", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "fact_video",
        sa.Column("backlog_age_bucket", sa.String(20), nullable=True),
    )

    # ── Index on is_processed for fast funnel queries ──────────────────────────
    op.create_index("ix_fact_video_is_processed", "fact_video", ["is_processed"])

    # ── Back-fill is_processed from existing created_duration_sec data ─────────
    op.execute("""
        UPDATE fact_video
        SET is_processed = (COALESCE(created_duration_sec, 0) > 0)
    """)

    # ── Back-fill sla_breach_flag and backlog_age_bucket where lag is known ────
    op.execute(f"""
        UPDATE fact_video
        SET
            sla_breach_flag = (publishing_lag_sec > {_SLA_SEC}),
            backlog_age_bucket = CASE
                WHEN publishing_lag_sec IS NULL                  THEN NULL
                WHEN publishing_lag_sec <= 86400                 THEN '< 1 day'
                WHEN publishing_lag_sec <= 3 * 86400             THEN '1-3 days'
                WHEN publishing_lag_sec <= 7 * 86400             THEN '3-7 days'
                ELSE                                                  '> 7 days'
            END
        WHERE publishing_lag_sec IS NOT NULL
    """)

    # ── Refresh analytics views to use is_processed ────────────────────────────
    # v_monthly_summary
    op.execute("DROP VIEW IF EXISTS v_monthly_summary")
    op.execute("""
        CREATE OR REPLACE VIEW v_monthly_summary AS
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int  AS year,
            EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int  AS month,
            COUNT(*)                                             AS total_uploaded,
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)       AS total_processed,
            SUM(CASE WHEN published    THEN 1 ELSE 0 END)        AS total_published,
            COALESCE(SUM(uploaded_duration_sec),  0)/3600.0     AS uploaded_duration_hrs,
            COALESCE(SUM(created_duration_sec),   0)/3600.0     AS created_duration_hrs,
            COALESCE(SUM(published_duration_sec), 0)/3600.0     AS published_duration_hrs
        FROM fact_video
        WHERE uploaded_at IS NOT NULL
        GROUP BY year, month
    """)

    # v_channel_summary
    op.execute("DROP VIEW IF EXISTS v_channel_summary")
    op.execute("""
        CREATE OR REPLACE VIEW v_channel_summary AS
        SELECT
            dc.name                                              AS channel,
            dc.obfuscated_code,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)    AS total_processed,
            SUM(CASE WHEN fv.published    THEN 1 ELSE 0 END)     AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        GROUP BY dc.name, dc.obfuscated_code
    """)

    # v_user_summary
    op.execute("DROP VIEW IF EXISTS v_user_summary")
    op.execute("""
        CREATE OR REPLACE VIEW v_user_summary AS
        SELECT
            du.name                                              AS "user",
            du.team_name,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)    AS total_processed,
            SUM(CASE WHEN fv.published    THEN 1 ELSE 0 END)     AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        GROUP BY du.name, du.team_name
    """)

    # v_language_summary
    op.execute("DROP VIEW IF EXISTS v_language_summary")
    op.execute("""
        CREATE OR REPLACE VIEW v_language_summary AS
        SELECT
            dl.iso_code, dl.display_name,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)    AS total_processed,
            SUM(CASE WHEN fv.published    THEN 1 ELSE 0 END)     AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        GROUP BY dl.iso_code, dl.display_name
    """)

    # v_input_type_summary
    op.execute("DROP VIEW IF EXISTS v_input_type_summary")
    op.execute("""
        CREATE OR REPLACE VIEW v_input_type_summary AS
        SELECT
            dit.name                                             AS input_type,
            COUNT(fv.id)                                         AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)    AS total_processed,
            SUM(CASE WHEN fv.published    THEN 1 ELSE 0 END)     AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0  AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0  AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_input_type dit ON dit.id = fv.input_type_id
        GROUP BY dit.name
    """)


def downgrade() -> None:
    # Restore views without is_processed
    op.execute("DROP VIEW IF EXISTS v_input_type_summary")
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
    op.execute("DROP VIEW IF EXISTS v_language_summary")
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
    op.execute("DROP VIEW IF EXISTS v_user_summary")
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
    op.execute("DROP VIEW IF EXISTS v_channel_summary")
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
    op.execute("DROP VIEW IF EXISTS v_monthly_summary")
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

    op.drop_index("ix_fact_video_is_processed", table_name="fact_video")
    op.drop_column("fact_video", "backlog_age_bucket")
    op.drop_column("fact_video", "sla_breach_flag")
    op.drop_column("fact_video", "is_processed")
