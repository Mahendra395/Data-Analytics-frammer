"""Deep platform analytics endpoints.

GET /content/platforms/mix        → platform × output_type distribution
GET /content/platforms/conversion → publish conversion by platform
GET /content/platforms/duration   → duration share by platform
GET /content/platforms/trend      → monthly platform publishing trend
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse
from app.utils.response import wrap

router = APIRouter(prefix="/platforms", tags=["Platform Deep Analytics"])


@router.get("/mix", response_model=ApiResponse[Dict[str, Any]])
async def platform_mix(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[Dict[str, Any]]:
    """Platform × output type publishing distribution."""
    where, params = build_where_clause(f)
    where_conds = where + ["fv.published = true", "fv.published_platform IS NOT NULL"]
    where_sql = "WHERE " + " AND ".join(where_conds)

    sql = text(f"""
        SELECT
            LOWER(fv.published_platform)                            AS platform,
            dot.name                                                AS output_type,
            COUNT(fv.id)                                            AS published_count,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0      AS duration_hrs
        FROM fact_video fv
        LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id
        LEFT JOIN dim_output_type dot ON dot.id = fvot.output_type_id
        {where_sql}
        GROUP BY LOWER(fv.published_platform), dot.name
        ORDER BY published_count DESC
        LIMIT 200
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    platforms = sorted({r["platform"] for r in rows if r["platform"]})
    output_types = sorted({r["output_type"] for r in rows if r["output_type"]})
    cells = [
        {
            "platform": r["platform"] or "unknown",
            "output_type": r["output_type"] or "unknown",
            "published_count": int(r["published_count"] or 0),
            "duration_hrs": round(float(r["duration_hrs"] or 0), 2),
        }
        for r in rows
    ]

    data = {
        "platforms": platforms,
        "output_types": output_types,
        "cells": cells,
    }
    return wrap(data, f, metrics=["total_published"], grain="segment-aggregated", unit="count")


@router.get("/conversion", response_model=ApiResponse[List[Dict[str, Any]]])
async def platform_conversion(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Publish conversion rate by platform."""
    where, params = build_where_clause(f)
    where_conds = where + ["fv.published_platform IS NOT NULL"]
    where_sql = "WHERE " + " AND ".join(where_conds)

    sql = text(f"""
        WITH plat AS (
            SELECT
                LOWER(fv.published_platform)                        AS platform,
                COUNT(fv.id)                                        AS total,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)      AS published,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)
                                / COUNT(fv.id) * 100, 1)
                     ELSE 0 END                                     AS conversion_pct
            FROM fact_video fv
            {where_sql}
            GROUP BY LOWER(fv.published_platform)
        )
        SELECT p.*, AVG(p.conversion_pct) OVER () AS avg_conv
        FROM plat p
        ORDER BY p.total DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "platform": r["platform"],
            "total": int(r["total"] or 0),
            "published": int(r["published"] or 0),
            "conversion_pct": float(r["conversion_pct"] or 0),
            "portfolio_avg": round(float(r["avg_conv"] or 0), 1),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["publish_rate"], grain="segment-aggregated", unit="percent")


@router.get("/duration", response_model=ApiResponse[List[Dict[str, Any]]])
async def platform_duration(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Duration share by platform (published content)."""
    where, params = build_where_clause(f)
    where_conds = where + ["fv.published = true", "fv.published_platform IS NOT NULL"]
    where_sql = "WHERE " + " AND ".join(where_conds)

    sql = text(f"""
        SELECT
            LOWER(fv.published_platform)                        AS platform,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0  AS duration_hrs,
            COUNT(fv.id)                                        AS count
        FROM fact_video fv
        {where_sql}
        GROUP BY LOWER(fv.published_platform)
        ORDER BY duration_hrs DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    total_hrs = sum(float(r["duration_hrs"] or 0) for r in rows) or 1
    data = [
        {
            "platform": r["platform"],
            "duration_hrs": round(float(r["duration_hrs"] or 0), 2),
            "count": int(r["count"] or 0),
            "share_pct": round(float(r["duration_hrs"] or 0) / total_hrs * 100, 1),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["published_duration_hrs"], grain="segment-aggregated", unit="hours")


@router.get("/trend", response_model=ApiResponse[List[Dict[str, Any]]])
async def platform_trend(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Monthly platform publishing trend."""
    where, params = build_where_clause(f)
    where_conds = where + ["fv.published = true", "fv.published_platform IS NOT NULL",
                           "fv.published_at IS NOT NULL"]
    where_sql = "WHERE " + " AND ".join(where_conds)

    sql = text(f"""
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(fv.published_at))::int  AS year,
            EXTRACT(MONTH FROM to_timestamp(fv.published_at))::int  AS month,
            LOWER(fv.published_platform)                             AS platform,
            COUNT(fv.id)                                             AS count
        FROM fact_video fv
        {where_sql}
        GROUP BY year, month, LOWER(fv.published_platform)
        ORDER BY year, month, count DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    _LABELS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
               7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    data = [
        {
            "year": int(r["year"]),
            "month": int(r["month"]),
            "month_label": f"{_LABELS.get(int(r['month']), '?')} {str(int(r['year']))[2:]}",
            "platform": r["platform"],
            "count": int(r["count"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["total_published"], grain="monthly-aggregated", unit="count")
