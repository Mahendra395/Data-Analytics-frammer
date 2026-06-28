"""Core domain: auth, kpis, dimensions, filters (registry).

Sub-routers carry their own URL prefix (e.g. kpis.router has prefix="/kpis"),
so they are included here without an extra prefix argument.
The domain prefix /core is applied by the central router.py.
"""
from fastapi import APIRouter

from app.api.v1 import auth, dimensions, kpis, registry

router = APIRouter()

router.include_router(auth.router)
router.include_router(kpis.router)
router.include_router(dimensions.router)
router.include_router(registry.router)
