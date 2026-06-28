"""GET /api/v1/diagnostics — concentration analysis and benchmarks."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_dim_only_where_clause, build_where_clause
from app.schemas.responses import (
    ApiResponse,
    BenchmarkResponse,
    BenchmarkSegmentRow,
)
from app.utils.response import wrap

router = APIRouter(tags=["Diagnostics"])


@router.get("/concentration", response_model=ApiResponse[Dict[str, Any]])
async def concentration(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[Dict[str, Any]]:
    """
    Pareto / concentration analysis: share of top-5 channels/users in total volume.
    Returns top_5_channel_share_pct, top_5_user_share_pct, and full ranked lists.
    """
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total_sql = text(f"SELECT COUNT(*) AS total FROM fact_video fv {where_sql}")
    total = int((await db.execute(total_sql, params)).scalar_one() or 0)

    if total == 0:
        return wrap(
            {"top_5_channel_share_pct": 0, "top_5_user_share_pct": 0, "top_channels": [], "top_users": []},
            f, metrics=["total_uploaded"], grain="segment-aggregated",
            caveats=["No data matches the current filters"], unit="percent",
        )

    ch_sql = text(f"""
        SELECT dc.name AS name, COUNT(fv.id) AS cnt
        FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    ch_rows = (await db.execute(ch_sql, params)).mappings().all()

    user_sql = text(f"""
        SELECT du.name AS name, COUNT(fv.id) AS cnt
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        {where_sql}
        GROUP BY du.name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    user_rows = (await db.execute(user_sql, params)).mappings().all()

    def _top5_share(rows) -> float:
        top5 = sum(int(r["cnt"] or 0) for r in list(rows)[:5])
        return round(top5 / total * 100, 1) if total else 0

    ch_list   = [{"name": r["name"], "count": int(r["cnt"] or 0),
                  "share_pct": round(int(r["cnt"] or 0) / total * 100, 1)} for r in ch_rows]
    user_list = [{"name": r["name"], "count": int(r["cnt"] or 0),
                  "share_pct": round(int(r["cnt"] or 0) / total * 100, 1)} for r in user_rows]

    data = {
        "total": total,
        "top_5_channel_share_pct": _top5_share(ch_rows),
        "top_5_user_share_pct":    _top5_share(user_rows),
        "top_channels": ch_list,
        "top_users":    user_list,
    }
    return wrap(data, f, metrics=["total_uploaded"],
                grain="segment-aggregated",
                caveats=["Share percentages are relative to the filtered total, not the portfolio total"],
                unit="percent")


# ── Benchmark helpers ──────────────────────────────────────────────────────────

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _month_epochs(yr: int, mo: int):
    first = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(yr, mo)[1]
    last  = int(datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return first, last


def _prev_month(yr: int, mo: int):
    return (yr - 1, 12) if mo == 1 else (yr, mo - 1)


async def _build_benchmark(
    db: AsyncSession,
    dimension: str,
    metric: str,
    f: FilterParams,
) -> BenchmarkResponse:
    """
    Generic benchmark builder.
    dimension: "client" | "channel" | "user" | "type" | "language"
    metric: "uploaded" | "published" | "publish_rate" | "duration_hrs"
    """
    where, params = build_where_clause(f)

    # Exclude the benchmarked dimension's filter so the peer group contains
    # all members (e.g. when benchmarking "user", don't filter to only 1 user)
    _DIMENSION_SKIP_MAP = {
        "user":     {"user"},
        "channel":  {"channel"},
        "client":   {"client"},
        "language": {"language"},
    }
    skip_dims = _DIMENSION_SKIP_MAP.get(dimension, set())
    dim_where, dim_params = build_dim_only_where_clause(f, exclude_dimensions=skip_dims)

    if f.date_to:
        ref_yr, ref_mo = f.date_to.year, f.date_to.month
    else:
        latest = (await db.execute(text("""
            SELECT EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int AS yr,
                   EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo
            FROM fact_video WHERE uploaded_at IS NOT NULL ORDER BY uploaded_at DESC LIMIT 1
        """))).mappings().first()
        if latest:
            ref_yr, ref_mo = int(latest["yr"]), int(latest["mo"])
        else:
            from datetime import date
            today = date.today()
            ref_yr, ref_mo = today.year, today.month

    prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)
    cur_from, cur_to = _month_epochs(ref_yr, ref_mo)
    prv_from, prv_to = _month_epochs(prev_yr, prev_mo)

    DIM_MAP = {
        "client":   ("JOIN dim_client dcl ON dcl.id = fv.client_id",  "dcl.name"),
        "channel":  ("JOIN dim_channel dc ON dc.id = fv.channel_id",  "dc.name"),
        "user":     ("JOIN dim_user du ON du.id = fv.user_id",         "du.name"),
        "type":     ("LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
                     "JOIN dim_output_type dot ON dot.id = fvot.output_type_id",
                     "dot.name"),
        "language": ("JOIN dim_language dl ON dl.id = fv.language_id", "dl.display_name"),
    }
    if dimension not in DIM_MAP:
        dimension = "channel"
    dim_join, dim_col = DIM_MAP[dimension]

    METRIC_MAP = {
        "uploaded":      "COUNT(fv.id)",
        "published":     "SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)",
        "publish_rate":  "CASE WHEN COUNT(fv.id) > 0 THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)/COUNT(fv.id)*100,1) ELSE 0 END",
        "duration_hrs":  "COALESCE(SUM(fv.uploaded_duration_sec),0)/3600.0",
    }
    metric_expr = METRIC_MAP.get(metric, METRIC_MAP["uploaded"])

    def _period_where(ep_from: int, ep_to: int, extra: list, extra_params: dict):
        key_from = f"_ep_from_{ep_from}"
        key_to   = f"_ep_to_{ep_to}"
        w = dim_where + extra + [f"fv.uploaded_at >= :{key_from}", f"fv.uploaded_at <= :{key_to}"]
        return "WHERE " + " AND ".join(w), {**dim_params, key_from: ep_from, key_to: ep_to, **extra_params}

    cur_wsql, cur_p = _period_where(cur_from, cur_to, [], {})
    prv_wsql, prv_p = _period_where(prv_from, prv_to, [], {})

    cur_sql = text(f"""
        SELECT {dim_col} AS seg, {metric_expr} AS val
        FROM fact_video fv {dim_join}
        {cur_wsql}
        GROUP BY {dim_col}
    """)
    prv_sql = text(f"""
        SELECT {dim_col} AS seg, {metric_expr} AS val
        FROM fact_video fv {dim_join}
        {prv_wsql}
        GROUP BY {dim_col}
    """)

    cur_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(cur_sql, cur_p)).mappings().all()}
    prv_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(prv_sql, prv_p)).mappings().all()}

    if not cur_rows:
        return BenchmarkResponse(
            dimension=dimension,
            metric=metric,
            segments=[],
            portfolio_avg=0.0,
            portfolio_median=0.0,
        )

    values = list(cur_rows.values())
    portfolio_avg    = round(sum(values) / len(values), 2)
    sorted_vals      = sorted(values)
    n                = len(sorted_vals)
    portfolio_median = round(
        (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2 if n % 2 == 0 else sorted_vals[n // 2],
        2,
    )

    segments: List[BenchmarkSegmentRow] = []
    for seg, val in sorted(cur_rows.items(), key=lambda x: x[1], reverse=True):
        rank  = sum(1 for v in values if v < val)
        pctile = round(rank / n * 100, 1)
        prev_val = prv_rows.get(seg, 0.0)
        trend_delta = round(val - prev_val, 2)
        peer_avg = round((sum(values) - val) / (n - 1), 2) if n > 1 else portfolio_avg

        # Compute health score: composite of percentile and trend direction
        trend_bonus = min(max(trend_delta / (portfolio_avg or 1) * 20, -10), 10)
        health = round(min(max(pctile + trend_bonus, 0), 100), 1)
        risk = "critical" if health < 30 else ("warning" if health < 60 else "healthy")
        grade = "A" if health >= 80 else ("B" if health >= 60 else ("C" if health >= 40 else ("D" if health >= 20 else "F")))

        segments.append(
            BenchmarkSegmentRow(
                segment=seg,
                segment_type=dimension,
                metric=metric,
                value=round(val, 2),
                portfolio_avg=portfolio_avg,
                peer_avg=peer_avg,
                percentile=pctile,
                trend_delta=trend_delta,
                health_score=health,
                risk_level=risk,
                grade=grade,
            )
        )

    return BenchmarkResponse(
        dimension=dimension,
        metric=metric,
        segments=segments,
        portfolio_avg=portfolio_avg,
        portfolio_median=portfolio_median,
    )


# ── /benchmarks/* ──────────────────────────────────────────────────────────────

_BENCHMARK_CAVEATS = [
    "Benchmark compares current month vs previous month; results shift with the date filter",
    "Percentile rank is computed within the filtered peer group, not the full portfolio",
]


@router.get("/benchmarks/client", response_model=ApiResponse[BenchmarkResponse])
async def benchmarks_client(
    metric: str = Query(default="uploaded", description="uploaded | published | publish_rate | duration_hrs"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BenchmarkResponse]:
    data = await _build_benchmark(db, "client", metric, f)
    return wrap(data, f, metrics=[metric], grain="monthly-aggregated", caveats=_BENCHMARK_CAVEATS)


@router.get("/benchmarks/channel", response_model=ApiResponse[BenchmarkResponse])
async def benchmarks_channel(
    metric: str = Query(default="uploaded", description="uploaded | published | publish_rate | duration_hrs"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BenchmarkResponse]:
    data = await _build_benchmark(db, "channel", metric, f)
    return wrap(data, f, metrics=[metric], grain="monthly-aggregated", caveats=_BENCHMARK_CAVEATS)


@router.get("/benchmarks/user", response_model=ApiResponse[BenchmarkResponse])
async def benchmarks_user(
    metric: str = Query(default="uploaded", description="uploaded | published | publish_rate | duration_hrs"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BenchmarkResponse]:
    data = await _build_benchmark(db, "user", metric, f)
    return wrap(data, f, metrics=[metric], grain="monthly-aggregated", caveats=_BENCHMARK_CAVEATS)


@router.get("/benchmarks/type", response_model=ApiResponse[BenchmarkResponse])
async def benchmarks_type(
    metric: str = Query(default="uploaded", description="uploaded | published | publish_rate | duration_hrs"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BenchmarkResponse]:
    data = await _build_benchmark(db, "type", metric, f)
    return wrap(data, f, metrics=[metric], grain="monthly-aggregated", caveats=_BENCHMARK_CAVEATS)


@router.get("/benchmarks/language", response_model=ApiResponse[BenchmarkResponse])
async def benchmarks_language(
    metric: str = Query(default="uploaded", description="uploaded | published | publish_rate | duration_hrs"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BenchmarkResponse]:
    data = await _build_benchmark(db, "language", metric, f)
    return wrap(data, f, metrics=[metric], grain="monthly-aggregated", caveats=_BENCHMARK_CAVEATS)
