"""Diagnostics domain: data quality, benchmarks, concentration, backlog.

diagnostics_analytics and diagnostics_backlog have no built-in prefix,
so their routes surface at the domain root (e.g. /diagnostics/concentration).
"""
from fastapi import APIRouter

from app.api.v1 import diagnostics_analytics, diagnostics_backlog, quality

router = APIRouter()

router.include_router(quality.router)
router.include_router(diagnostics_analytics.router)
router.include_router(diagnostics_backlog.router)
