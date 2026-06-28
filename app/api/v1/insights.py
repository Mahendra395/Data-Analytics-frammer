"""Insight & recommendation endpoints.

GET /insights/summary   → top risks, opportunities, drivers, executive summary
GET /insights/risks     → filtered risk items
GET /insights/opportunities → filtered opportunity items
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.schemas.insights import InsightItem, InsightResponse
from app.schemas.responses import ApiResponse
from app.services.insight_engine import collect_insight_context
from app.services.insight_llm import generate_insights
from app.utils.response import wrap

router = APIRouter(tags=["Insights"])


@router.get("/summary", response_model=ApiResponse[InsightResponse])
async def insight_summary(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[InsightResponse]:
    """Top risks, opportunities, likely drivers, and executive summary."""
    context = await collect_insight_context(db, f)
    data = await generate_insights(context)
    return wrap(
        data, f,
        metrics=["publish_rate", "dq_score", "total_uploaded", "total_published"],
        grain="insight-aggregated",
        caveats=[
            "Insights are generated from the current filter scope",
            "Severity thresholds: critical < 30% conversion or < 60 DQ, warning < 50% conversion or < 80 DQ",
        ],
    )


@router.get("/risks", response_model=ApiResponse[List[InsightItem]])
async def insight_risks(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[InsightItem]]:
    """Filtered list of risk items with drill-down context."""
    context = await collect_insight_context(db, f)
    data = await generate_insights(context)
    return wrap(
        data.top_risks, f,
        metrics=["publish_rate", "dq_score"],
        grain="insight-aggregated",
    )


@router.get("/opportunities", response_model=ApiResponse[List[InsightItem]])
async def insight_opportunities(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[InsightItem]]:
    """Filtered list of opportunity items."""
    context = await collect_insight_context(db, f)
    data = await generate_insights(context)
    return wrap(
        data.top_opportunities, f,
        metrics=["total_uploaded", "total_published"],
        grain="insight-aggregated",
    )
