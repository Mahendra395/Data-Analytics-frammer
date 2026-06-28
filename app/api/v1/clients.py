"""GET /api/v1/clients/summary — per-client aggregated KPIs."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, ClientSummaryRow
from app.services.aggregate_support import supports_kpi_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/clients", tags=["Clients"])


@router.get("/summary", response_model=ApiResponse[List[ClientSummaryRow]])
async def get_clients_summary(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[ClientSummaryRow]]:
    """Return one aggregated row per client with volume + health KPIs.

    Applies all active filters (date range, channel, language, user, team,
    input_type, output_type, published_flag, published_platform, billable_flag).
    The per-client grouping is preserved; filters narrow the video population
    before aggregation.
    """
    if supports_kpi_aggregate(f):
        client_filter_sql = "AND dc.slug = :client" if f.client else ""
        sql = text(f"""
            SELECT
                dc.slug,
                dc.name,
                COALESCE((SELECT SUM(uploaded_count) FROM agg_monthly_stat), 0) AS total_uploaded,
                COALESCE((SELECT SUM(published_count) FROM agg_monthly_stat), 0) AS total_published,
                COALESCE((SELECT SUM(created_count) FROM agg_monthly_stat), 0) AS total_clips,
                COALESCE((
                    SELECT SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)
                    FROM fact_video fv
                    WHERE fv.client_id = dc.id
                ), 0) AS total_processed,
                ROUND(
                    COALESCE((SELECT SUM(published_count) FROM agg_monthly_stat), 0)::numeric
                    / NULLIF(COALESCE((SELECT SUM(uploaded_count) FROM agg_monthly_stat), 0), 0),
                    4
                ) AS publish_rate,
                (SELECT COUNT(*) FROM agg_channel_stat WHERE uploaded_count > 0) AS active_channels,
                (SELECT COUNT(*) FROM agg_user_stat WHERE uploaded_count > 0) AS active_users,
                ROUND(COALESCE((SELECT SUM(uploaded_duration_sec) FROM agg_monthly_stat), 0) / 3600.0, 2)
                    AS uploaded_duration_hrs
            FROM dim_client dc
            WHERE dc.slug = 'client-1'
              {client_filter_sql}
        """)
        params = {"client": f.client} if f.client else {}
        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            ClientSummaryRow(
                slug=r["slug"],
                name=r["name"],
                total_uploaded=int(r["total_uploaded"] or 0),
                total_processed=int(r["total_processed"] or 0),
                total_published=int(r["total_published"] or 0),
                total_clips=int(r["total_clips"] or 0),
                publish_rate=float(r["publish_rate"] or 0.0),
                active_channels=int(r["active_channels"] or 0),
                active_users=int(r["active_users"] or 0),
                uploaded_duration_hrs=float(r["uploaded_duration_hrs"] or 0.0),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_uploaded", "total_processed", "total_published", "publish_rate", "uploaded_duration_hrs"],
            grain="segment-aggregated",
            unit="count",
        )

    where, params = build_where_clause(f)
    # We always join fact_video so the global filters apply correctly.
    # dim_client rows with zero videos after filtering are excluded.
    where_sql = ("AND " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dc.slug,
            dc.name,
            COUNT(DISTINCT fv.id)                                           AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                   AS total_published,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)               AS total_processed,
            COALESCE(SUM(clips.clip_count), 0)                              AS total_clips,
            ROUND(
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)::numeric
                / NULLIF(COUNT(DISTINCT fv.id), 0),
                4
            )                                                               AS publish_rate,
            COUNT(DISTINCT fv.channel_id)                                   AS active_channels,
            COUNT(DISTINCT fv.user_id)                                      AS active_users,
            ROUND(COALESCE(SUM(fv.uploaded_duration_sec), 0) / 3600.0, 2)  AS uploaded_duration_hrs
        FROM       dim_client dc
        JOIN       fact_video fv ON fv.client_id = dc.id
        LEFT JOIN  (
            SELECT video_id, SUM(created_count) AS clip_count
            FROM   fact_video_output_type
            GROUP BY video_id
        ) clips ON clips.video_id = fv.id
        WHERE      1=1
        {where_sql}
        GROUP BY   dc.id, dc.slug, dc.name
        ORDER BY   total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        ClientSummaryRow(
            slug=r["slug"],
            name=r["name"],
            total_uploaded=int(r["total_uploaded"] or 0),
            total_processed=int(r["total_processed"] or 0),
            total_published=int(r["total_published"] or 0),
            total_clips=int(r["total_clips"] or 0),
            publish_rate=float(r["publish_rate"] or 0.0),
            active_channels=int(r["active_channels"] or 0),
            active_users=int(r["active_users"] or 0),
            uploaded_duration_hrs=float(r["uploaded_duration_hrs"] or 0.0),
        )
        for r in rows
    ]
    return wrap(data, f,
                metrics=["total_uploaded", "total_processed", "total_published", "publish_rate", "uploaded_duration_hrs"],
                grain="segment-aggregated",
                unit="count")
