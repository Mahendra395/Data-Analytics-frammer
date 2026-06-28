"""GET /api/v1/funnel-efficiency/lag — processing and publishing time efficiency metrics."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import (
    ApiResponse,
    LagMetricsRow,
    LagResponse,
    SLABreachResponse,
    SLABreachRow,
)
from app.utils.response import wrap

router = APIRouter(prefix="/lag", tags=["Funnel & Efficiency"])


def _lag_select_cols(prefix: str = "fv") -> str:
    """SQL columns for avg/median/p90 of processing and publishing lag.

    Falls back to a NULL-safe computation if dedicated lag columns are all NULL.
    Uses processing_lag_sec and publishing_lag_sec if populated,
    otherwise derives from timestamps where available.
    """
    return f"""
        COUNT(*) AS cnt,
        ROUND((AVG(COALESCE(
            {prefix}.processing_lag_sec,
            CASE WHEN {prefix}.processed_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.processed_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS avg_proc_lag_min,
        ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY COALESCE(
            {prefix}.processing_lag_sec,
            CASE WHEN {prefix}.processed_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.processed_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS med_proc_lag_min,
        ROUND((PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY COALESCE(
            {prefix}.processing_lag_sec,
            CASE WHEN {prefix}.processed_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.processed_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS p90_proc_lag_min,
        ROUND((AVG(COALESCE(
            {prefix}.publishing_lag_sec,
            CASE WHEN {prefix}.published_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.published_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS avg_pub_lag_min,
        ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY COALESCE(
            {prefix}.publishing_lag_sec,
            CASE WHEN {prefix}.published_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.published_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS med_pub_lag_min,
        ROUND((PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY COALESCE(
            {prefix}.publishing_lag_sec,
            CASE WHEN {prefix}.published_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.published_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS p90_pub_lag_min,
        ROUND((AVG(COALESCE(
            {prefix}.total_cycle_lag_sec,
            CASE WHEN {prefix}.published_at IS NOT NULL AND {prefix}.uploaded_at IS NOT NULL
                 THEN {prefix}.published_at - {prefix}.uploaded_at ELSE NULL END
        )) / 60.0)::numeric, 1)   AS avg_cycle_lag_min
    """


def _row_to_lag(row, segment: Optional[str] = None, segment_type: str = "overall") -> LagMetricsRow:
    def _f(v) -> Optional[float]:
        return float(v) if v is not None else None

    return LagMetricsRow(
        segment=segment,
        segment_type=segment_type,
        avg_processing_lag_min=_f(row["avg_proc_lag_min"]),
        median_processing_lag_min=_f(row["med_proc_lag_min"]),
        p90_processing_lag_min=_f(row["p90_proc_lag_min"]),
        avg_publishing_lag_min=_f(row["avg_pub_lag_min"]),
        median_publishing_lag_min=_f(row["med_pub_lag_min"]),
        p90_publishing_lag_min=_f(row["p90_pub_lag_min"]),
        avg_cycle_lag_min=_f(row["avg_cycle_lag_min"]),
        count=int(row["cnt"] or 0),
    )


@router.get("", response_model=ApiResponse[LagResponse])
async def get_lag(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[LagResponse]:
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    overall_sql = text(f"""
        SELECT {_lag_select_cols()}
        FROM fact_video fv
        {where_sql}
    """)
    overall_row = (await db.execute(overall_sql, params)).mappings().one()
    overall = _row_to_lag(overall_row, segment_type="overall")

    ch_sql = text(f"""
        SELECT dc.name AS seg, {_lag_select_cols()}
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY cnt DESC
        LIMIT 30
    """)
    ch_rows = (await db.execute(ch_sql, params)).mappings().all()
    by_channel = [_row_to_lag(r, segment=r["seg"], segment_type="channel") for r in ch_rows]

    user_sql = text(f"""
        SELECT du.name AS seg, {_lag_select_cols()}
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        {where_sql}
        GROUP BY du.name
        ORDER BY cnt DESC
        LIMIT 30
    """)
    user_rows = (await db.execute(user_sql, params)).mappings().all()
    by_user = [_row_to_lag(r, segment=r["seg"], segment_type="user") for r in user_rows]

    return wrap(
        LagResponse(overall=overall, by_channel=by_channel, by_user=by_user),
        f,
        metrics=["avg_processing_lag_min", "avg_publishing_lag_min"],
        grain="video-level",
        caveats=["Lag metrics fall back to timestamp differences when dedicated lag columns are NULL",
                 "NULL lag values indicate timestamps were not recorded for those rows"],
        unit="minutes",
    )


# ── /lag/sla-breaches ──────────────────────────────────────────────────────────

# Best-effort lag expression: prefer stored column, fall back to timestamp diff
_SLA_LAG_EXPR = """COALESCE(
            fv.total_cycle_lag_sec,
            CASE WHEN fv.published_at IS NOT NULL AND fv.uploaded_at IS NOT NULL
                 THEN fv.published_at - fv.uploaded_at ELSE NULL END,
            fv.processing_lag_sec,
            CASE WHEN fv.processed_at IS NOT NULL AND fv.uploaded_at IS NOT NULL
                 THEN fv.processed_at - fv.uploaded_at ELSE NULL END
        )"""

@router.get("/sla-breaches", response_model=ApiResponse[SLABreachResponse])
async def lag_sla_breaches(
    sla_days: float = Query(default=7.0, ge=0.1,
                            description="SLA threshold in days (default 7)"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SLABreachResponse]:
    """
    SLA breach analysis for the processing→publishing pipeline.
    A breach = published row whose total_cycle_lag_sec / processing_lag_sec
    exceeds the given SLA threshold.
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sla_secs = sla_days * 86400
    params["sla_secs"] = sla_secs

    ov_sql = text(f"""
        SELECT
            COUNT(*) FILTER (WHERE {_SLA_LAG_EXPR} > :sla_secs) AS breach,
            COUNT(*) AS total
        FROM fact_video fv
        {where_sql}
    """)
    ov = (await db.execute(ov_sql, params)).one()
    overall_breach = int(ov.breach or 0)
    overall_total  = int(ov.total or 0)
    overall_breach_pct = round(overall_breach / overall_total * 100, 2) if overall_total else 0.0

    ch_sql = text(f"""
        SELECT
            dc.name AS seg,
            COUNT(*) FILTER (WHERE {_SLA_LAG_EXPR} > :sla_secs) AS breach,
            COUNT(*) AS total,
            ROUND(AVG({_SLA_LAG_EXPR}) / 60.0, 1) AS avg_lag_min
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY breach DESC NULLS LAST
        LIMIT 30
    """)
    ch_rows = (await db.execute(ch_sql, params)).mappings().all()
    by_channel = [
        SLABreachRow(
            segment=r["seg"],
            segment_type="channel",
            breach_count=int(r["breach"] or 0),
            total_count=int(r["total"] or 0),
            breach_pct=round(int(r["breach"] or 0) / int(r["total"] or 1) * 100, 2),
            avg_lag_min=float(r["avg_lag_min"]) if r["avg_lag_min"] is not None else None,
            sla_threshold_days=sla_days,
        )
        for r in ch_rows
    ]

    usr_sql = text(f"""
        SELECT
            du.name AS seg,
            COUNT(*) FILTER (WHERE {_SLA_LAG_EXPR} > :sla_secs) AS breach,
            COUNT(*) AS total,
            ROUND(AVG({_SLA_LAG_EXPR}) / 60.0, 1) AS avg_lag_min
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        {where_sql}
        GROUP BY du.name
        ORDER BY breach DESC NULLS LAST
        LIMIT 30
    """)
    usr_rows = (await db.execute(usr_sql, params)).mappings().all()
    by_user = [
        SLABreachRow(
            segment=r["seg"],
            segment_type="user",
            breach_count=int(r["breach"] or 0),
            total_count=int(r["total"] or 0),
            breach_pct=round(int(r["breach"] or 0) / int(r["total"] or 1) * 100, 2),
            avg_lag_min=float(r["avg_lag_min"]) if r["avg_lag_min"] is not None else None,
            sla_threshold_days=sla_days,
        )
        for r in usr_rows
    ]

    cl_sql = text(f"""
        SELECT
            dcl.name AS seg,
            COUNT(*) FILTER (WHERE {_SLA_LAG_EXPR} > :sla_secs) AS breach,
            COUNT(*) AS total,
            ROUND(AVG({_SLA_LAG_EXPR}) / 60.0, 1) AS avg_lag_min
        FROM fact_video fv
        JOIN dim_client dcl ON dcl.id = fv.client_id
        {where_sql}
        GROUP BY dcl.name
        ORDER BY breach DESC NULLS LAST
        LIMIT 30
    """)
    cl_rows = (await db.execute(cl_sql, params)).mappings().all()
    by_client = [
        SLABreachRow(
            segment=r["seg"],
            segment_type="client",
            breach_count=int(r["breach"] or 0),
            total_count=int(r["total"] or 0),
            breach_pct=round(int(r["breach"] or 0) / int(r["total"] or 1) * 100, 2),
            avg_lag_min=float(r["avg_lag_min"]) if r["avg_lag_min"] is not None else None,
            sla_threshold_days=sla_days,
        )
        for r in cl_rows
    ]

    return wrap(
        SLABreachResponse(
            sla_threshold_days=sla_days,
            overall_breach_count=overall_breach,
            overall_breach_pct=overall_breach_pct,
            by_channel=by_channel,
            by_user=by_user,
            by_client=by_client,
        ),
        f,
        metrics=["avg_processing_lag_min", "avg_publishing_lag_min"],
        grain="video-level",
        caveats=[f"SLA threshold = {sla_days} days; breach uses total_cycle_lag_sec or processing_lag_sec"],
        unit="count",
    )
