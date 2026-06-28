"""GET /api/v1/videos — paginated, sortable, filterable video list + extended explorer."""
from __future__ import annotations

import math
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import (
    ApiResponse,
    PaginatedResponse,
    VideoExplorerResponse,
    VideoRow,
    VideoRowExtended,
)
from app.utils.response import build_metadata

router = APIRouter(prefix="/videos", tags=["Videos"])

SortField = Literal[
    "uploaded_at", "headline", "channel", "user",
    "language", "input_type", "published", "uploaded_duration_hrs"
]


@router.get("", response_model=ApiResponse[PaginatedResponse[VideoRow]])
async def list_videos(
    page:      int   = Query(default=1, ge=1),
    page_size: int   = Query(default=50, ge=1, le=500, alias="pageSize"),
    sort_by:   SortField = Query(default="uploaded_at", alias="sortBy"),
    sort_dir:  Literal["asc", "desc"] = Query(default="desc", alias="sortDir"),
    search:    Optional[str] = Query(default=None),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[PaginatedResponse[VideoRow]]:
    offset = (page - 1) * page_size

    # Safe column → SQL expression mapping (avoids injection)
    SORT_MAP = {
        "uploaded_at":           "fv.uploaded_at",
        "headline":              "fv.headline",
        "channel":               "dc.name",
        "user":                  "du.name",
        "language":              "dl.display_name",
        "input_type":            "dit.name",
        "published":             "fv.published",
        "uploaded_duration_hrs": "fv.uploaded_duration_sec",
    }
    sort_col = SORT_MAP.get(sort_by, "fv.uploaded_at")
    order_clause = f"{sort_col} {sort_dir.upper()}"

    where_clauses, params = build_where_clause(f)
    if search:
        where_clauses.append("fv.headline ILIKE :search")
        params["search"] = f"%{search}%"
    base_where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params["limit"]  = page_size
    params["offset"] = offset

    count_sql = text(f"""
        SELECT COUNT(fv.id) AS cnt
        FROM fact_video fv
        LEFT JOIN dim_channel    dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user       du  ON du.id  = fv.user_id
        LEFT JOIN dim_language   dl  ON dl.id  = fv.language_id
        LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
        LEFT JOIN dim_client     dcl ON dcl.id = fv.client_id
        {base_where}
    """)
    data_sql = text(f"""
        SELECT
            fv.id,
            fv.video_id,
            fv.headline,
            dcl.name                                              AS client,
            dc.name                                               AS channel,
            du.name                                               AS "user",
            dl.display_name                                       AS language,
            dit.name                                              AS input_type,
            fv.published,
            fv.published_platform,
            fv.uploaded_at,
            fv.uploaded_duration_sec  / 3600.0                    AS uploaded_duration_hrs,
            fv.created_duration_sec   / 3600.0                    AS created_duration_hrs,
            fv.published_duration_sec / 3600.0                    AS published_duration_hrs,
            COALESCE(
                (SELECT STRING_AGG(dot2.name, ', ')
                 FROM fact_video_output_type fvot2
                 JOIN dim_output_type dot2 ON dot2.id = fvot2.output_type_id
                 WHERE fvot2.video_id = fv.id),
                ''
            ) AS output_types_str
        FROM fact_video fv
        LEFT JOIN dim_channel    dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user       du  ON du.id  = fv.user_id
        LEFT JOIN dim_language   dl  ON dl.id  = fv.language_id
        LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
        LEFT JOIN dim_client     dcl ON dcl.id = fv.client_id
        {base_where}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
    """)

    cnt_result = await db.execute(count_sql, params)
    total = cnt_result.scalar_one() or 0

    data_result = await db.execute(data_sql, params)
    rows = data_result.mappings().all()

    items: List[VideoRow] = []
    for r in rows:
        output_types = (
            [t.strip() for t in r["output_types_str"].split(",") if t.strip()]
            if r["output_types_str"]
            else []
        )
        items.append(
            VideoRow(
                id=str(r["id"]),
                video_id=r["video_id"],
                headline=r["headline"],
                client=r["client"],
                channel=r["channel"],
                user=r["user"],
                language=r["language"],
                input_type=r["input_type"],
                output_types=output_types,
                published=bool(r["published"]),
                published_platform=r["published_platform"],
                uploaded_at=r["uploaded_at"],
                uploaded_duration_hrs=(
                    round(float(r["uploaded_duration_hrs"]), 2)
                    if r["uploaded_duration_hrs"] is not None else None
                ),
                created_duration_hrs=(
                    round(float(r["created_duration_hrs"]), 2)
                    if r["created_duration_hrs"] is not None else None
                ),
                published_duration_hrs=(
                    round(float(r["published_duration_hrs"]), 2)
                    if r["published_duration_hrs"] is not None else None
                ),
            )
        )

    page_data = PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / page_size)),
    )
    return ApiResponse(data=page_data, meta=build_metadata(
        f, grain="video-level",
        caveats=["Sorted and paginated view; use /detail/videos/explorer for DQ flags and lag fields"],
    ))


