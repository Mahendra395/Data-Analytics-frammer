"""Response envelope utilities.

Usage in an endpoint::

    from app.utils.response import wrap
    from app.schemas.responses import ApiResponse, KPIResponse

    @router.get("/kpis", response_model=ApiResponse[KPIResponse])
    async def get_kpis(f: FilterParams = Depends()) -> ApiResponse[KPIResponse]:
        data = ...  # build KPIResponse
        return wrap(
            data, f,
            metrics=["total_uploaded", "publish_rate"],
            grain="video-level",
            caveats=["Comparison delta requires at least 1 prior month of data"],
            unit="count",
        )
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional, TypeVar

from app.api.deps import FilterParams
from app.schemas.responses import ApiResponse, ResponseMetadata

T = TypeVar("T")


def build_metadata(
    f: FilterParams,
    *,
    metrics: Optional[List[str]] = None,
    grain: str = "video-level",
    caveats: Optional[List[str]] = None,
    unit: Optional[str] = None,
    currency: Optional[str] = None,
) -> ResponseMetadata:
    """Build a :class:`ResponseMetadata` instance from the current filter context.

    Parameters
    ----------
    f:
        The :class:`FilterParams` dependency resolved for this request.
    metrics:
        Registry keys of the KPIs/metrics returned by this endpoint.
    grain:
        Describes the data granularity. Common values:

        * ``"video-level"``       – each row represents one video asset
        * ``"monthly-aggregated"`` – aggregated to calendar month buckets
        * ``"rule-evaluated"``    – result of DQ rule evaluation pass
        * ``"raw-sql"``           – consumer-supplied query against raw tables
        * ``"segment-aggregated"`` – grouped by a dimension (channel, user…)
    caveats:
        Human-readable notes about data limitations or proxy fields.
    unit:
        Primary unit: ``"count"``, ``"hours"``, ``"percent"``, ``"minutes"``.
    currency:
        ISO 4217 code when monetary values are present.
    """
    return ResponseMetadata(
        filters_applied=f.as_dict(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        metric_definitions_used=metrics or [],
        source_grain=grain,
        caveats=caveats or [],
        unit=unit,
        currency=currency,
    )


def wrap(data: T, f: FilterParams, **kwargs: Any) -> ApiResponse[T]:
    """Convenience wrapper that builds :class:`ApiResponse` in one call.

    All keyword arguments are forwarded to :func:`build_metadata`.

    Example::

        return wrap(my_data, f, metrics=["total_uploaded"], grain="video-level")
    """
    return ApiResponse(data=data, meta=build_metadata(f, **kwargs))
