"""GET /api/v1/channels — channel-level aggregations."""

from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, ChannelRow, ChannelUserRow
from app.services.aggregate_support import supports_channel_aggregate, supports_channel_user_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/channels", tags=["Channels"])


@router.get("", response_model=ApiResponse[List[ChannelRow]])
async def get_channels(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[ChannelRow]]:
    if supports_channel_aggregate(f):
        clauses = []
        params: dict[str, str] = {}
        if f.channel:
            clauses.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
            params["channel"] = f.channel
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = text(f"""
            SELECT
                dc.name AS channel,
                dc.obfuscated_code AS obfuscated_code,
                acs.uploaded_count AS total_uploaded,
                acs.created_count AS total_created,
                acs.published_count AS total_published,
                acs.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                acs.created_duration_sec / 3600.0 AS created_duration_hrs,
                acs.published_duration_sec / 3600.0 AS published_duration_hrs,
                CASE WHEN acs.uploaded_count > 0
                     THEN acs.uploaded_duration_sec / acs.uploaded_count / 60.0
                     ELSE 0 END AS avg_duration_min
            FROM agg_channel_stat acs
            JOIN dim_channel dc ON dc.id = acs.channel_id
            {where_sql}
            ORDER BY acs.uploaded_count DESC, dc.name
        """)

        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            ChannelRow(
                channel=r["channel"],
                obfuscated_code=r["obfuscated_code"],
                total_uploaded=int(r["total_uploaded"] or 0),
                total_created=int(r["total_created"] or 0),
                total_published=int(r["total_published"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
                avg_duration_min=round(float(r["avg_duration_min"] or 0), 1),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_uploaded", "total_published", "uploaded_duration_hrs"],
            grain="segment-aggregated",
            unit="count",
        )

    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dc.name                                                         AS channel,
            dc.obfuscated_code                                              AS obfuscated_code,
            COUNT(fv.id)                                                    AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)               AS total_created,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                  AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0) / 3600.0           AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0) / 3600.0           AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0) / 3600.0           AS published_duration_hrs,
            CASE WHEN COUNT(fv.id) > 0
                 THEN COALESCE(SUM(fv.uploaded_duration_sec), 0) / COUNT(fv.id) / 60.0
                 ELSE 0 END                                                 AS avg_duration_min
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name, dc.obfuscated_code
        ORDER BY total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        ChannelRow(
            channel=r["channel"],
            obfuscated_code=r["obfuscated_code"],
            total_uploaded=int(r["total_uploaded"] or 0),
            total_created=int(r["total_created"] or 0),
            total_published=int(r["total_published"] or 0),
            uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
            created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
            published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
            avg_duration_min=round(float(r["avg_duration_min"] or 0), 1),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_uploaded", "total_published", "uploaded_duration_hrs"],
                grain="segment-aggregated", unit="count")


@router.get("/users", response_model=ApiResponse[List[ChannelUserRow]])
async def get_channel_users(
    channel: Optional[str] = None,
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[ChannelUserRow]]:
    """Cross-tab of channel × user productivity."""
    if supports_channel_user_aggregate(f):
        target_channel = channel or f.channel
        clauses = []
        params: dict[str, str] = {}
        if target_channel:
            clauses.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
            params["channel"] = target_channel
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = text(f"""
            SELECT
                dc.name AS channel,
                du.name AS "user",
                acus.uploaded_count AS total_uploaded,
                acus.created_count AS total_created,
                acus.published_count AS total_published,
                acus.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                acus.created_duration_sec / 3600.0 AS created_duration_hrs,
                acus.published_duration_sec / 3600.0 AS published_duration_hrs
            FROM agg_channel_user_stat acus
            JOIN dim_channel dc ON dc.id = acus.channel_id
            JOIN dim_user du ON du.id = acus.user_id
            {where_sql}
            ORDER BY dc.name, acus.uploaded_count DESC, du.name
        """)

        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            ChannelUserRow(
                channel=r["channel"],
                user=r["user"],
                total_uploaded=int(r["total_uploaded"] or 0),
                total_created=int(r["total_created"] or 0),
                total_published=int(r["total_published"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_uploaded", "total_published"],
            grain="segment-aggregated",
            unit="count",
        )

    where, params = build_where_clause(f)
    if channel:
        where.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
        params["channel"] = channel
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dc.name                                               AS channel,
            du.name                                               AS "user",
            COUNT(fv.id)                                          AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)     AS total_created,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)        AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0   AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0   AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0   AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        JOIN dim_user    du ON du.id = fv.user_id
        {where_sql}
        GROUP BY dc.name, du.name
        ORDER BY dc.name, total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        ChannelUserRow(
            channel=r["channel"],
            user=r["user"],
            total_uploaded=int(r["total_uploaded"] or 0),
            total_created=int(r["total_created"] or 0),
            total_published=int(r["total_published"] or 0),
            uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
            created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
            published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_uploaded", "total_published"],
                grain="segment-aggregated", unit="count")
