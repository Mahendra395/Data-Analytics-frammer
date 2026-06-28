"""GET /api/v1/publishing — channel × platform publishing matrix."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, PublishingPlatformCount, PublishingPlatformDuration
from app.services.aggregate_support import supports_publishing_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/publishing", tags=["Publishing"])

_PLATFORMS = ["facebook", "instagram", "linkedin", "reels", "shorts", "x", "youtube", "threads"]


@router.get("/by-channel", response_model=ApiResponse[List[PublishingPlatformCount]])
async def publishing_by_channel(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[PublishingPlatformCount]]:
    """
    Returns the channel × platform count matrix from channel-wise-publishing.csv data
    (loaded into the agg_channel_publishing table during ingest).
    Falls back to grouping fact_video.published_platform if the agg table is empty.
    """
    if supports_publishing_aggregate(f):
        clauses = []
        params: dict[str, str] = {}
        if f.channel:
            clauses.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
            params["channel"] = f.channel
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = text(f"""
            SELECT
                dc.name AS channel,
                acp.facebook,
                acp.instagram,
                acp.linkedin,
                acp.reels,
                acp.shorts,
                acp.x,
                acp.youtube,
                acp.threads,
                (
                    acp.facebook + acp.instagram + acp.linkedin + acp.reels +
                    acp.shorts + acp.x + acp.youtube + acp.threads
                ) AS total
            FROM agg_channel_publishing acp
            JOIN dim_channel dc ON dc.id = acp.channel_id
            {where_sql}
            ORDER BY total DESC, dc.name
        """)

        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            PublishingPlatformCount(
                channel=r["channel"],
                facebook=int(r["facebook"] or 0),
                instagram=int(r["instagram"] or 0),
                linkedin=int(r["linkedin"] or 0),
                reels=int(r["reels"] or 0),
                shorts=int(r["shorts"] or 0),
                x=int(r["x"] or 0),
                youtube=int(r["youtube"] or 0),
                threads=int(r["threads"] or 0),
                total=int(r["total"] or 0),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_published"],
            grain="segment-aggregated",
            caveats=["Publishing counts come from the modified aggregate tables"],
            unit="count",
        )

    where_clauses, params = build_where_clause(f)
    if f.published_flag is None:
        where_clauses.append("fv.published = TRUE")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = text(f"""
        SELECT
            dc.name                                                        AS channel,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'facebook'  THEN 1 ELSE 0 END), 0) AS facebook,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'instagram' THEN 1 ELSE 0 END), 0) AS instagram,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'linkedin'  THEN 1 ELSE 0 END), 0) AS linkedin,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'reels'     THEN 1 ELSE 0 END), 0) AS reels,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'shorts'    THEN 1 ELSE 0 END), 0) AS shorts,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'x'         THEN 1 ELSE 0 END), 0) AS x,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'youtube'   THEN 1 ELSE 0 END), 0) AS youtube,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'threads'   THEN 1 ELSE 0 END), 0) AS threads,
            COUNT(CASE WHEN fv.published THEN 1 END)                       AS total
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY total DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        PublishingPlatformCount(
            channel=r["channel"],
            facebook=int(r["facebook"] or 0),
            instagram=int(r["instagram"] or 0),
            linkedin=int(r["linkedin"] or 0),
            reels=int(r["reels"] or 0),
            shorts=int(r["shorts"] or 0),
            x=int(r["x"] or 0),
            youtube=int(r["youtube"] or 0),
            threads=int(r["threads"] or 0),
            total=int(r["total"] or 0),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_published"],
                grain="segment-aggregated",
                caveats=["Defaults to published=TRUE rows when publishedFlag filter is not set"],
                unit="count")


@router.get("/by-channel/duration", response_model=ApiResponse[List[PublishingPlatformDuration]])
async def publishing_duration_by_channel(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[PublishingPlatformDuration]]:
    """Duration published per channel per platform (in hours)."""
    if supports_publishing_aggregate(f):
        clauses = []
        params: dict[str, str] = {}
        if f.channel:
            clauses.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
            params["channel"] = f.channel
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = text(f"""
            SELECT
                dc.name AS channel,
                acpd.facebook_duration_sec / 3600.0 AS facebook_hrs,
                acpd.instagram_duration_sec / 3600.0 AS instagram_hrs,
                acpd.linkedin_duration_sec / 3600.0 AS linkedin_hrs,
                acpd.reels_duration_sec / 3600.0 AS reels_hrs,
                acpd.shorts_duration_sec / 3600.0 AS shorts_hrs,
                acpd.x_duration_sec / 3600.0 AS x_hrs,
                acpd.youtube_duration_sec / 3600.0 AS youtube_hrs,
                acpd.threads_duration_sec / 3600.0 AS threads_hrs
            FROM agg_channel_publishing_duration acpd
            JOIN dim_channel dc ON dc.id = acpd.channel_id
            {where_sql}
            ORDER BY dc.name
        """)

        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            PublishingPlatformDuration(
                channel=r["channel"],
                facebook_hrs=round(float(r["facebook_hrs"] or 0), 3),
                instagram_hrs=round(float(r["instagram_hrs"] or 0), 3),
                linkedin_hrs=round(float(r["linkedin_hrs"] or 0), 3),
                reels_hrs=round(float(r["reels_hrs"] or 0), 3),
                shorts_hrs=round(float(r["shorts_hrs"] or 0), 3),
                x_hrs=round(float(r["x_hrs"] or 0), 3),
                youtube_hrs=round(float(r["youtube_hrs"] or 0), 3),
                threads_hrs=round(float(r["threads_hrs"] or 0), 3),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_published_duration_hrs"],
            grain="segment-aggregated",
            caveats=["Publishing durations come from the modified aggregate tables"],
            unit="hours",
        )

    where_clauses, params = build_where_clause(f)
    if f.published_flag is None:
        where_clauses.append("fv.published = TRUE")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = text("""
        SELECT
            dc.name AS channel,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'facebook'  THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS facebook_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'instagram' THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS instagram_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'linkedin'  THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS linkedin_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'reels'     THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS reels_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'shorts'    THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS shorts_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'x'         THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS x_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'youtube'   THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS youtube_hrs,
            COALESCE(SUM(CASE WHEN LOWER(fv.published_platform) = 'threads'   THEN fv.published_duration_sec ELSE 0 END), 0)/3600.0 AS threads_hrs
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY dc.name
    """.format(where_sql=where_sql))

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        PublishingPlatformDuration(
            channel=r["channel"],
            facebook_hrs=round(float(r["facebook_hrs"] or 0), 3),
            instagram_hrs=round(float(r["instagram_hrs"] or 0), 3),
            linkedin_hrs=round(float(r["linkedin_hrs"] or 0), 3),
            reels_hrs=round(float(r["reels_hrs"] or 0), 3),
            shorts_hrs=round(float(r["shorts_hrs"] or 0), 3),
            x_hrs=round(float(r["x_hrs"] or 0), 3),
            youtube_hrs=round(float(r["youtube_hrs"] or 0), 3),
            threads_hrs=round(float(r["threads_hrs"] or 0), 3),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_published_duration_hrs"],
                grain="segment-aggregated",
                caveats=["Defaults to published=TRUE rows when publishedFlag filter is not set"],
                unit="hours")
