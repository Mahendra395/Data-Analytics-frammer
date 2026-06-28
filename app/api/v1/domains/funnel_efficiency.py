"""Funnel & Efficiency domain: funnel stages, lag and SLA, processing, publishing."""
from fastapi import APIRouter

from app.api.v1 import funnel, lag, processing, publishing

router = APIRouter()

router.include_router(funnel.router)
router.include_router(lag.router)
router.include_router(processing.router)
router.include_router(publishing.router)
