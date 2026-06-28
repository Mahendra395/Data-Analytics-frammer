"""GET /api/v1/performance/analytics — channel health scores and user productivity."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import (
    ApiResponse,
    ChannelHealthRow,
    UserProductivityRow,
)
from app.utils.response import wrap

router = APIRouter(prefix="/analytics", tags=["Performance"])


@router.get("/channel-health", response_model=ApiResponse[List[ChannelHealthRow]])
async def channel_health(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[ChannelHealthRow]]:
    """
    Channel health quadrant:
      - star:             high volume (>= median) AND high conversion (>= median)
      - high_volume:      high volume, low conversion
      - high_efficiency:  low volume, high conversion
      - underperforming:  low volume, low conversion
    Health score 0-100 = 0.5 * (volume_percentile) + 0.5 * (conversion_pct)
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        WITH ch_stats AS (
            SELECT
                dc.name                                                        AS channel,
                dc.obfuscated_code,
                COUNT(fv.id)                                                   AS total_uploaded,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                 AS total_published,
                CASE WHEN COUNT(fv.id) > 0
                     THEN ROUND(SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)::numeric / COUNT(fv.id) * 100, 1)
                     ELSE 0 END                                                AS pub_pct,
                CASE WHEN COUNT(fv.id) > 0
                     THEN COALESCE(SUM(fv.uploaded_duration_sec),0)/COUNT(fv.id)/60.0
                     ELSE 0 END                                                AS avg_dur_min
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name, dc.obfuscated_code
        ),
        medians AS (
            SELECT
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_uploaded) AS med_vol,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pub_pct)        AS med_conv
            FROM ch_stats
        )
        SELECT
            ch.channel,
            ch.obfuscated_code,
            ch.total_uploaded,
            ch.total_published,
            ch.pub_pct,
            ch.avg_dur_min,
            (ch.total_uploaded - ch.total_published)  AS processed_not_published,
            m.med_vol,
            m.med_conv,
            ROUND(
                (PERCENT_RANK() OVER (ORDER BY ch.total_uploaded) * 50
               + ch.pub_pct * 0.5)::numeric, 1
            ) AS health_score
        FROM ch_stats ch, medians m
        ORDER BY health_score DESC
    """)

    rows = (await db.execute(sql, params)).mappings().all()

    result: List[ChannelHealthRow] = []
    for r in rows:
        vol  = int(r["total_uploaded"] or 0)
        conv = float(r["pub_pct"] or 0)
        med_vol  = float(r["med_vol"]  or 0)
        med_conv = float(r["med_conv"] or 0)

        if vol >= med_vol and conv >= med_conv:
            quadrant = "star"
        elif vol >= med_vol and conv < med_conv:
            quadrant = "high_volume"
        elif vol < med_vol and conv >= med_conv:
            quadrant = "high_efficiency"
        else:
            quadrant = "underperforming"

        result.append(
            ChannelHealthRow(
                channel=r["channel"],
                obfuscated_code=r.get("obfuscated_code"),
                total_uploaded=vol,
                total_published=int(r["total_published"] or 0),
                publish_conversion_pct=conv,
                avg_duration_min=round(float(r["avg_dur_min"] or 0), 1),
                processed_not_published=int(r["processed_not_published"] or 0),
                health_quadrant=quadrant,
                health_score=round(float(r["health_score"] or 0), 1),
            )
        )
    return wrap(result, f,
                metrics=["total_uploaded", "total_published", "publish_rate"],
                grain="segment-aggregated",
                caveats=["Health quadrant thresholds use the median of the filtered dataset; "
                         "they shift as filters change"],
                unit=None)


@router.get("/user-productivity", response_model=ApiResponse[List[UserProductivityRow]])
async def user_productivity(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[UserProductivityRow]]:
    """
    User Productivity Index (0-100):
      40% of volume percentile rank + 30% of conversion % + 30% of consistency
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            du.name                                                         AS "user",
            du.team_name,
            COUNT(fv.id)                                                    AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                  AS total_published,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND((SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END) / COUNT(fv.id) * 100)::numeric, 1)
                 ELSE 0 END                                                 AS pub_pct,
            COALESCE(SUM(fv.uploaded_duration_sec),  0) / 3600.0           AS uploaded_hrs,
            CASE WHEN COUNT(fv.id) > 0
                 THEN COALESCE(SUM(fv.uploaded_duration_sec),0)/COUNT(fv.id)/60.0
                 ELSE 0 END                                                 AS avg_dur_min,
            COUNT(DISTINCT
                EXTRACT(MONTH FROM to_timestamp(fv.uploaded_at))::int
            )                                                               AS active_months
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        {where_sql}
        GROUP BY du.name, du.team_name
        ORDER BY total_uploaded DESC
    """)

    rows = (await db.execute(sql, params)).mappings().all()

    if not rows:
        return wrap([], f, metrics=["total_uploaded", "total_published", "publish_rate"],
                    grain="segment-aggregated", unit=None)

    max_vol = max(int(r["total_uploaded"] or 0) for r in rows) or 1

    result: List[UserProductivityRow] = []
    for r in rows:
        vol      = int(r["total_uploaded"] or 0)
        conv     = float(r["pub_pct"] or 0)
        months   = min(12, int(r["active_months"] or 0))
        vol_rank = vol / max_vol * 100
        consistency = months / 12 * 100
        productivity = round(vol_rank * 0.4 + conv * 0.3 + consistency * 0.3, 1)

        result.append(
            UserProductivityRow(
                user=r["user"],
                team_name=r.get("team_name"),
                total_uploaded=vol,
                total_published=int(r["total_published"] or 0),
                publish_conversion_pct=conv,
                uploaded_duration_hrs=round(float(r["uploaded_hrs"] or 0), 2),
                avg_duration_min=round(float(r["avg_dur_min"] or 0), 1),
                productivity_index=productivity,
            )
        )
    return wrap(result, f,
                metrics=["total_uploaded", "total_published", "publish_rate"],
                grain="segment-aggregated",
                caveats=["Productivity index = 40% volume rank + 30% publish rate + 30% monthly consistency; "
                         "weights are fixed and not configurable"],
                unit=None)
