"""GET /api/v1/languages — language breakdown."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, LanguageRow
from app.services.aggregate_support import supports_language_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/languages", tags=["Languages"])


@router.get("", response_model=ApiResponse[List[LanguageRow]])
async def get_languages(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[LanguageRow]]:
    if supports_language_aggregate(f):
        clauses: list[str] = []
        params: dict[str, str] = {}
        if f.language:
            clauses.append("(dl.iso_code = :language OR dl.display_name = :language)")
            params["language"] = f.language
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = text(f"""
            SELECT
                dl.iso_code,
                dl.display_name,
                als.uploaded_count AS total_uploaded,
                als.created_count AS total_created,
                als.published_count AS total_published,
                als.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                als.created_duration_sec / 3600.0 AS created_duration_hrs,
                als.published_duration_sec / 3600.0 AS published_duration_hrs,
                ROUND(
                    als.uploaded_count * 100.0 / NULLIF(SUM(als.uploaded_count) OVER (), 0),
                    2
                ) AS percentage
            FROM agg_language_stat als
            JOIN dim_language dl ON dl.id = als.language_id
            {where_sql}
            ORDER BY als.uploaded_count DESC, dl.display_name
        """)
        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            LanguageRow(
                iso_code=r["iso_code"],
                display_name=r["display_name"],
                total_uploaded=int(r["total_uploaded"] or 0),
                total_created=int(r["total_created"] or 0),
                total_published=int(r["total_published"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
                percentage=float(r["percentage"] or 0),
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
    # The percentage is computed against the filtered set, not the global total

    sql = text(f"""
        SELECT
            dl.iso_code,
            dl.display_name,
            COUNT(fv.id)                                              AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)         AS total_created,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)            AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0       AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0       AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0)/3600.0       AS published_duration_hrs,
            ROUND(
                COUNT(fv.id) * 100.0 / NULLIF(SUM(COUNT(fv.id)) OVER (), 0),
                2
            ) AS percentage
        FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        {where_sql}
        GROUP BY dl.iso_code, dl.display_name
        ORDER BY total_uploaded DESC
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    data = [
        LanguageRow(
            iso_code=r["iso_code"],
            display_name=r["display_name"],
            total_uploaded=int(r["total_uploaded"] or 0),
            total_created=int(r["total_created"] or 0),
            total_published=int(r["total_published"] or 0),
            uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
            created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
            published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
            percentage=float(r["percentage"] or 0),
        )
        for r in rows
    ]
    return wrap(data, f, metrics=["total_uploaded", "total_published"],
                grain="segment-aggregated", unit="count")
