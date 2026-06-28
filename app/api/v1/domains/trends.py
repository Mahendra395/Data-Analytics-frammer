"""Trends domain: monthly time series, growth tracking, forecast."""
from fastapi import APIRouter

from app.api.v1 import forecast, growth, monthly

router = APIRouter()

router.include_router(monthly.router)
router.include_router(growth.router)
router.include_router(forecast.router)
