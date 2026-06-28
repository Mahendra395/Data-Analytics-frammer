"""GET /api/v1/funnel — upload → processed → published funnel."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, FunnelResponse, FunnelStage
from app.services.aggregate_support import supports_language_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/funnel", tags=["Funnel"])

# Segments supported for by-segment funnel breakdown.
# output_type is intentionally excluded: bridge-join would change funnel semantics.
_VALID_SEGMENT_KEYS = frozenset(
    k for k, d in DIMENSION_REGISTRY.items()
    if not d.supports_bridge and not d.is_direct
)


@router.get("", response_model=ApiResponse[FunnelResponse])
async def get_funnel(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[FunnelResponse]:
    """
    Three-stage funnel: Uploaded → Processed (has clips) → Published.
    'processed' = rows where is_processed = TRUE (created_duration_sec > 0, materialized at ingest).
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            COUNT(*)                                                         AS uploaded_count,
            COALESCE(SUM(uploaded_duration_sec),  0) / 3600.0               AS uploaded_hrs,
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)                   AS processed_count,
            COALESCE(SUM(CASE WHEN is_processed
                              THEN created_duration_sec ELSE 0 END), 0)/3600.0  AS processed_hrs,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)                      AS published_count,
            COALESCE(SUM(CASE WHEN published
                              THEN published_duration_sec ELSE 0 END), 0)/3600.0 AS published_hrs
        FROM fact_video fv
        {where_sql}
    """)
    row = (await db.execute(sql, params)).mappings().one()

    uploaded  = int(row["uploaded_count"]  or 0)
    proc_cnt  = int(row["processed_count"] or 0)
    pub_cnt   = int(row["published_count"] or 0)
    upl_hrs   = round(float(row["uploaded_hrs"]  or 0), 2)
    proc_hrs  = round(float(row["processed_hrs"] or 0), 2)
    pub_hrs   = round(float(row["published_hrs"] or 0), 2)

    def _pct(a: int, b: int) -> float | None:
        return round(a / b * 100, 1) if b else None

    stages: List[FunnelStage] = [
        FunnelStage(
            stage="Uploaded",
            count=uploaded,
            duration_hrs=upl_hrs,
            conversion_from_prev=None,
            conversion_from_first=100.0,
        ),
        FunnelStage(
            stage="Processed",
            count=proc_cnt,
            duration_hrs=proc_hrs,
            conversion_from_prev=_pct(proc_cnt, uploaded),
            conversion_from_first=_pct(proc_cnt, uploaded),
        ),
        FunnelStage(
            stage="Published",
            count=pub_cnt,
            duration_hrs=pub_hrs,
            conversion_from_prev=_pct(pub_cnt, proc_cnt),
            conversion_from_first=_pct(pub_cnt, uploaded),
        ),
    ]

    data = FunnelResponse(
        stages=stages,
        publish_gap_count=max(0, proc_cnt - pub_cnt),
        publish_gap_duration_hrs=round(max(0, proc_hrs - pub_hrs), 2),
    )
    return wrap(data, f,
                metrics=["total_uploaded", "total_processed", "total_published", "publish_rate"],
                grain="video-level",
                caveats=["Processed = is_processed flag (created_duration_sec > 0)"],
                unit="count")


@router.get("/by-segment", response_model=ApiResponse[List[dict]])
async def funnel_by_segment(
    segment: str = "channel",   # channel | language | input_type | output_type | user
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[dict]]:
    """Funnel conversion broken down by a chosen segment dimension."""
    if segment not in _VALID_SEGMENT_KEYS:
        segment = "channel"

    if segment == "language" and supports_language_aggregate(f):
        clauses: list[str] = []
        params: dict[str, str] = {}
        if f.language:
            clauses.append("(dl.iso_code = :language OR dl.display_name = :language)")
            params["language"] = f.language
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = text(f"""
            SELECT
                dl.display_name AS seg,
                als.uploaded_count AS uploaded,
                als.created_count AS processed,
                als.published_count AS published,
                CASE WHEN als.uploaded_count > 0
                     THEN ROUND(als.published_count::numeric / als.uploaded_count * 100, 1)
                     ELSE 0 END AS publish_rate_pct
            FROM agg_language_stat als
            JOIN dim_language dl ON dl.id = als.language_id
            {where_sql}
            ORDER BY als.uploaded_count DESC, dl.display_name
        """)
        rows = (await db.execute(sql, params)).mappings().all()
        data = [
            {
                "segment": r["seg"],
                "uploaded": int(r["uploaded"] or 0),
                "processed": int(r["processed"] or 0),
                "published": int(r["published"] or 0),
                "publish_rate_pct": float(r["publish_rate_pct"] or 0),
            }
            for r in rows
        ]
        return wrap(
            data,
            f,
            metrics=["total_uploaded", "total_published", "publish_rate"],
            grain="segment-aggregated",
            unit="count",
        )

    dim_def = DIMENSION_REGISTRY[segment]
    dim_join_clause = dim_def.join_sql("d")
    dim_name_col = dim_def.name_sql("d")
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        SELECT
            {dim_name_col}                                                       AS seg,
            COUNT(fv.id)                                                     AS uploaded,
            SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END)                AS processed,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)                   AS published,
            CASE WHEN COUNT(fv.id) > 0
                 THEN ROUND(SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)::numeric / COUNT(fv.id) * 100, 1)
                 ELSE 0 END                                                  AS publish_rate_pct
        FROM fact_video fv
        {dim_join_clause}
        {where_sql}
        GROUP BY {dim_name_col}
        ORDER BY uploaded DESC
        LIMIT 50
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    data = [
        {
            "segment": r["seg"],
            "uploaded": int(r["uploaded"] or 0),
            "processed": int(r["processed"] or 0),  # explicit 0 when none processed — no fallback to uploaded
            "published": int(r["published"] or 0),
            "publish_rate_pct": float(r["publish_rate_pct"] or 0),
        }
        for r in rows
    ]
    return wrap(data, f, metrics=["total_uploaded", "total_published", "publish_rate"],
                grain="segment-aggregated", unit="count")
