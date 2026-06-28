"""Anomaly detection and waterfall (contribution-to-change) endpoints.

GET /insights/anomalies              → detected anomalies across all dimensions
GET /insights/anomalies/{dimension}  → anomalies for a specific dimension
GET /insights/waterfall              → contribution-to-change waterfall chart data
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.schemas.insights import AnomalyResponse, WaterfallResponse
from app.schemas.responses import ApiResponse
from app.services.anomaly import compute_waterfall, detect_anomalies
from app.utils.response import wrap

router = APIRouter(tags=["Anomalies"])


@router.get("/anomalies", response_model=ApiResponse[AnomalyResponse])
async def get_anomalies(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AnomalyResponse]:
    """Detected anomalies across all metric × dimension combinations."""
    data = await detect_anomalies(db, f)
    return wrap(
        data, f,
        metrics=["total_uploaded", "total_published", "publish_rate"],
        grain="anomaly-detection",
        caveats=[
            "Anomalies flagged when MoM change > 50% or deviation from portfolio avg > 2.5x",
            "Only segments with volume >= 5 are evaluated",
        ],
    )


@router.get("/anomalies/{dimension}", response_model=ApiResponse[AnomalyResponse])
async def get_anomalies_by_dimension(
    dimension: str = Path(description="channel | user | language | client | input_type | output_type"),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AnomalyResponse]:
    """Anomalies for a specific dimension."""
    data = await detect_anomalies(db, f, dimension=dimension)
    return wrap(
        data, f,
        metrics=["total_uploaded", "total_published", "publish_rate"],
        grain="anomaly-detection",
    )


@router.get("/waterfall", response_model=ApiResponse[WaterfallResponse])
async def get_waterfall(
    metric: str = Query(
        default="uploaded",
        description="uploaded | published | duration_hrs | publish_rate",
    ),
    dimension: str = Query(
        default="channel",
        description="channel | client | user | language | input_type | output_type",
    ),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[WaterfallResponse]:
    """Contribution-to-change waterfall for the specified metric × dimension."""
    data = await compute_waterfall(db, f, metric=metric, dimension=dimension)
    return wrap(
        data, f,
        metrics=[metric],
        grain="waterfall-analysis",
        caveats=["Waterfall shows MoM delta decomposition by segment"],
    )
