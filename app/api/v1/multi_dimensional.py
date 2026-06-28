"""GET /api/v1/multi-dimensional — dimension × dimension matrix analysis.

This is the explicitly highlighted requirement from the problem statement:
supports any dim1 × dim2 cross-tab for uploaded, published, duration, conversion %.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, MultiDimensionalCell, MultiDimensionalResponse
from app.utils.response import wrap

router = APIRouter(prefix="/multi-dimensional", tags=["Multi-Dimensional"])

_METRICS = {"uploaded", "published", "duration_hrs", "publish_conversion_pct", "contribution_pct"}


def _resolve_dim(dim: str, which: int) -> dict:
    """Return a cfg dict compatible with the query builder for a numbered dim slot."""
    dim_def = DIMENSION_REGISTRY.get(dim, DIMENSION_REGISTRY["channel"])
    alias = f"d{which}"
    return {
        "join":   dim_def.join_sql(alias),
        "name":   dim_def.name_sql(alias),
        "bridge": dim_def.supports_bridge,
        "direct": dim_def.is_direct,
    }


@router.get("", response_model=ApiResponse[MultiDimensionalResponse])
async def multi_dimensional(
    dim1: str = Query(default="channel",     description="First dimension"),
    dim2: str = Query(default="language",    description="Second dimension"),
    metric: str = Query(default="uploaded",  description="uploaded|published|duration_hrs|publish_conversion_pct"),
    top_n: int = Query(default=10, ge=1, le=50, description="Limit each dimension to top N values"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[MultiDimensionalResponse]:
    if dim1 not in DIMENSION_REGISTRY or dim2 not in DIMENSION_REGISTRY:
        raise HTTPException(
            status_code=422,
            detail=f"dim1 and dim2 must be one of: {', '.join(DIMENSION_REGISTRY.keys())}",
        )
    if dim1 == dim2:
        raise HTTPException(status_code=422, detail="dim1 and dim2 must be different.")
    if metric not in _METRICS:
        metric = "uploaded"

    cfg1 = _resolve_dim(dim1, 1)
    cfg2 = _resolve_dim(dim2, 2)

    where, params = build_where_clause(f)

    # Build JOIN list, deduplicating identical join strings.
    # Note: DIMENSION_REGISTRY["output_type"].join_sql() already embeds the bridge
    # (fact_video_output_type fvot) join, so no separate bridge_join is needed.
    all_joins: list[str] = []
    for join_str in (cfg1.get("join", ""), cfg2.get("join", "")):
        if join_str and join_str not in all_joins:
            all_joins.append(join_str)

    join_sql  = " ".join(all_joins)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Get top N values for each dimension first
    for idx, (cfg, dim_name, param_key) in enumerate(
        [(cfg1, dim1, f"top_n_{1}"), (cfg2, dim2, f"top_n_{2}")], start=1
    ):
        pass  # we'll filter in the main query via LIMIT on the axes

    sql = text(f"""
        WITH base AS (
            SELECT
                COALESCE({cfg1['name']}, 'Unknown')  AS d1_val,
                COALESCE({cfg2['name']}, 'Unknown')  AS d2_val,
                COUNT(fv.id)                          AS uploaded,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END) AS published,
                COALESCE(SUM(fv.uploaded_duration_sec), 0) / 3600.0 AS duration_hrs
            FROM fact_video fv
            {join_sql}
            {where_sql}
            GROUP BY d1_val, d2_val
        ),
        totals AS (
            SELECT SUM(uploaded) AS grand_total FROM base
        )
        SELECT
            b.d1_val,
            b.d2_val,
            b.uploaded,
            b.published,
            ROUND(b.duration_hrs::numeric, 2) AS duration_hrs,
            CASE WHEN b.uploaded > 0
                 THEN ROUND(b.published::numeric / b.uploaded * 100, 1)
                 ELSE 0 END AS publish_conversion_pct,
            CASE WHEN t.grand_total > 0
                 THEN ROUND(b.uploaded::numeric / t.grand_total * 100, 2)
                 ELSE 0 END AS contribution_pct
        FROM base b, totals t
        ORDER BY b.uploaded DESC
        LIMIT 2000
    """)

    rows = (await db.execute(sql, params)).mappings().all()

    # Extract top-N values for each axis by total volume
    d1_totals: dict[str, int] = {}
    d2_totals: dict[str, int] = {}
    for r in rows:
        d1_totals[r["d1_val"]] = d1_totals.get(r["d1_val"], 0) + int(r["uploaded"] or 0)
        d2_totals[r["d2_val"]] = d2_totals.get(r["d2_val"], 0) + int(r["uploaded"] or 0)

    top_d1 = set(sorted(d1_totals, key=d1_totals.get, reverse=True)[:top_n])
    top_d2 = set(sorted(d2_totals, key=d2_totals.get, reverse=True)[:top_n])

    cells: List[MultiDimensionalCell] = []
    for r in rows:
        d1v = r["d1_val"]
        d2v = r["d2_val"]
        if d1v not in top_d1 or d2v not in top_d2:
            continue
        cells.append(
            MultiDimensionalCell(
                dim1=d1v,
                dim2=d2v,
                uploaded=int(r["uploaded"] or 0),
                published=int(r["published"] or 0),
                duration_hrs=float(r["duration_hrs"] or 0),
                publish_conversion_pct=float(r["publish_conversion_pct"] or 0),
                contribution_pct=float(r["contribution_pct"] or 0),
            )
        )

    # Ordered axis labels
    d1_ordered = sorted(top_d1, key=lambda v: d1_totals.get(v, 0), reverse=True)
    d2_ordered = sorted(top_d2, key=lambda v: d2_totals.get(v, 0), reverse=True)

    data = MultiDimensionalResponse(
        dim1=dim1,
        dim2=dim2,
        metric=metric,
        cells=cells,
        dim1_values=d1_ordered,
        dim2_values=d2_ordered,
    )
    return wrap(data, f,
                metrics=["total_uploaded", "total_published", "uploaded_duration_hrs", "publish_rate"],
                grain="segment-aggregated",
                caveats=[f"Cross-tab limited to top {top_n} values per axis by uploaded volume"],
                unit=None)
