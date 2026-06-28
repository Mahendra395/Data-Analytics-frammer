"""GET /api/v1/input-types — content input type breakdown."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, InputTypeRow
from app.services.aggregate_support import supports_input_type_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/input-types", tags=["Input Types"])


@router.get("", response_model=ApiResponse[List[InputTypeRow]])
async def get_input_types(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[InputTypeRow]]:
    if supports_input_type_aggregate(f):
        sql = text("""
            SELECT
                dit.name AS input_type,
                aits.uploaded_count AS total_uploaded,
                aits.created_count AS total_created,
                aits.published_count AS total_published,
                aits.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                aits.created_duration_sec / 3600.0 AS created_duration_hrs,
                aits.published_duration_sec / 3600.0 AS published_duration_hrs
            FROM agg_input_type_stat aits
            JOIN dim_input_type dit ON dit.id = aits.input_type_id
            ORDER BY aits.uploaded_count DESC, dit.name
        """)
        rows = (await db.execute(sql)).mappings().all()
        data = [
            InputTypeRow(
                input_type=r["input_type"],
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
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            dit.name                                                  AS input_type,
            COUNT(fv.id)                                              AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)         AS total_created,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)            AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0       AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0       AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0       AS published_duration_hrs
        FROM fact_video fv
        JOIN dim_input_type dit ON dit.id = fv.input_type_id
        {where_sql}
        GROUP BY dit.name
        ORDER BY total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        InputTypeRow(
            input_type=r["input_type"],
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