# ── /videos/explorer ──────────────────────────────────────────────────────────

_PRESET_FILTERS: dict[str, str] = {
    "processed_not_published": (
        "fv.published = FALSE "
        "AND fv.is_processed = TRUE"
    ),
    "high_lag": (
        "COALESCE(fv.total_cycle_lag_sec, fv.processing_lag_sec) > 604800"  # >7 days
    ),
    "missing_metadata": (
        "fv.channel_id IS NULL OR fv.user_id IS NULL "
        "OR fv.language_id IS NULL OR fv.input_type_id IS NULL"
    ),
    "invalid_url": (
        "fv.published = TRUE "
        "AND (fv.published_url IS NULL OR fv.published_url = '' "
        "OR fv.published_url NOT LIKE 'http%')"
    ),
    "duplicates": "fv.duplicate_video_id_flag = TRUE",
    "billable_only":     "fv.billable_flag = TRUE",
    "non_billable_only": "fv.billable_flag = FALSE",
}

_ISSUE_CATEGORY_SQL = """
    CASE
        WHEN fv.duplicate_video_id_flag = TRUE THEN 'duplicate'
        WHEN fv.invalid_url_flag = TRUE        THEN 'invalid_url'
        WHEN fv.missing_team_flag = TRUE       THEN 'missing_metadata'
        WHEN (COALESCE(fv.total_cycle_lag_sec, fv.processing_lag_sec) > 604800) THEN 'high_lag'
        ELSE NULL
    END
"""


