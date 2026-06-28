"""Unified health-scoring engine.

Computes per-segment health scores across any dimension (client, channel,
user, team, language, input_type, output_type) with portfolio/peer benchmarks,
percentile ranks, delta vs benchmark, composite health score, and risk level.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from typing import List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams
from app.registry.filters import build_dim_only_where_clause, build_where_clause
from app.schemas.insights import ScoreResponse, ScoreSegment


# ── Helpers ────────────────────────────────────────────────────────────────────

def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    if score >= 20:
        return "D"
    return "F"


def _risk_level(score: float) -> str:
    if score < 30:
        return "critical"
    if score < 60:
        return "warning"
    return "healthy"


def _month_epochs(yr: int, mo: int):
    first = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(yr, mo)[1]
    last = int(datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return first, last


def _prev_month(yr: int, mo: int):
    return (yr - 1, 12) if mo == 1 else (yr, mo - 1)


# ── Dimension configuration ───────────────────────────────────────────────────

_DIM_CONFIG = {
    "client": {
        "join": "JOIN dim_client dcl ON dcl.id = fv.client_id",
        "col": "dcl.name",
        "skip_filter": {"client"},
    },
    "channel": {
        "join": "JOIN dim_channel dc ON dc.id = fv.channel_id",
        "col": "dc.name",
        "skip_filter": {"channel"},
    },
    "user": {
        "join": "JOIN dim_user du ON du.id = fv.user_id",
        "col": "du.name",
        "skip_filter": {"user"},
    },
    "team": {
        "join": "JOIN dim_user du ON du.id = fv.user_id",
        "col": "du.team_name",
        "skip_filter": {"user"},
    },
    "language": {
        "join": "JOIN dim_language dl ON dl.id = fv.language_id",
        "col": "dl.display_name",
        "skip_filter": {"language"},
    },
    "input_type": {
        "join": "JOIN dim_input_type dit ON dit.id = fv.input_type_id",
        "col": "dit.name",
        "skip_filter": set(),
    },
    "output_type": {
        "join": ("LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
                 "JOIN dim_output_type dot ON dot.id = fvot.output_type_id"),
        "col": "dot.name",
        "skip_filter": set(),
    },
}


async def compute_scores(
    db: AsyncSession,
    dimension: str,
    f: FilterParams,
) -> ScoreResponse:
    """Compute health scores for every segment in the given dimension."""
    if dimension not in _DIM_CONFIG:
        dimension = "channel"

    cfg = _DIM_CONFIG[dimension]
    dim_join = cfg["join"]
    dim_col = cfg["col"]

    # Use dimension-excluded filters for peer group (don't filter to only 1 segment)
    peer_where, peer_params = build_dim_only_where_clause(f, exclude_dimensions=cfg["skip_filter"])
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Determine reference month for trend comparison
    ref_yr, ref_mo = await _get_ref_month(db)
    if f.date_to:
        ref_yr, ref_mo = f.date_to.year, f.date_to.month
    prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)
    cur_from, cur_to = _month_epochs(ref_yr, ref_mo)
    prv_from, prv_to = _month_epochs(prev_yr, prev_mo)

    # ── Main query: per-segment metrics ───────────────────────────────────────
    sql = text(f"""
        WITH seg_stats AS (
            SELECT
                {dim_col}                                              AS segment,
                COUNT(fv.id)                                           AS vol,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)         AS pub,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)
                                / COUNT(fv.id) * 100, 1)
                     ELSE 0 END                                        AS conv_pct,
                COALESCE(SUM(fv.uploaded_duration_sec), 0)/3600.0      AS dur_hrs,
                ROUND(COALESCE(AVG(fv.publishing_lag_sec), 0)/60.0, 1) AS avg_lag_min,
                COUNT(*) FILTER (
                    WHERE fv.publishing_lag_sec > 7 * 86400
                )                                                      AS sla_breaches
            FROM fact_video fv
            {dim_join}
            {where_sql}
            GROUP BY {dim_col}
            HAVING {dim_col} IS NOT NULL
        )
        SELECT
            s.segment,
            s.vol,
            s.pub,
            s.conv_pct,
            s.dur_hrs,
            s.avg_lag_min,
            s.sla_breaches,
            PERCENT_RANK() OVER (ORDER BY s.vol)       AS vol_prank,
            PERCENT_RANK() OVER (ORDER BY s.conv_pct)  AS conv_prank,
            AVG(s.vol)       OVER ()                   AS avg_vol,
            AVG(s.conv_pct)  OVER ()                   AS avg_conv
        FROM seg_stats s
        ORDER BY s.vol DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    if not rows:
        return ScoreResponse(
            dimension=dimension, segments=[], portfolio_avg_score=0,
            critical_count=0, warning_count=0, healthy_count=0,
        )

    # ── Previous-period values for trend ──────────────────────────────────────
    prv_kf = f"_score_prv_from_{dimension}"
    prv_kt = f"_score_prv_to_{dimension}"
    peer_w = peer_where + [f"fv.uploaded_at >= :{prv_kf}", f"fv.uploaded_at <= :{prv_kt}"]
    peer_w_sql = "WHERE " + " AND ".join(peer_w)
    prv_params = {**peer_params, prv_kf: prv_from, prv_kt: prv_to}
    prv_sql = text(f"""
        SELECT {dim_col} AS segment, COUNT(fv.id) AS vol
        FROM fact_video fv {dim_join}
        {peer_w_sql}
        GROUP BY {dim_col}
    """)
    prv_rows = {r["segment"]: int(r["vol"] or 0)
                for r in (await db.execute(prv_sql, prv_params)).mappings().all()}

    # ── Build segments ────────────────────────────────────────────────────────
    all_vols = [float(r["vol"]) for r in rows]
    portfolio_avg = sum(all_vols) / len(all_vols) if all_vols else 0
    sorted_vols = sorted(all_vols)
    mid = len(sorted_vols) // 2
    portfolio_median = (
        sorted_vols[mid] if len(sorted_vols) % 2 == 1
        else (sorted_vols[mid - 1] + sorted_vols[mid]) / 2
    ) if sorted_vols else 0

    segments: List[ScoreSegment] = []
    for r in rows:
        vol = float(r["vol"])
        conv = float(r["conv_pct"])
        vol_prank = float(r["vol_prank"] or 0) * 100
        conv_prank = float(r["conv_prank"] or 0) * 100
        avg_lag = float(r["avg_lag_min"] or 0)
        sla_b = int(r["sla_breaches"] or 0)

        # Lag score: lower lag = better, normalize to 0-100
        lag_score = max(0, 100 - min(avg_lag, 100))
        # SLA score: fewer breaches = better
        sla_score = max(0, 100 - min(sla_b * 10, 100))

        # Composite health score
        health = round(
            vol_prank * 0.3 + conv * 0.3 + lag_score * 0.2 + sla_score * 0.2,
            1,
        )

        # Trend
        prv_vol = prv_rows.get(r["segment"], 0)
        if prv_vol > 0:
            trend_delta = round((vol - prv_vol) / prv_vol * 100, 1)
            trend_dir = "up" if trend_delta > 0 else ("down" if trend_delta < 0 else "flat")
        else:
            trend_delta = None
            trend_dir = "new" if vol > 0 else None

        avg_conv = float(r["avg_conv"] or 0)
        peer_avg = float(r["avg_vol"] or 0)

        segments.append(ScoreSegment(
            segment=r["segment"],
            segment_type=dimension,
            value=vol,
            portfolio_avg=round(portfolio_avg, 1),
            peer_avg=round(peer_avg, 1),
            percentile=round(vol_prank, 1),
            delta_vs_benchmark=round(vol - portfolio_avg, 1),
            health_score=health,
            risk_level=_risk_level(health),
            grade=_grade(health),
            volume_rank=round(vol_prank, 1),
            conversion_rate=round(conv, 1),
            lag_score=round(lag_score, 1),
            sla_score=round(sla_score, 1),
            trend_direction=trend_dir,
            trend_delta=trend_delta,
        ))

    critical = sum(1 for s in segments if s.risk_level == "critical")
    warning = sum(1 for s in segments if s.risk_level == "warning")
    healthy = sum(1 for s in segments if s.risk_level == "healthy")

    return ScoreResponse(
        dimension=dimension,
        segments=segments,
        portfolio_avg_score=round(portfolio_avg, 1),
        critical_count=critical,
        warning_count=warning,
        healthy_count=healthy,
    )


async def _get_ref_month(db: AsyncSession) -> tuple[int, int]:
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
