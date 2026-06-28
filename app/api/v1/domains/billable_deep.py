"""Deep billable analytics endpoints.

GET /performance/billable/mix        → monthly billable vs non-billable trend
GET /performance/billable/by-segment → billable breakdown by dimension
GET /performance/billable/funnel     → billable funnel (uploaded → published → billable)
GET /performance/billable/waste      → published-but-unbilled waste
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse
from app.utils.response import wrap

router = APIRouter(prefix="/billable", tags=["Billable Deep Analytics"])


@router.get("/mix", response_model=ApiResponse[List[Dict[str, Any]]])
async def billable_mix(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Monthly billable vs non-billable trend."""
    where, params = build_where_clause(f)
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = text(f"""
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(fv.uploaded_at))::int    AS year,
            EXTRACT(MONTH FROM to_timestamp(fv.uploaded_at))::int    AS month,
            SUM(CASE WHEN fv.billable_flag THEN 1 ELSE 0 END)       AS billable_count,
            SUM(CASE WHEN NOT fv.billable_flag THEN 1 ELSE 0 END)   AS non_billable_count,
            COUNT(fv.id)                                            AS total,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND(SUM(CASE WHEN fv.billable_flag THEN 1.0 ELSE 0 END)
                            / COUNT(fv.id) * 100, 1)
                 ELSE 0 END                                         AS billable_pct
        FROM fact_video fv
        {where_sql}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    _LABELS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
               7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    data = [
        {
            "year": int(r["year"]),
            "month": int(r["month"]),
            "month_label": f"{_LABELS.get(int(r['month']), '?')} {str(int(r['year']))[2:]}",
            "billable_count": int(r["billable_count"] or 0),
            "non_billable_count": int(r["non_billable_count"] or 0),
            "total": int(r["total"] or 0),
            "billable_pct": float(r["billable_pct"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["billable_count", "billable_pct"], grain="monthly-aggregated", unit="count")


@router.get("/by-segment", response_model=ApiResponse[List[Dict[str, Any]]])
async def billable_by_segment(
    dimension: str = Query("channel", pattern="^(channel|client|user|language|input_type|output_type)$"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Billable breakdown by a chosen dimension."""
    dim_map = {
        "client": ("dcl.name", "JOIN dim_client dcl ON dcl.id = fv.client_id"),
        "channel": ("dc.name", "JOIN dim_channel dc ON dc.id = fv.channel_id"),
        "user": ("du.name", "JOIN dim_user du ON du.id = fv.user_id"),
        "language": ("dl.display_name", "JOIN dim_language dl ON dl.id = fv.language_id"),
        "input_type": ("dit.name", "JOIN dim_input_type dit ON dit.id = fv.input_type_id"),
        "output_type": ("dot.name",
                        "JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
                        "JOIN dim_output_type dot ON dot.id = fvot.output_type_id"),
    }
    col_expr, join_clause = dim_map[dimension]

    where, params = build_where_clause(f)
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = text(f"""
        SELECT
            {col_expr}                                              AS segment,
            SUM(CASE WHEN fv.billable_flag THEN 1 ELSE 0 END)      AS billable,
            SUM(CASE WHEN NOT fv.billable_flag THEN 1 ELSE 0 END)  AS non_billable,
            COUNT(fv.id)                                            AS total,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND(SUM(CASE WHEN fv.billable_flag THEN 1.0 ELSE 0 END)
                            / COUNT(fv.id) * 100, 1)
                 ELSE 0 END                                         AS billable_pct
        FROM fact_video fv
        {join_clause}
        {where_sql}
        GROUP BY {col_expr}
        ORDER BY total DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "segment": r["segment"],
            "billable": int(r["billable"] or 0),
            "non_billable": int(r["non_billable"] or 0),
            "total": int(r["total"] or 0),
            "billable_pct": float(r["billable_pct"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["billable_count", "billable_pct"],
                grain="segment-aggregated", unit="count")


@router.get("/funnel", response_model=ApiResponse[Dict[str, Any]])
async def billable_funnel(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[Dict[str, Any]]:
    """Billable funnel: uploaded → published → billable."""
    where, params = build_where_clause(f)
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = text(f"""
        SELECT
            COUNT(fv.id)                                            AS uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)          AS published,
            SUM(CASE WHEN fv.billable_flag THEN 1 ELSE 0 END)      AS billable
        FROM fact_video fv
        {where_sql}
    """)
    row = (await db.execute(sql, params)).mappings().first()
    uploaded = int(row["uploaded"] or 0)
    published = int(row["published"] or 0)
    billable = int(row["billable"] or 0)

    data = {
        "uploaded": uploaded,
        "published": published,
        "billable": billable,
        "publish_rate": round(published / uploaded * 100, 1) if uploaded else 0,
        "billable_rate": round(billable / uploaded * 100, 1) if uploaded else 0,
        "billable_of_published": round(billable / published * 100, 1) if published else 0,
    }
    return wrap(data, f, metrics=["publish_rate", "billable_rate"],
                grain="single-aggregate", unit="percent")


@router.get("/waste", response_model=ApiResponse[List[Dict[str, Any]]])
async def billable_waste(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Published-but-unbilled content grouped by channel."""
    where, params = build_where_clause(f)
    where_conds = where + ["fv.published = true", "fv.billable_flag = false"]
    where_sql = "WHERE " + " AND ".join(where_conds)

    sql = text(f"""
        SELECT
            dc.name                                                     AS channel,
            COUNT(fv.id)                                                AS waste_count,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0          AS waste_hrs
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY waste_count DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "channel": r["channel"],
            "waste_count": int(r["waste_count"] or 0),
            "waste_hrs": round(float(r["waste_hrs"] or 0), 2),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["waste_count", "waste_hrs"],
                grain="segment-aggregated", unit="count")
