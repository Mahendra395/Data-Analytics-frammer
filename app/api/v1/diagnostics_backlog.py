"""GET /api/v1/diagnostics/backlog — backlog inventory and aging distribution."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import (
    AgingBucket,
    AgingResponse,
    ApiResponse,
    BacklogItem,
    BacklogResponse,
)
from app.utils.response import wrap

router = APIRouter(tags=["Diagnostics"])

_BACKLOG_BUCKETS = [
    ("0–7 days",   0,   7),
    ("8–14 days",  8,  14),
    ("15–30 days", 15, 30),
    ("31–60 days", 31, 60),
    ("61–90 days", 61, 90),
    ("90+ days",   91, None),
]


@router.get("/backlog", response_model=ApiResponse[BacklogResponse])
async def lag_backlog(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[BacklogResponse]:
    """
    Processed-but-not-published backlog: total count, age distribution,
    and a list of the oldest items.
    """
    where, params = build_where_clause(f)
    backlog_clauses = (where or []) + [
        "fv.published = FALSE",
        "(fv.processed_at IS NOT NULL OR fv.created_duration_sec > 0)",
    ]
    where_sql = "WHERE " + " AND ".join(backlog_clauses)

    data_sql = text(f"""
        SELECT
            fv.id::text                                              AS id,
            fv.video_id,
            fv.headline,
            dcl.name                                                 AS client,
            dc.name                                                  AS channel,
            du.name                                                  AS "user",
            fv.uploaded_at,
            ROUND(
                (EXTRACT(EPOCH FROM NOW()) - COALESCE(fv.processed_at, fv.uploaded_at))
                / 86400.0, 1
            )                                                        AS days_in_backlog
        FROM fact_video fv
        LEFT JOIN dim_channel dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user    du  ON du.id  = fv.user_id
        LEFT JOIN dim_client  dcl ON dcl.id = fv.client_id
        {where_sql}
        ORDER BY days_in_backlog DESC
    """)
    rows = (await db.execute(data_sql, params)).mappings().all()

    total = len(rows)
    if total == 0:
        return wrap(
            BacklogResponse(total_backlog=0, oldest_days=0.0, avg_days=0.0, buckets=[], oldest_items=[]),
            f, grain="video-level", caveats=["No backlog items match the current filters"],
        )

    days_list = [float(r["days_in_backlog"] or 0) for r in rows]
    oldest_days = max(days_list)
    avg_days    = round(sum(days_list) / total, 1)

    buckets: List[AgingBucket] = []
    for label, lo, hi in _BACKLOG_BUCKETS:
        count = sum(1 for d in days_list if d >= lo and (hi is None or d <= hi))
        buckets.append(AgingBucket(
            bucket_label=label,
            min_days=lo,
            max_days=hi,
            count=count,
            pct=round(count / total * 100, 1) if total else 0.0,
        ))

    oldest_items = [
        BacklogItem(
            row_id=str(r["id"]),
            video_id=r.get("video_id"),
            headline=r.get("headline"),
            client=r.get("client"),
            channel=r.get("channel"),
            user=r.get("user"),
            uploaded_at=r.get("uploaded_at"),
            days_in_backlog=float(r["days_in_backlog"] or 0),
        )
        for r in rows[:50]
    ]

    return wrap(
        BacklogResponse(
            total_backlog=total,
            oldest_days=oldest_days,
            avg_days=avg_days,
            buckets=buckets,
            oldest_items=oldest_items,
        ),
        f,
        metrics=["total_processed"],
        grain="video-level",
        caveats=["Backlog = processed (is_processed=TRUE) but not yet published"],
        unit="count",
    )


@router.get("/aging", response_model=ApiResponse[AgingResponse])
async def lag_aging(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AgingResponse]:
    """
    Bucket distribution of backlog age for processed-but-not-published videos.
    Also returns the single oldest item.
    """
    where, params = build_where_clause(f)
    backlog_clauses = (where or []) + [
        "fv.published = FALSE",
        "(fv.processed_at IS NOT NULL OR fv.created_duration_sec > 0)",
    ]
    where_sql = "WHERE " + " AND ".join(backlog_clauses)

    data_sql = text(f"""
        SELECT
            fv.id::text AS id,
            fv.video_id,
            fv.headline,
            dcl.name  AS client,
            dc.name   AS channel,
            du.name   AS "user",
            fv.uploaded_at,
            ROUND(
                (EXTRACT(EPOCH FROM NOW()) - COALESCE(fv.processed_at, fv.uploaded_at))
                / 86400.0, 1
            ) AS days_in_backlog
        FROM fact_video fv
        LEFT JOIN dim_channel dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user    du  ON du.id  = fv.user_id
        LEFT JOIN dim_client  dcl ON dcl.id = fv.client_id
        {where_sql}
        ORDER BY days_in_backlog DESC
    """)
    rows = (await db.execute(data_sql, params)).mappings().all()

    total = len(rows)
    if total == 0:
        return wrap(AgingResponse(buckets=[], total_backlog=0, oldest_item=None),
                    f, grain="video-level", caveats=["No backlog items match the current filters"])

    days_list = [float(r["days_in_backlog"] or 0) for r in rows]
    buckets: List[AgingBucket] = []
    for label, lo, hi in _BACKLOG_BUCKETS:
        count = sum(1 for d in days_list if d >= lo and (hi is None or d <= hi))
        buckets.append(AgingBucket(
            bucket_label=label,
            min_days=lo,
            max_days=hi,
            count=count,
            pct=round(count / total * 100, 1),
        ))

    oldest = rows[0]
    oldest_item = BacklogItem(
        row_id=str(oldest["id"]),
        video_id=oldest.get("video_id"),
        headline=oldest.get("headline"),
        client=oldest.get("client"),
        channel=oldest.get("channel"),
        user=oldest.get("user"),
        uploaded_at=oldest.get("uploaded_at"),
        days_in_backlog=float(oldest["days_in_backlog"] or 0),
    )

    return wrap(
        AgingResponse(buckets=buckets, total_backlog=total, oldest_item=oldest_item),
        f,
        metrics=["total_processed"],
        grain="video-level",
        caveats=["Age is computed from processed_at (or uploaded_at as fallback) to NOW()"],
        unit="count",
    )
