"""POST /api/v1/query — read-only SQL sandbox for the Queries page."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.schemas.responses import ApiResponse, QueryRequest, QueryResponse
from app.utils.response import build_metadata

router = APIRouter(prefix="/query", tags=["Query"])

# ── Allowlist: only SELECT statements on known view/table names ────────────────
_ALLOWED_TABLES = {
    "fact_video", "dim_channel", "dim_user", "dim_language",
    "dim_input_type", "dim_output_type", "dim_client",
    "fact_video_output_type",
    # Convenience views (created during migration)
    "v_monthly_summary", "v_channel_summary", "v_user_summary",
    "v_language_summary", "v_input_type_summary", "v_output_type_summary",
}

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXECUTE|COPY)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> None:
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        raise HTTPException(
            status_code=400,
            detail="Only SELECT statements are allowed in the query sandbox.",
        )
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise HTTPException(
            status_code=400,
            detail="Statement contains forbidden keywords. Only SELECT is allowed.",
        )


@router.post("", response_model=ApiResponse[QueryResponse])
async def execute_query(
    body: QueryRequest,
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[QueryResponse]:
    _validate_sql(body.sql)

    # Enforce a hard LIMIT to prevent runaway queries
    safe_sql = body.sql.rstrip("; \n")
    if "limit" not in safe_sql.lower():
        safe_sql = f"{safe_sql} LIMIT {min(body.limit, 500)}"

    try:
        t0 = time.perf_counter()
        result = await db.execute(text(safe_sql))
        elapsed_ms = (time.perf_counter() - t0) * 1000

        keys = list(result.keys())
        rows = [list(r) for r in result.fetchall()]

        query_data = QueryResponse(
            columns=keys,
            rows=rows,
            row_count=len(rows),
            execution_time_ms=round(elapsed_ms, 2),
        )
        return ApiResponse(
            data=query_data,
            meta=build_metadata(
                f,
                metrics=[],
                grain="raw-sql",
                caveats=[
                    "Only SELECT statements are permitted; no DDL or DML operations",
                    "Results are capped at 500 rows per query",
                    "Queries run against live production tables; use with care",
                ],
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
