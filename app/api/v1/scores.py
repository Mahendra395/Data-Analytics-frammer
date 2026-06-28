"""Health score endpoints.

GET /diagnostics/scores/{dimension} → per-segment health scores
GET /diagnostics/scores/overview    → summary across all dimensions
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.schemas.insights import ScoreOverviewItem, ScoreOverviewResponse, ScoreResponse
from app.schemas.responses import ApiResponse
from app.services.scoring import compute_scores
from app.utils.response import wrap

router = APIRouter(prefix="/scores", tags=["Scores"])

_DIMENSIONS = ("client", "channel", "user", "team", "language", "input_type", "output_type")


@router.get("/overview", response_model=ApiResponse[ScoreOverviewResponse])
async def scores_overview(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ScoreOverviewResponse]:
    """Summary health scores across all dimensions."""
    items = []
    total_health = 0.0
    count = 0

    for dim in _DIMENSIONS:
        score_resp = await compute_scores(db, dim, f)
        avg_health = (
            sum(s.health_score for s in score_resp.segments) / len(score_resp.segments)
            if score_resp.segments else 0
        )
        # Find worst segment
        worst_seg = min(score_resp.segments, key=lambda s: s.health_score) if score_resp.segments else None
        items.append(ScoreOverviewItem(
            dimension=dim,
            portfolio_avg_score=round(avg_health, 1),
            critical_count=score_resp.critical_count,
            warning_count=score_resp.warning_count,
            healthy_count=score_resp.healthy_count,
            worst_segment=worst_seg.segment if worst_seg else "",
            worst_score=worst_seg.health_score if worst_seg else 0,
        ))
        total_health += avg_health
        count += 1

    data = ScoreOverviewResponse(overview=items)
    return wrap(
        data, f,
        metrics=["health_score"],
        grain="score-overview",
        caveats=["Health score = 30% volume rank + 30% conversion + 20% lag + 20% SLA compliance"],
    )


@router.get("/{dimension}", response_model=ApiResponse[ScoreResponse])
async def scores_by_dimension(
    dimension: str = Path(description="client | channel | user | team | language | input_type | output_type"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ScoreResponse]:
    """Per-segment health scores for the given dimension."""
    data = await compute_scores(db, dimension, f)
    return wrap(
        data, f,
        metrics=["health_score", "total_uploaded", "publish_rate"],
        grain="score-aggregated",
        caveats=[
            "Health score = 30% volume rank + 30% conversion + 20% lag + 20% SLA compliance",
            "Risk levels: critical < 30, warning < 60, healthy >= 60",
            "Grades: A >= 80, B >= 60, C >= 40, D >= 20, F < 20",
        ],
    )
