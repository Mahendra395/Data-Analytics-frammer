"""GET /api/v1/output-types — output type breakdown."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, OutputTypeRow
from app.services.aggregate_support import supports_output_type_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/output-types", tags=["Output Types"])


@router.get("", response_model=ApiResponse[List[OutputTypeRow]])
async def get_output_types(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[OutputTypeRow]]:
    """Per-output-type clip counts from bridge table, with optional filters."""
    if supports_output_type_aggregate(f):
        sql = text("""
            SELECT
                dot.name AS output_type,
                aots.created_count AS total_clips_created,
                aots.published_count AS total_clips_published,
                aots.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                aots.created_duration_sec / 3600.0 AS created_duration_hrs,
                aots.published_duration_sec / 3600.0 AS published_duration_hrs
            FROM agg_output_type_stat aots
            JOIN dim_output_type dot ON dot.id = aots.output_type_id
            ORDER BY aots.created_count DESC, dot.name
        """)

        rows = (await db.execute(sql)).mappings().all()
        data = [
            OutputTypeRow(
                output_type=r["output_type"],
                total_uploaded=int(r["total_clips_created"] or 0),
                total_created=int(r["total_clips_created"] or 0),
                total_published=int(r["total_clips_published"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
            )
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_clips_created", "total_clips_published"],
            grain="segment-aggregated",
            caveats=[
                "total_uploaded and total_created represent clip counts from the modified aggregate tables, not video counts. Use /core/kpis for video-level proxies."
            ],
            unit="count",
        )

    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dot.name                                                    AS output_type,
            -- NOTE: total_clips_created = SUM(created_count) from bridge table.
            -- This is CLIP count, not video count.  The response fields
            -- total_uploaded and total_created are both mapped to this clip count
            -- for backward-compat; for the canonical definition see
            -- METRIC_REGISTRY["total_clips_created"].
            COALESCE(SUM(fvot.created_count),   0)                      AS total_clips_created,
            COALESCE(SUM(fvot.published_count),  0)                     AS total_clips_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0          AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0          AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0          AS published_duration_hrs
        FROM fact_video_output_type fvot
        JOIN dim_output_type dot ON dot.id = fvot.output_type_id
        JOIN fact_video      fv  ON fv.id  = fvot.video_id
        {where_sql}
        GROUP BY dot.name
        ORDER BY total_clips_created DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        OutputTypeRow(
            output_type=r["output_type"],
            total_uploaded=int(r["total_clips_created"] or 0),
            total_created=int(r["total_clips_created"] or 0),
            total_published=int(r["total_clips_published"] or 0),
            uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
            created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
            published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
        )
        for r in rows
    ]
    return wrap(
        data, f,
        metrics=["total_clips_created", "total_clips_published"],
        grain="segment-aggregated",
        caveats=["total_uploaded and total_created represent clip counts from the bridge table, "
                 "not video counts. Use /core/kpis for video-level totals."],
        unit="count",
    )
