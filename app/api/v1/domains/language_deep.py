"""Deep language analytics endpoints.

GET /content/languages/matrix       → language × (output_type|channel) cross-tab
GET /content/languages/lag          → processing & publishing lag by language
GET /content/languages/conversion   → publish conversion by language with benchmark
GET /content/languages/underperforming → language×channel combos below portfolio avg
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

router = APIRouter(prefix="/languages", tags=["Language Deep Analytics"])


@router.get("/matrix", response_model=ApiResponse[Dict[str, Any]])
async def language_matrix(
    cross: str = Query(default="output_type", description="output_type | channel"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[Dict[str, Any]]:
    """Language × cross-dimension matrix (uploads, published, conversion)."""
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if cross == "channel":
        cross_join = "JOIN dim_channel dc ON dc.id = fv.channel_id"
        cross_col = "dc.name"
    else:
        cross = "output_type"
        cross_join = (
            "LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
            "JOIN dim_output_type dot ON dot.id = fvot.output_type_id"
        )
        cross_col = "dot.name"

    sql = text(f"""
        SELECT
            dl.display_name                                        AS language,
            {cross_col}                                            AS cross_dim,
            COUNT(fv.id)                                           AS uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)         AS published,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)
                            / COUNT(fv.id) * 100, 1)
                 ELSE 0 END                                        AS conversion_pct
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        {cross_join}
        {where_sql}
        GROUP BY dl.display_name, {cross_col}
        HAVING dl.display_name IS NOT NULL AND {cross_col} IS NOT NULL
        ORDER BY uploaded DESC
        LIMIT 200
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    languages = sorted({r["language"] for r in rows})
    cross_values = sorted({r["cross_dim"] for r in rows})
    cells = [
        {
            "language": r["language"],
            "cross_dim": r["cross_dim"],
            "uploaded": int(r["uploaded"] or 0),
            "published": int(r["published"] or 0),
            "conversion_pct": float(r["conversion_pct"] or 0),
        }
        for r in rows
    ]

    data = {
        "cross_dimension": cross,
        "languages": languages,
        "cross_values": cross_values,
        "cells": cells,
    }
    return wrap(data, f, metrics=["total_uploaded", "total_published", "publish_rate"],
                grain="segment-aggregated", unit="count")


@router.get("/lag", response_model=ApiResponse[List[Dict[str, Any]]])
async def language_lag(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Processing and publishing lag by language."""
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dl.display_name                                              AS language,
            COUNT(fv.id)                                                 AS total,
            ROUND(AVG(fv.processing_lag_sec) / 60.0, 1)                 AS avg_proc_lag_min,
            ROUND(AVG(fv.publishing_lag_sec) / 60.0, 1)                 AS avg_pub_lag_min,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fv.processing_lag_sec) / 60.0, 1)
                                                                         AS median_proc_lag_min,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fv.publishing_lag_sec) / 60.0, 1)
                                                                         AS median_pub_lag_min
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        {where_sql}
        GROUP BY dl.display_name
        HAVING dl.display_name IS NOT NULL
        ORDER BY avg_pub_lag_min DESC NULLS LAST
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "language": r["language"],
            "total": int(r["total"] or 0),
            "avg_processing_lag_min": float(r["avg_proc_lag_min"] or 0),
            "avg_publishing_lag_min": float(r["avg_pub_lag_min"] or 0),
            "median_processing_lag_min": float(r["median_proc_lag_min"] or 0),
            "median_publishing_lag_min": float(r["median_pub_lag_min"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["avg_processing_lag_min", "avg_publishing_lag_min"],
                grain="segment-aggregated", unit="minutes")


@router.get("/conversion", response_model=ApiResponse[List[Dict[str, Any]]])
async def language_conversion(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Publish conversion by language with portfolio benchmark."""
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        WITH lang_stats AS (
            SELECT
                dl.display_name AS language,
                COUNT(fv.id) AS uploaded,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END) AS published,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)/COUNT(fv.id)*100,1)
                     ELSE 0 END AS conversion_pct
            FROM fact_video fv
            JOIN dim_language dl ON dl.id = fv.language_id
            {where_sql}
            GROUP BY dl.display_name
            HAVING dl.display_name IS NOT NULL
        )
        SELECT
            l.language, l.uploaded, l.published, l.conversion_pct,
            AVG(l.conversion_pct) OVER () AS portfolio_avg_conv,
            PERCENT_RANK() OVER (ORDER BY l.conversion_pct) * 100 AS percentile
        FROM lang_stats l
        ORDER BY l.uploaded DESC
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "language": r["language"],
            "uploaded": int(r["uploaded"] or 0),
            "published": int(r["published"] or 0),
            "conversion_pct": float(r["conversion_pct"] or 0),
            "portfolio_avg_conversion": round(float(r["portfolio_avg_conv"] or 0), 1),
            "delta_vs_benchmark": round(float(r["conversion_pct"] or 0) - float(r["portfolio_avg_conv"] or 0), 1),
            "percentile": round(float(r["percentile"] or 0), 1),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["publish_rate"], grain="segment-aggregated", unit="percent")


@router.get("/underperforming", response_model=ApiResponse[List[Dict[str, Any]]])
async def language_underperforming(
    min_volume: int = Query(default=5, description="Minimum upload volume to consider"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[Dict[str, Any]]]:
    """Language × channel combos where conversion is below portfolio average."""
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        WITH combo AS (
            SELECT
                dl.display_name AS language,
                dc.name AS channel,
                COUNT(fv.id) AS uploaded,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END) AS published,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)/COUNT(fv.id)*100,1)
                     ELSE 0 END AS conversion_pct
            FROM fact_video fv
            JOIN dim_language dl ON dl.id = fv.language_id
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dl.display_name, dc.name
            HAVING dl.display_name IS NOT NULL AND dc.name IS NOT NULL
               AND COUNT(fv.id) >= :min_vol
        ),
        portfolio AS (
            SELECT AVG(conversion_pct) AS avg_conv FROM combo
        )
        SELECT c.language, c.channel, c.uploaded, c.published, c.conversion_pct,
               p.avg_conv AS portfolio_avg,
               ROUND(c.conversion_pct - p.avg_conv, 1) AS delta
        FROM combo c, portfolio p
        WHERE c.conversion_pct < p.avg_conv
        ORDER BY c.uploaded DESC
        LIMIT 50
    """)
    params["min_vol"] = min_volume
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "language": r["language"],
            "channel": r["channel"],
            "uploaded": int(r["uploaded"] or 0),
            "published": int(r["published"] or 0),
            "conversion_pct": float(r["conversion_pct"] or 0),
            "portfolio_avg": round(float(r["portfolio_avg"] or 0), 1),
            "delta_vs_avg": float(r["delta"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["publish_rate"], grain="segment-aggregated", unit="percent",
                caveats=[f"Only language×channel combos with >= {min_volume} uploads included"])