@router.get("/explorer", response_model=ApiResponse[VideoExplorerResponse])
async def explore_videos(
    page:      int   = Query(default=1, ge=1),
    page_size: int   = Query(default=50, ge=1, le=500, alias="pageSize"),
    sort_by:   str   = Query(default="uploaded_at", alias="sortBy"),
    sort_dir:  Literal["asc", "desc"] = Query(default="desc", alias="sortDir"),
    search:    Optional[str] = Query(default=None),
    preset:    Optional[str] = Query(default=None,
                                     description=(
                                         "processed_not_published | high_lag | missing_metadata | "
                                         "invalid_url | duplicates | billable_only | non_billable_only"
                                     )),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[VideoExplorerResponse]:
    """
    Extended video explorer — returns VideoRowExtended with all DQ flags,
    lag fields, timestamps, and optional preset filter.
    """
    offset = (page - 1) * page_size

    SORT_MAP = {
        "uploaded_at":              "fv.uploaded_at",
        "processed_at":             "fv.processed_at",
        "published_at":             "fv.published_at",
        "headline":                 "fv.headline",
        "channel":                  "dc.name",
        "user":                     "du.name",
        "language":                 "dl.display_name",
        "input_type":               "dit.name",
        "published":                "fv.published",
        "uploaded_duration_hrs":    "fv.uploaded_duration_sec",
        "processing_lag_min":       "fv.processing_lag_sec",
        "total_cycle_lag_min":      "fv.total_cycle_lag_sec",
    }
    sort_col    = SORT_MAP.get(sort_by, "fv.uploaded_at")
    order_clause = f"{sort_col} {sort_dir.upper()} NULLS LAST"

    where_clauses, params = build_where_clause(f)
    if search:
        where_clauses.append("fv.headline ILIKE :search")
        params["search"] = f"%{search}%"
    if preset and preset in _PRESET_FILTERS:
        where_clauses.append(f"({_PRESET_FILTERS[preset]})")
    full_where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params["limit"]  = page_size
    params["offset"] = offset

    count_sql = text(f"""
        SELECT COUNT(fv.id) AS cnt
        FROM fact_video fv
        LEFT JOIN dim_channel    dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user       du  ON du.id  = fv.user_id
        LEFT JOIN dim_language   dl  ON dl.id  = fv.language_id
        LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
        LEFT JOIN dim_client     dcl ON dcl.id = fv.client_id
        {full_where}
    """)

    data_sql = text(f"""
        SELECT
            fv.id::text                                               AS id,
            fv.video_id,
            fv.headline,
            dcl.name                                                  AS client,
            dcl.slug                                                  AS client_slug,
            dc.name                                                   AS channel,
            du.name                                                   AS "user",
            du.team_name,
            dl.display_name                                           AS language,
            dit.name                                                  AS input_type,
            fv.published,
            fv.published_platform                                     AS platform,
            fv.published_platform,
            fv.source_url,
            fv.published_url,
            fv.billable_flag,
            fv.uploaded_at,
            fv.processed_at,
            fv.published_at,
            fv.uploaded_duration_sec  / 3600.0                        AS uploaded_duration_hrs,
            fv.created_duration_sec   / 3600.0                        AS created_duration_hrs,
            fv.published_duration_sec / 3600.0                        AS published_duration_hrs,
            ROUND(fv.processing_lag_sec   / 60.0, 1)                 AS processing_lag_min,
            ROUND(fv.publishing_lag_sec   / 60.0, 1)                 AS publishing_lag_min,
            ROUND(fv.total_cycle_lag_sec  / 60.0, 1)                 AS total_cycle_lag_min,
            fv.missing_team_flag,
            fv.missing_platform_flag,
            fv.invalid_url_flag,
            fv.duplicate_video_id_flag,
            {_ISSUE_CATEGORY_SQL}                                      AS issue_category,
            COALESCE(
                (SELECT STRING_AGG(dot2.name, ', ')
                 FROM fact_video_output_type fvot2
                 JOIN dim_output_type dot2 ON dot2.id = fvot2.output_type_id
                 WHERE fvot2.video_id = fv.id),
                ''
            ) AS output_types_str
        FROM fact_video fv
        LEFT JOIN dim_channel    dc  ON dc.id  = fv.channel_id
        LEFT JOIN dim_user       du  ON du.id  = fv.user_id
        LEFT JOIN dim_language   dl  ON dl.id  = fv.language_id
        LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
        LEFT JOIN dim_client     dcl ON dcl.id = fv.client_id
        {full_where}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
    """)

    total = int((await db.execute(count_sql, params)).scalar_one() or 0)
    rows  = (await db.execute(data_sql, params)).mappings().all()

    items: List[VideoRowExtended] = []
    for r in rows:
        output_types = (
            [t.strip() for t in r["output_types_str"].split(",") if t.strip()]
            if r["output_types_str"] else []
        )

        def _flt(v) -> Optional[float]:
            return round(float(v), 2) if v is not None else None

        def _flt1(v) -> Optional[float]:
            return round(float(v), 1) if v is not None else None

        items.append(
            VideoRowExtended(
                id=r["id"],
                video_id=r["video_id"],
                headline=r["headline"],
                client=r["client"],
                client_slug=r.get("client_slug"),
                channel=r["channel"],
                user=r["user"],
                team_name=r.get("team_name"),
                language=r["language"],
                input_type=r["input_type"],
                output_types=output_types,
                platform=r.get("platform"),
                published=bool(r["published"]),
                published_platform=r.get("published_platform"),
                source_url=r.get("source_url"),
                published_url=r.get("published_url"),
                billable_flag=bool(r.get("billable_flag", False)),
                uploaded_at=r.get("uploaded_at"),
                processed_at=r.get("processed_at"),
                published_at=r.get("published_at"),
                uploaded_duration_hrs=_flt(r.get("uploaded_duration_hrs")),
                created_duration_hrs=_flt(r.get("created_duration_hrs")),
                published_duration_hrs=_flt(r.get("published_duration_hrs")),
                processing_lag_min=_flt1(r.get("processing_lag_min")),
                publishing_lag_min=_flt1(r.get("publishing_lag_min")),
                total_cycle_lag_min=_flt1(r.get("total_cycle_lag_min")),
                missing_team_flag=bool(r.get("missing_team_flag", False)),
                missing_platform_flag=bool(r.get("missing_platform_flag", False)),
                invalid_url_flag=bool(r.get("invalid_url_flag", False)),
                duplicate_video_id_flag=bool(r.get("duplicate_video_id_flag", False)),
                issue_category=r.get("issue_category"),
            )
        )

    preset_caveats = [f"Preset filter applied: {preset}"] if preset else []
    explorer_data = VideoExplorerResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / page_size)),
        preset=preset,
    )
    return ApiResponse(data=explorer_data, meta=build_metadata(
        f, grain="video-level",
        caveats=preset_caveats + [
            "DQ flags (missing_team_flag, invalid_url_flag, etc.) are pre-computed at ingest",
            "Lag values fall back to timestamp differences when dedicated lag columns are NULL",
        ],
    ))
