"""Build structured data payloads for email digests.

Each builder runs SQL queries using the same patterns as the analytics
endpoints (``text()`` + ``build_where_clause()``), assembles the result
into a plain dict that can be handed to ``template_renderer.render_template()``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams
from app.registry.filters import build_where_clause


def _filters_from_json(filters_json: dict[str, Any] | None) -> FilterParams:
    """Construct a FilterParams instance from a stored JSON dict.

    FilterParams is a FastAPI dependency class that uses Query(default=X) for
    its __init__ parameters. When instantiated directly (not via FastAPI DI),
    those defaults are Query objects, not the actual default values. We bypass
    __init__ entirely and set attributes manually to avoid this issue.
    """
    f: FilterParams = object.__new__(FilterParams)
    f.date_range = "all"
    f.client = None
    f.channel = None
    f.language = None
    f.team_member = None
    f.input_type = None
    f.output_type = None
    f.published_flag = None
    f.published_platform = None
    f.billable_flag = None
    f.date_from = None
    f.date_to = None
    f.compare_mode = None
    f.compare_date_from = None
    f.compare_date_to = None
    f.compare_period_label = ""
    if filters_json:
        _allowed = {
            "client", "channel", "language", "team_member", "input_type",
            "output_type", "published_flag", "published_platform", "billable_flag",
        }
        for k, v in filters_json.items():
            if k in _allowed and v is not None:
                setattr(f, k, v)
    return f


# ── Leadership digest ─────────────────────────────────────────────────────────


async def build_leadership_digest(
    db: AsyncSession,
    filters_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble all data sections for the weekly leadership digest."""
    f = _filters_from_json(filters_json)
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # ── KPI summary ──────────────────────────────────────────────────────
    kpi_row = (await db.execute(text(f"""
        SELECT
            COUNT(*)                                        AS total_uploaded,
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)  AS total_processed,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)     AS total_published,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0)                       AS publish_rate,
            COALESCE(SUM(created_duration_sec), 0) / 3600.0 AS processed_duration_hrs,
            COALESCE(SUM(published_duration_sec), 0) / 3600.0 AS published_duration_hrs
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    kpis = {k: (float(v) if v is not None else 0) for k, v in kpi_row.items()}

    # ── Top growth channel ───────────────────────────────────────────────
    top_growth = None
    try:
        growth_row = (await db.execute(text(f"""
            WITH monthly AS (
                SELECT
                    dc.name AS channel,
                    DATE_TRUNC('month', TO_TIMESTAMP(fv.uploaded_at) AT TIME ZONE 'UTC') AS mo,
                    COUNT(*) AS cnt
                FROM fact_video fv
                JOIN dim_channel dc ON dc.id = fv.channel_id
                {where_sql}
                GROUP BY dc.name, mo
            ),
            ranked AS (
                SELECT channel, mo, cnt,
                    LAG(cnt) OVER (PARTITION BY channel ORDER BY mo) AS prev_cnt,
                    ROW_NUMBER() OVER (ORDER BY mo DESC) AS rn
                FROM monthly
            )
            SELECT channel AS name, cnt AS count,
                   CASE WHEN prev_cnt > 0
                       THEN ((cnt - prev_cnt)::float / prev_cnt) * 100
                       ELSE NULL END AS growth_pct
            FROM ranked
            WHERE prev_cnt IS NOT NULL
            ORDER BY growth_pct DESC NULLS LAST
            LIMIT 1
        """), params)).mappings().first()

        if growth_row:
            top_growth = {
                "dimension": "Channel",
                "name": growth_row["name"],
                "count": int(growth_row["count"] or 0),
                "growth_pct": float(growth_row["growth_pct"]) if growth_row["growth_pct"] is not None else None,
            }
    except Exception:
        pass

    # ── Biggest risk (lowest publish rate channel with ≥10 videos) ─────
    biggest_risk = None
    try:
        risk_row = (await db.execute(text(f"""
            SELECT dc.name AS channel,
                   SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)::float
                       / NULLIF(COUNT(*), 0) AS publish_rate,
                   COUNT(*) AS count
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name
            HAVING COUNT(*) >= 10
            ORDER BY publish_rate ASC
            LIMIT 1
        """), params)).mappings().first()

        if risk_row:
            biggest_risk = {
                "channel": risk_row["channel"],
                "publish_rate": float(risk_row["publish_rate"] or 0),
                "count": int(risk_row["count"] or 0),
            }
    except Exception:
        pass

    # ── DQ summary ─────────────────────────────────────────────────────
    dq_summary = None
    try:
        dq_row = (await db.execute(text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE channel_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS ch_null,
                COUNT(*) FILTER (WHERE user_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS usr_null,
                COUNT(*) FILTER (WHERE language_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS lang_null,
                COUNT(*) FILTER (WHERE input_type_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS it_null,
                SUM(CASE WHEN invalid_url_flag THEN 1 ELSE 0 END) AS invalid_urls,
                SUM(CASE WHEN duplicate_video_id_flag THEN 1 ELSE 0 END) AS duplicate_ids
            FROM fact_video fv
            {where_sql}
        """), params)).mappings().one()

        penalties = sum(
            float(dq_row[c] or 0) * 0.25
            for c in ("ch_null", "usr_null", "lang_null", "it_null")
        )
        dq_summary = {
            "overall_score": round(100.0 - penalties, 1),
            "invalid_urls": int(dq_row["invalid_urls"] or 0),
            "duplicate_ids": int(dq_row["duplicate_ids"] or 0),
        }
    except Exception:
        pass

    # ── Funnel snapshot ────────────────────────────────────────────────
    funnel = [
        {"name": "Uploaded", "count": kpis.get("total_uploaded", 0)},
        {"name": "Processed", "count": kpis.get("total_processed", 0)},
        {"name": "Published", "count": kpis.get("total_published", 0)},
    ]

    # ── AI-powered insights (best-effort) ─────────────────────────────
    ai_executive_summary = None
    ai_top_risks: list[dict[str, Any]] = []
    try:
        from app.services.insight_engine import collect_insight_context
        from app.services.insight_llm import generate_insights

        ctx = await collect_insight_context(db, f)
        insights = await generate_insights(ctx)
        ai_executive_summary = insights.executive_summary
        ai_top_risks = [
            {"title": r.title, "severity": r.severity, "detail": r.detail}
            for r in (insights.top_risks or [])[:3]
        ]
    except Exception:
        pass

    return {
        "kpis": kpis,
        "top_growth": top_growth,
        "biggest_risk": biggest_risk,
        "dq_summary": dq_summary,
        "funnel": funnel,
        "executive_summary": ai_executive_summary,
        "top_risks": ai_top_risks,
        "period_label": f.date_range if f.date_range != "all" else "All Time",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": "#",
    }


