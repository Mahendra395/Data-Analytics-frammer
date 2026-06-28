"""GET /api/v1/monthly — monthly trend data."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, MonthlyRow
from app.services.aggregate_support import monthly_aggregate_filters, supports_monthly_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/monthly", tags=["Monthly"])

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


@router.get("", response_model=ApiResponse[List[MonthlyRow]])
async def get_monthly(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[MonthlyRow]]:
    """Monthly aggregation grouped by (year, month) with optional filters."""
    if supports_monthly_aggregate(f):
        where_sql, params = monthly_aggregate_filters(f)
        sql = text(f"""
            SELECT
                ams.year,
                ams.month,
                ams.month_label,
                ams.uploaded_count AS total_uploaded,
                ams.created_count AS total_created,
                ams.published_count AS total_published,
                ams.uploaded_duration_sec / 3600.0 AS uploaded_duration_hrs,
                ams.created_duration_sec / 3600.0 AS created_duration_hrs,
                ams.published_duration_sec / 3600.0 AS published_duration_hrs,
                CASE WHEN ams.uploaded_count > 0
                     THEN ams.uploaded_duration_sec / ams.uploaded_count / 60.0
                     ELSE 0 END AS avg_duration_min
            FROM agg_monthly_stat ams
            {where_sql}
            ORDER BY ams.year, ams.month
        """)

        rows = (await db.execute(sql, params)).mappings().all()
        out = [
            MonthlyRow(
                month_label=str(r["month_label"]),
                year=int(r["year"]),
                month=int(r["month"]),
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
            out,
            f,
            metrics=["total_uploaded", "total_published", "uploaded_duration_hrs"],
            grain="monthly-aggregated",
            unit="count",
        )

    where, params = build_where_clause(f)
    # always exclude rows with no upload timestamp
    where = ["fv.uploaded_at IS NOT NULL"] + where
    where_sql = "WHERE " + " AND ".join(where)

    sql = text(f"""
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(fv.uploaded_at))::int  AS year,
            EXTRACT(MONTH FROM to_timestamp(fv.uploaded_at))::int  AS month,
            COUNT(fv.id)                                            AS total_uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)       AS total_created,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)          AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0) / 3600.0   AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0) / 3600.0   AS created_duration_hrs,
            COALESCE(SUM(fv.published_duration_sec), 0) / 3600.0   AS published_duration_hrs,
            CASE WHEN COUNT(fv.id) > 0
                 THEN COALESCE(SUM(fv.uploaded_duration_sec), 0) / COUNT(fv.id) / 60.0
                 ELSE 0 END                                         AS avg_duration_min
        FROM fact_video fv
        {where_sql}
        GROUP BY year, month
        ORDER BY year, month
    """)

    result = await db.execute(sql, params)
    rows = result.mappings().all()

    out: List[MonthlyRow] = []
    for r in rows:
        yr  = int(r["year"])
        mo  = int(r["month"])
        label = f"{_MONTH_LABELS[mo]} {str(yr)[2:]}"  # e.g. "Mar 25"
        out.append(
            MonthlyRow(
                month_label=label,
                year=yr,
                month=mo,
                total_uploaded=int(r["total_uploaded"] or 0),
                total_created=int(r["total_created"] or 0),
                total_published=int(r["total_published"] or 0),
                uploaded_duration_hrs=round(float(r["uploaded_duration_hrs"] or 0), 2),
                created_duration_hrs=round(float(r["created_duration_hrs"] or 0), 2),
                published_duration_hrs=round(float(r["published_duration_hrs"] or 0), 2),
                avg_duration_min=round(float(r["avg_duration_min"] or 0), 1),
            )
        )
    return wrap(out, f, metrics=["total_uploaded", "total_published", "uploaded_duration_hrs"],
                grain="monthly-aggregated", unit="count")
