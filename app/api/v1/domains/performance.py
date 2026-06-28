"""Performance domain: clients, channels, users, teams, analytics, billable."""
from fastapi import APIRouter

from app.api.v1 import analytics, channels, clients, teams, users
from app.api.v1.domains import billable_deep

router = APIRouter()

router.include_router(clients.router)
router.include_router(channels.router)
router.include_router(users.router)
router.include_router(teams.router)
router.include_router(analytics.router)
router.include_router(billable_deep.router)