# ── Ops digest ─────────────────────────────────────────────────────────────────


async def build_ops_digest(
    db: AsyncSession,
    filters_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble data for the daily operations digest."""
    f = _filters_from_json(filters_json)
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # ── Core KPIs ────────────────────────────────────────────────────────
    kpi_row = (await db.execute(text(f"""
        SELECT
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END) AS total_processed,
            SUM(CASE WHEN published THEN 1 ELSE 0 END) AS total_published
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    kpis = {k: int(v or 0) for k, v in kpi_row.items()}

    # ── Backlog ──────────────────────────────────────────────────────────
    backlog = None
    try:
        bl_row = (await db.execute(text(f"""
            SELECT
                COUNT(*) FILTER (WHERE is_processed AND NOT published) AS pending_count,
                SUM(CASE WHEN sla_breach_flag THEN 1 ELSE 0 END) AS sla_breaches
            FROM fact_video fv
            {where_sql}
        """), params)).mappings().one()
        backlog = {
            "pending_count": int(bl_row["pending_count"] or 0),
            "sla_breaches": int(bl_row["sla_breaches"] or 0),
        }
    except Exception:
        pass

    # ── Lagging channels ─────────────────────────────────────────────────
    lagging_channels: list[dict[str, Any]] = []
    try:
        lag_rows = (await db.execute(text(f"""
            SELECT dc.name AS channel,
                   COUNT(*) FILTER (WHERE fv.is_processed AND NOT fv.published) AS pending,
                   AVG(fv.processing_lag_sec) / 60.0 AS avg_lag_min
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name
            HAVING COUNT(*) FILTER (WHERE fv.is_processed AND NOT fv.published) > 0
            ORDER BY pending DESC
            LIMIT 5
        """), params)).mappings().all()
        for r in lag_rows:
            lagging_channels.append({
                "channel": r["channel"],
                "pending": int(r["pending"] or 0),
                "avg_lag_min": float(r["avg_lag_min"] or 0),
            })
    except Exception:
        pass

    return {
        "kpis": kpis,
        "backlog": backlog,
        "lagging_channels": lagging_channels,
        "period_label": f.date_range if f.date_range != "all" else "Today",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": "#",
    }


# ── DQ digest ──────────────────────────────────────────────────────────────────


