"""Insight engine: collects analytics signals and assembles InsightContext.

This service queries the database directly (reusing filter/SQL patterns from
existing endpoints) to build a comprehensive analytics snapshot used by the
LLM insight generator or the deterministic fallback.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams
from app.registry.filters import build_dim_only_where_clause, build_where_clause


# ── Epoch helpers (mirrors growth.py) ──────────────────────────────────────────

def _month_epochs(yr: int, mo: int) -> tuple[int, int]:
    first = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(yr, mo)[1]
    last = int(datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return first, last


def _prev_month(yr: int, mo: int) -> tuple[int, int]:
    return (yr - 1, 12) if mo == 1 else (yr, mo - 1)


# ── InsightContext type ────────────────────────────────────────────────────────

InsightContext = Dict[str, Any]


async def collect_insight_context(
    db: AsyncSession,
    f: FilterParams,
) -> InsightContext:
    """Assemble a comprehensive analytics snapshot for insight generation.

    Returns a dict with keys: kpis, funnel, channel_health, growth_drivers,
    dq_summary, lag_summary, concentration, low_conversion_segments.
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    dim_where, dim_params = build_dim_only_where_clause(f)

    ctx: InsightContext = {}

    # ── 1. Core KPIs ──────────────────────────────────────────────────────────
    kpi_sql = text(f"""
        SELECT
            COUNT(*)                                         AS total_uploaded,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)      AS total_published,
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)   AS total_processed,
            COALESCE(SUM(uploaded_duration_sec), 0)/3600.0   AS uploaded_hrs,
            COALESCE(SUM(published_duration_sec), 0)/3600.0  AS published_hrs,
            COUNT(DISTINCT channel_id)                       AS active_channels,
            COUNT(DISTINCT user_id)                          AS active_users,
            COUNT(DISTINCT client_id)                        AS active_clients
        FROM fact_video fv
        {where_sql}
    """)
    row = (await db.execute(kpi_sql, params)).mappings().one()
    total = int(row["total_uploaded"] or 0)
    total_pub = int(row["total_published"] or 0)
    total_proc = int(row["total_processed"] or 0)
    ctx["kpis"] = {
        "total_uploaded": total,
        "total_published": total_pub,
        "total_processed": total_proc,
        "publish_rate": round(total_pub / total * 100, 1) if total else 0,
        "processing_rate": round(total_proc / total * 100, 1) if total else 0,
        "uploaded_hrs": round(float(row["uploaded_hrs"] or 0), 2),
        "published_hrs": round(float(row["published_hrs"] or 0), 2),
        "active_channels": int(row["active_channels"] or 0),
        "active_users": int(row["active_users"] or 0),
        "active_clients": int(row["active_clients"] or 0),
    }

    # ── 2. Funnel ─────────────────────────────────────────────────────────────
    ctx["funnel"] = {
        "uploaded": total,
        "processed": total_proc,
        "published": total_pub,
        "upload_to_processed_pct": round(total_proc / total * 100, 1) if total else 0,
        "processed_to_published_pct": round(total_pub / total_proc * 100, 1) if total_proc else 0,
        "upload_to_published_pct": round(total_pub / total * 100, 1) if total else 0,
        "publish_gap": max(0, total_proc - total_pub),
    }

    # ── 3. Channel health (top performers + underperformers) ──────────────────
    ch_sql = text(f"""
        WITH ch AS (
            SELECT
                dc.name AS channel,
                COUNT(fv.id) AS vol,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END) AS pub,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)/COUNT(fv.id)*100,1)
                     ELSE 0 END AS conv
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name
            HAVING COUNT(fv.id) >= 5
        )
        SELECT channel, vol, pub, conv FROM ch ORDER BY conv ASC
    """)
    ch_rows = (await db.execute(ch_sql, params)).mappings().all()
    ctx["channel_health"] = [
        {"channel": r["channel"], "volume": int(r["vol"]), "published": int(r["pub"]),
         "conversion_pct": float(r["conv"])}
        for r in ch_rows
    ]

    # ── 4. Growth drivers (MoM by channel) ────────────────────────────────────
    ref_yr, ref_mo = await _get_ref_month(db, f)
    prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)
    ctx["growth_drivers"] = await _growth_drivers_multi(
        db, dim_where, dim_params, ref_yr, ref_mo, prev_yr, prev_mo,
    )
    ctx["ref_period"] = f"{ref_yr}-{ref_mo:02d}"
    ctx["prev_period"] = f"{prev_yr}-{prev_mo:02d}"

    # ── 5. DQ summary ─────────────────────────────────────────────────────────
    dq_columns = [
        "video_id", "headline", "source_url", "channel_id", "user_id",
        "language_id", "input_type_id", "uploaded_at", "published_platform",
        "published_url", "uploaded_duration_sec", "created_duration_sec",
    ]
    col_exprs = ", ".join(
        f"SUM(CASE WHEN {col} IS NULL OR {col}::text = '' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) AS np_{i}"
        for i, col in enumerate(dq_columns)
    )
    dq_sql = text(f"SELECT COUNT(*) AS total, {col_exprs} FROM fact_video fv {where_sql}")
    try:
        dq_row = (await db.execute(dq_sql, params)).mappings().one()
        score_total = sum(
            max(0.0, 100.0 - float(dq_row[f"np_{i}"] or 0) * 100)
            for i in range(len(dq_columns))
        )
        dq_score = round(score_total / len(dq_columns), 1)
        worst_fields = []
        for i, col in enumerate(dq_columns):
            null_pct = round(float(dq_row[f"np_{i}"] or 0) * 100, 1)
            if null_pct > 5:
                worst_fields.append({"field": col, "null_pct": null_pct})
        worst_fields.sort(key=lambda x: x["null_pct"], reverse=True)
    except Exception:
        dq_score = 0.0
        worst_fields = []

    ctx["dq_summary"] = {
        "overall_score": dq_score,
        "worst_fields": worst_fields[:5],
    }

    # ── 6. Lag / SLA summary ──────────────────────────────────────────────────
    lag_sql = text(f"""
        SELECT
            ROUND(AVG(processing_lag_sec) / 60.0, 1) AS avg_proc_lag_min,
            ROUND(AVG(publishing_lag_sec) / 60.0, 1) AS avg_pub_lag_min,
            COUNT(*) FILTER (WHERE publishing_lag_sec > 7 * 86400) AS sla_breaches,
            COUNT(*) FILTER (WHERE NOT is_processed AND NOT published) AS backlog
        FROM fact_video fv
        {where_sql}
    """)
    try:
        lag_row = (await db.execute(lag_sql, params)).mappings().one()
        ctx["lag_summary"] = {
            "avg_processing_lag_min": float(lag_row["avg_proc_lag_min"] or 0),
            "avg_publishing_lag_min": float(lag_row["avg_pub_lag_min"] or 0),
            "sla_breaches": int(lag_row["sla_breaches"] or 0),
            "backlog_count": int(lag_row["backlog"] or 0),
        }
    except Exception:
        ctx["lag_summary"] = {
            "avg_processing_lag_min": 0, "avg_publishing_lag_min": 0,
            "sla_breaches": 0, "backlog_count": 0,
        }

    # ── 7. Low-conversion segment combos (language × channel) ─────────────────
    low_conv_sql = text(f"""
        SELECT
            dl.display_name AS language,
            dc.name AS channel,
            COUNT(fv.id) AS vol,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)/COUNT(fv.id)*100,1)
                 ELSE 0 END AS conv
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dl.display_name, dc.name
        HAVING COUNT(fv.id) >= 5
        ORDER BY conv ASC
        LIMIT 10
    """)
    try:
        low_rows = (await db.execute(low_conv_sql, params)).mappings().all()
        ctx["low_conversion_segments"] = [
            {"language": r["language"], "channel": r["channel"],
             "volume": int(r["vol"]), "conversion_pct": float(r["conv"])}
            for r in low_rows
        ]
    except Exception:
        ctx["low_conversion_segments"] = []

    # ── 8. Concentration (top 5 channels share) ──────────────────────────────
    if total > 0 and ctx["channel_health"]:
        top5_vol = sum(ch["volume"] for ch in sorted(ctx["channel_health"], key=lambda x: -x["volume"])[:5])
        ctx["concentration"] = {
            "top_5_channel_share_pct": round(top5_vol / total * 100, 1),
        }
    else:
        ctx["concentration"] = {"top_5_channel_share_pct": 0}

    return ctx


