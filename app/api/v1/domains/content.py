"""Content domain: input types, output types, languages, platform/language deep."""
from fastapi import APIRouter

from app.api.v1 import input_types, languages, output_types
from app.api.v1.domains import language_deep, platform_deep

router = APIRouter()

router.include_router(input_types.router)
router.include_router(output_types.router)
router.include_router(languages.router)
router.include_router(language_deep.router)
router.include_router(platform_deep.router)