async def build_dq_digest(
    db: AsyncSession,
    filters_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble data for the data quality digest."""
    f = _filters_from_json(filters_json)
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE channel_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS ch_null,
            COUNT(*) FILTER (WHERE user_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS usr_null,
            COUNT(*) FILTER (WHERE language_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS lang_null,
            COUNT(*) FILTER (WHERE input_type_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS it_null,
            SUM(CASE WHEN invalid_url_flag THEN 1 ELSE 0 END) AS invalid_urls,
            SUM(CASE WHEN duplicate_video_id_flag THEN 1 ELSE 0 END) AS duplicate_ids,
            SUM(CASE WHEN missing_team_flag THEN 1 ELSE 0 END) AS missing_team,
            SUM(CASE WHEN missing_platform_flag THEN 1 ELSE 0 END) AS missing_platform
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    total = int(row["total"] or 0)
    penalties = sum(float(row[c] or 0) * 0.25 for c in ("ch_null", "usr_null", "lang_null", "it_null"))
    dq_score = round(100.0 - penalties, 1)

    # Build failing fields list
    field_checks = [
        ("channel_id", float(row["ch_null"] or 0)),
        ("user_id", float(row["usr_null"] or 0)),
        ("language_id", float(row["lang_null"] or 0)),
        ("input_type_id", float(row["it_null"] or 0)),
    ]
    failing_fields = [
        {"field": name, "null_pct": pct}
        for name, pct in sorted(field_checks, key=lambda x: -x[1])
        if pct > 0
    ]

    return {
        "dq_score": dq_score,
        "total_records": total,
        "failing_fields": failing_fields,
        "invalid_urls": int(row["invalid_urls"] or 0),
        "duplicate_ids": int(row["duplicate_ids"] or 0),
        "missing_team": int(row["missing_team"] or 0),
        "missing_platform": int(row["missing_platform"] or 0),
        "period_label": f.date_range if f.date_range != "all" else "All Time",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": "#",
    }


# ── Client health digest ──────────────────────────────────────────────────────


async def build_client_health_digest(
    db: AsyncSession,
    client_id: int,
    filters_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble data for a per-client health digest."""
    f = _filters_from_json(filters_json)
    where, params = build_where_clause(f)
    where.append("fv.client_id = :_client_id")
    params["_client_id"] = client_id
    where_sql = "WHERE " + " AND ".join(where)

    # Client name
    cn = (await db.execute(
        text("SELECT name FROM dim_client WHERE id = :cid"), {"cid": client_id}
    )).scalar_one_or_none() or "Unknown"

    # KPIs
    kpi_row = (await db.execute(text(f"""
        SELECT
            COUNT(*) AS total_uploaded,
            SUM(CASE WHEN published THEN 1 ELSE 0 END) AS total_published,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0) AS publish_rate,
            COUNT(DISTINCT channel_id) AS active_channels
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    kpis = {
        "total_uploaded": int(kpi_row["total_uploaded"] or 0),
        "total_published": int(kpi_row["total_published"] or 0),
        "publish_rate": float(kpi_row["publish_rate"] or 0),
        "active_channels": int(kpi_row["active_channels"] or 0),
    }

    # Top channels
    top_channels: list[dict[str, Any]] = []
    try:
        ch_rows = (await db.execute(text(f"""
            SELECT dc.name AS channel,
                   COUNT(*) AS uploaded,
                   SUM(CASE WHEN fv.published THEN 1 ELSE 0 END) AS published,
                   SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)::float
                       / NULLIF(COUNT(*), 0) AS rate
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name
            ORDER BY uploaded DESC
            LIMIT 5
        """), params)).mappings().all()
        for r in ch_rows:
            top_channels.append({
                "channel": r["channel"],
                "uploaded": int(r["uploaded"] or 0),
                "published": int(r["published"] or 0),
                "rate": float(r["rate"] or 0),
            })
    except Exception:
        pass

    # Top content types
    top_content_types: list[dict[str, Any]] = []
    try:
        ct_rows = (await db.execute(text(f"""
            SELECT dit.name, COUNT(*) AS count
            FROM fact_video fv
            JOIN dim_input_type dit ON dit.id = fv.input_type_id
            {where_sql}
            GROUP BY dit.name
            ORDER BY count DESC
            LIMIT 5
        """), params)).mappings().all()
        for r in ct_rows:
            top_content_types.append({
                "name": r["name"],
                "count": int(r["count"] or 0),
            })
    except Exception:
        pass

    return {
        "client_name": cn,
        "kpis": kpis,
        "top_channels": top_channels,
        "top_content_types": top_content_types,
        "period_label": f.date_range if f.date_range != "all" else "All Time",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": "#",
    }