async def _get_ref_month(db: AsyncSession, f: FilterParams) -> tuple[int, int]:
    """Determine the reference year/month for growth analysis."""
    if f.date_to:
        return f.date_to.year, f.date_to.month
    latest = (await db.execute(text("""
        SELECT EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int AS yr,
               EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo
        FROM fact_video WHERE uploaded_at IS NOT NULL ORDER BY uploaded_at DESC LIMIT 1
    """))).mappings().first()
    if latest:
        return int(latest["yr"]), int(latest["mo"])
    from datetime import date
    today = date.today()
    return today.year, today.month


async def _growth_drivers_multi(
    db: AsyncSession,
    dim_where: List[str],
    dim_params: dict,
    ref_yr: int, ref_mo: int,
    prev_yr: int, prev_mo: int,
) -> Dict[str, Any]:
    """Compute growth drivers for uploaded AND published across channels."""
    cur_from, cur_to = _month_epochs(ref_yr, ref_mo)
    prv_from, prv_to = _month_epochs(prev_yr, prev_mo)

    results: Dict[str, Any] = {}
    for metric_key, metric_expr in [
        ("uploaded", "COUNT(fv.id)"),
        ("published", "SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)"),
    ]:
        for dim_key, dim_join, dim_col in [
            ("channel", "JOIN dim_channel dc ON dc.id = fv.channel_id", "dc.name"),
            ("language", "JOIN dim_language dl ON dl.id = fv.language_id", "dl.display_name"),
        ]:
            def _period_sql(ep_from: int, ep_to: int):
                kf = f"_ep_from_{metric_key}_{dim_key}_{ep_from}"
                kt = f"_ep_to_{metric_key}_{dim_key}_{ep_to}"
                w = dim_where + [f"fv.uploaded_at >= :{kf}", f"fv.uploaded_at <= :{kt}"]
                return (
                    f"SELECT {dim_col} AS seg, {metric_expr} AS val "
                    f"FROM fact_video fv {dim_join} "
                    f"WHERE {' AND '.join(w)} "
                    f"GROUP BY {dim_col}",
                    {**dim_params, kf: ep_from, kt: ep_to},
                )

            cur_qsql, cur_p = _period_sql(cur_from, cur_to)
            prv_qsql, prv_p = _period_sql(prv_from, prv_to)

            cur_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(text(cur_qsql), cur_p)).mappings().all()}
            prv_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(text(prv_qsql), prv_p)).mappings().all()}

            all_segs = set(cur_rows) | set(prv_rows)
            drivers = []
            for seg in all_segs:
                cur_v = cur_rows.get(seg, 0)
                prv_v = prv_rows.get(seg, 0)
                drivers.append({"segment": seg, "current": cur_v, "previous": prv_v, "delta": cur_v - prv_v})
            drivers.sort(key=lambda x: abs(x["delta"]), reverse=True)
            total_abs = sum(abs(d["delta"]) for d in drivers) or 1
            for d in drivers:
                d["share"] = round(abs(d["delta"]) / total_abs, 4)

            results[f"{metric_key}_by_{dim_key}"] = {
                "total_delta": sum(d["delta"] for d in drivers),
                "drivers": drivers[:10],
            }

    return results
