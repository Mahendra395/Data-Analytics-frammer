"""GET /api/v1/teams — team-level productivity aggregations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.schemas.responses import ApiResponse, TeamRow
from app.services.aggregate_support import supports_team_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/teams", tags=["Teams"])


@router.get("", response_model=ApiResponse[List[TeamRow]])
async def get_teams(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[TeamRow]]:
    if supports_team_aggregate(f):
        if f.channel:
            sql = text("""
                SELECT
                    du.team_name AS team_name,
                    COALESCE(SUM(acus.uploaded_count), 0) AS total_uploaded,
                    COALESCE(SUM(acus.published_count), 0) AS total_published,
                    COUNT(DISTINCT du.id) AS total_users,
                    COALESCE(SUM(acus.uploaded_duration_sec), 0) / 3600.0 AS uploaded_duration_hrs,
                    COALESCE(SUM(acus.published_duration_sec), 0) / 3600.0 AS published_duration_hrs,
                    CASE WHEN COALESCE(SUM(acus.uploaded_count), 0) > 0
                         THEN COALESCE(SUM(acus.uploaded_duration_sec), 0) / COALESCE(SUM(acus.uploaded_count), 0) / 60.0
                         ELSE 0 END AS avg_duration_min
                FROM agg_channel_user_stat acus
                JOIN dim_user du ON du.id = acus.user_id
                JOIN dim_channel dc ON dc.id = acus.channel_id
                WHERE du.team_name IS NOT NULL
                  AND LOWER(du.team_name) NOT IN ('unknown', '')
                  AND (dc.obfuscated_code = :channel OR dc.name = :channel)
                GROUP BY du.team_name
                ORDER BY total_uploaded DESC, du.team_name
            """)
            rows = (await db.execute(sql, {"channel": f.channel})).mappings().all()
        else:
            sql = text("""
                SELECT
                    du.team_name AS team_name,
                    COALESCE(SUM(aus.uploaded_count), 0) AS total_uploaded,
                    COALESCE(SUM(aus.published_count), 0) AS total_published,
                    COUNT(DISTINCT du.id) AS total_users,
                    COALESCE(SUM(aus.uploaded_duration_sec), 0) / 3600.0 AS uploaded_duration_hrs,
                    COALESCE(SUM(aus.published_duration_sec), 0) / 3600.0 AS published_duration_hrs,
                    CASE WHEN COALESCE(SUM(aus.uploaded_count), 0) > 0
                         THEN COALESCE(SUM(aus.uploaded_duration_sec), 0) / COALESCE(SUM(aus.uploaded_count), 0) / 60.0
                         ELSE 0 END AS avg_duration_min
                FROM agg_user_stat aus
                JOIN dim_user du ON du.id = aus.user_id
                WHERE du.team_name IS NOT NULL
                  AND LOWER(du.team_name) NOT IN ('unknown', '')
                GROUP BY du.team_name
                ORDER BY total_uploaded DESC, du.team_name
            """)
            rows = (await db.execute(sql)).mappings().all()

        data = [
            TeamRow(
                team_name=r["team_name"],
                total_uploaded=int(r["total_uploaded"] or 0),
                total_published=int(r["total_published"] or 0),
                total_users=int(r["total_users"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
                publish_rate=round(
                    int(r["total_published"] or 0) / int(r["total_uploaded"] or 1) * 100, 1
                ),
                avg_duration_min=round(float(r["avg_duration_min"] or 0), 1),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_uploaded", "total_published", "publish_rate"],
            grain="segment-aggregated",
            unit="count",
        )

    joins: list[str] = ["JOIN dim_user du ON du.id = fv.user_id"]
    where: list[str] = ["du.team_name IS NOT NULL", "LOWER(du.team_name) NOT IN ('unknown', '')"]
    params: dict = {}

    if f.client:
        joins.append("JOIN dim_client dcl ON dcl.id = fv.client_id")
        where.append("dcl.slug = :client")
        params["client"] = f.client

    if f.channel:
        joins.append("JOIN dim_channel dc ON dc.id = fv.channel_id")
        where.append("(dc.obfuscated_code = :channel OR dc.name = :channel)")
        params["channel"] = f.channel

    if f.date_from:
        where.append("fv.uploaded_at >= :date_from_epoch")
        params["date_from_epoch"] = int(
            datetime(f.date_from.year, f.date_from.month, f.date_from.day, tzinfo=timezone.utc).timestamp()
        )
    if f.date_to:
        where.append("fv.uploaded_at <= :date_to_epoch")
        params["date_to_epoch"] = int(
            datetime(f.date_to.year, f.date_to.month, f.date_to.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        )

    join_sql  = " ".join(joins)
    where_sql = "WHERE " + " AND ".join(where)

    sql = text(f"""
        SELECT
            du.team_name                                                    AS team_name,
            COUNT(fv.id)                                                    AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                  AS total_published,
            COUNT(DISTINCT du.id)                                           AS total_users,
            COALESCE(SUM(fv.uploaded_duration_sec),  0) / 3600.0           AS uploaded_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0) / 3600.0           AS published_duration_hrs,
            CASE WHEN COUNT(fv.id) > 0
                 THEN COALESCE(SUM(fv.uploaded_duration_sec), 0) / COUNT(fv.id) / 60.0
                 ELSE 0 END                                                 AS avg_duration_min
        FROM fact_video fv
        {join_sql}
        {where_sql}
        GROUP BY du.team_name
        ORDER BY total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        TeamRow(
            team_name=r["team_name"],
            total_uploaded=int(r["total_uploaded"] or 0),
            total_published=int(r["total_published"] or 0),
            total_users=int(r["total_users"] or 0),
            uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
            published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
            publish_rate=round(
                int(r["total_published"] or 0) / int(r["total_uploaded"] or 1) * 100, 1
            ),
            avg_duration_min=round(float(r["avg_duration_min"] or 0), 1),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_uploaded", "total_published", "publish_rate"],
                grain="segment-aggregated", unit="count")
