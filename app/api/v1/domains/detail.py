"""Detail / Query domain: video explorer, SQL query sandbox, multi-dimensional analysis."""
from fastapi import APIRouter

from app.api.v1 import multi_dimensional, query, videos

router = APIRouter()

router.include_router(videos.router)
router.include_router(query.router)
router.include_router(multi_dimensional.router)
