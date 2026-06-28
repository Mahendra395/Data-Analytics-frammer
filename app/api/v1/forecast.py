"""GET /api/v1/forecast/{metric} — Holt-Winters ETS forecast."""
from __future__ import annotations

import math
from typing import List, Literal, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import ApiResponse, ForecastPoint, ForecastResponse
from app.utils.response import build_metadata

router = APIRouter(prefix="/forecast", tags=["Forecast"])

Metric = Literal[
    "total_uploaded",
    "total_published",
    "uploaded_duration_hrs",
    "created_duration_hrs",
]

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


@router.get("/{metric}", response_model=ApiResponse[ForecastResponse])
async def get_forecast(
    metric: Metric,
    horizon: int = Query(default=6, ge=1, le=24),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ForecastResponse]:
    """
    Returns last 12 months of actuals + horizon-month linear extrapolation.
    Confidence interval = ±1 std-error of the regression residuals.
    """
    # ── Fetch monthly actuals ──────────────────────────────────────────────────
    where, params = build_where_clause(f)
    # Always exclude rows with no upload timestamp
    base_conditions = ["fv.uploaded_at IS NOT NULL"] + where
    where_sql = "WHERE " + " AND ".join(base_conditions)

    sql = text(f"""
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(fv.uploaded_at))::int AS year,
            EXTRACT(MONTH FROM to_timestamp(fv.uploaded_at))::int AS month,
            COUNT(*)                                            AS total_uploaded,
            SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)         AS total_published,
            COALESCE(SUM(fv.uploaded_duration_sec),  0)/3600.0    AS uploaded_duration_hrs,
            COALESCE(SUM(fv.created_duration_sec),   0)/3600.0    AS created_duration_hrs
        FROM fact_video fv
        {where_sql}
        GROUP BY year, month
        ORDER BY year, month
    """)
    result = await db.execute(sql, params)
    raw = result.mappings().all()

    if not raw:
        raise HTTPException(status_code=404, detail="No historical data found.")

    # Take last 12 months
    history = list(raw)[-12:]

    actuals = [float(r[metric]) for r in history]

    if len(actuals) < 2:
        raise HTTPException(status_code=422, detail="Not enough data for forecasting.")

    # ── Holt-Winters ETS model ─────────────────────────────────────────────────
    #   >= 24 pts → additive trend + additive seasonality (period=12)
    #   6-23 pts  → additive trend only (no seasonality)
    #   2-5 pts   → simple exponential smoothing (fallback)
    n = len(actuals)
    series = np.array(actuals, dtype=float)
    model_type: str

    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing  # type: ignore[import]

        if n >= 24:
            model = ExponentialSmoothing(series, trend="add", seasonal="add", seasonal_periods=12, initialization_method="estimated")
            model_type = "Holt-Winters (trend + seasonality, period=12)"
        elif n >= 6:
            model = ExponentialSmoothing(series, trend="add", seasonal=None, initialization_method="estimated")
            model_type = "Holt-Winters (trend only)"
        else:
            model = ExponentialSmoothing(series, trend=None, seasonal=None, initialization_method="estimated")
            model_type = "Simple exponential smoothing (< 6 data points)"

        fit = model.fit(optimized=True)
        forecasted = fit.forecast(horizon)
        residuals = fit.resid
        sigma = float(np.std(residuals))

        # In-sample R² from residuals vs actuals
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((series - series.mean()) ** 2))
        model_confidence = round(max(0.0, min(1.0, 1 - ss_res / ss_tot)) if ss_tot > 0 else 0.0, 3)

    except Exception:
        # Ultimate fallback: linear extrapolation
        xs = list(range(n))
        from scipy.stats import linregress as _linreg  # type: ignore[import]
        slope, intercept, r_value, _, _ = _linreg(xs, actuals)
        residuals_lin = [actuals[i] - (slope * i + intercept) for i in xs]
        sigma = float(np.std(residuals_lin))
        forecasted = np.array([slope * (n - 1 + h) + intercept for h in range(1, horizon + 1)])
        model_confidence = round(r_value ** 2, 3)
        model_type = "Linear regression (fallback)"

    mom_growth = 0.0
    if actuals[-1] > 0 and horizon > 0:
        mom_growth = round(((float(forecasted[0]) - actuals[-1]) / actuals[-1]) * 100, 2)

    # ── Build response ─────────────────────────────────────────────────────────
    points: List[ForecastPoint] = []

    # Historical points
    for i, r in enumerate(history):
        yr  = int(r["year"])
        mo  = int(r["month"])
        points.append(
            ForecastPoint(
                month_label=f"{_MONTH_LABELS[mo]} {str(yr)[2:]}",
                year=yr,
                month=mo,
                actual=round(float(r[metric]), 2),
                is_forecast=False,
            )
        )

    # Forecast points
    last = history[-1]
    yr, mo = int(last["year"]), int(last["month"])
    for h in range(1, horizon + 1):
        mo += 1
        if mo > 12:
            mo = 1
            yr += 1
        predicted = float(forecasted[h - 1])
        upper = predicted + 1.96 * sigma
        lower = max(0.0, predicted - 1.96 * sigma)
        points.append(
            ForecastPoint(
                month_label=f"{_MONTH_LABELS[mo]} {str(yr)[2:]}",
                year=yr,
                month=mo,
                forecast=round(max(0.0, predicted), 2),
                upper=round(upper, 2),
                lower=round(lower, 2),
                is_forecast=True,
            )
        )

    data = ForecastResponse(
        metric=metric,
        horizon_months=horizon,
        monthly_growth_rate=mom_growth,
        model_confidence=model_confidence,
        data=points,
    )
    from app.schemas.responses import ApiResponse
    return ApiResponse(
        data=data,
        meta=build_metadata(
            f,
            metrics=[metric],
            grain="monthly-aggregated",
            caveats=[
                f"Forecast model: {model_type}",
                "Confidence interval = ±1.96 × in-sample residual std dev",
                "Seasonal decomposition requires ≥ 24 months of data; trend-only model used when 6–23 months available",
                "Model confidence (R²) below 0.5 indicates low predictive reliability",
            ],
            unit="count" if "duration" not in metric else "hours",
        ),
    )
