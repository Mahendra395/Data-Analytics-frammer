"""Frammer Analytics API – FastAPI application entry point."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "Production analytics API for the Frammer AI media operations platform. "
        "Provides aggregated video processing, publishing, and team metrics."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ── CORS ───────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Deprecation header middleware ───────────────────────────────────────────────
# Routes at the old flat paths (/api/v1/{resource}) are served for backward
# compatibility. This middleware detects them and adds advisory headers so
# clients can migrate to the domain-grouped canonical paths.
_V1 = settings.API_V1_PREFIX  # e.g. "/api/v1"

_DEPRECATED_PATH_MAP: dict[str, str] = {
    f"{_V1}/kpis":               f"{_V1}/core/kpis",
    f"{_V1}/monthly":            f"{_V1}/trends/monthly",
    f"{_V1}/funnel":             f"{_V1}/funnel-efficiency/funnel",
    f"{_V1}/growth":             f"{_V1}/trends/growth",
    f"{_V1}/lag":                f"{_V1}/funnel-efficiency/lag",
    f"{_V1}/multi-dimensional":  f"{_V1}/detail/multi-dimensional",
    f"{_V1}/analytics":          f"{_V1}/performance/analytics",
    f"{_V1}/concentration":      f"{_V1}/diagnostics/concentration",
    f"{_V1}/benchmarks":         f"{_V1}/diagnostics/benchmarks",
    f"{_V1}/backlog":            f"{_V1}/diagnostics/backlog",
    f"{_V1}/aging":              f"{_V1}/diagnostics/aging",
    f"{_V1}/channels":           f"{_V1}/performance/channels",
    f"{_V1}/users":              f"{_V1}/performance/users",
    f"{_V1}/teams":              f"{_V1}/performance/teams",
    f"{_V1}/languages":          f"{_V1}/content/languages",
    f"{_V1}/input-types":        f"{_V1}/content/input-types",
    f"{_V1}/output-types":       f"{_V1}/content/output-types",
    f"{_V1}/videos":             f"{_V1}/detail/videos",
    f"{_V1}/publishing":         f"{_V1}/funnel-efficiency/publishing",
    f"{_V1}/quality":            f"{_V1}/diagnostics/quality",
    f"{_V1}/clients":            f"{_V1}/performance/clients",
    f"{_V1}/forecast":           f"{_V1}/trends/forecast",
    f"{_V1}/query":              f"{_V1}/detail/query",
    f"{_V1}/dimensions":         f"{_V1}/core/dimensions",
    f"{_V1}/processing":         f"{_V1}/funnel-efficiency/processing",
    f"{_V1}/registry":           f"{_V1}/core/registry",
}


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    """Adds X-Deprecated-Endpoint and X-New-Endpoint headers for legacy paths."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        path = request.url.path
        for old_prefix, new_path in _DEPRECATED_PATH_MAP.items():
            if path == old_prefix or path.startswith(old_prefix + "/"):
                response.headers["X-Deprecated-Endpoint"] = "true"
                response.headers["X-New-Endpoint"] = new_path
                response.headers["Deprecation"] = "true"
                break
        return response


app.add_middleware(DeprecationHeaderMiddleware)

# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["Health"])
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "frammer-api"})


@app.get("/", tags=["Health"], include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse({"message": "Frammer Analytics API", "docs": "/docs"})
