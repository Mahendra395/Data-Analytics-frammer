"""Central API v1 router.

Domain-grouped routes (new canonical URLs):
  /core/          auth, kpis, dimensions, registry
  /trends/        monthly, growth, forecast
  /performance/   clients, channels, users, teams, analytics
  /funnel-efficiency/  funnel, lag, processing, publishing
  /content/       input-types, output-types, languages
  /diagnostics/   quality, benchmarks, concentration, backlog
  /detail/        videos, query, multi-dimensional

Deprecated flat routes (old URLs) are kept with X-Deprecated-Endpoint headers
so existing frontend consumers continue to work during the transition period.
"""
from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.v1 import (
    analytics,
    auth,
    channels,
    clients,
    diagnostics_analytics,
    diagnostics_backlog,
    dimensions,
    forecast,
    funnel,
    growth,
    input_types,
    kpis,
    lag,
    languages,
    monthly,
    multi_dimensional,
    output_types,
    processing,
    publishing,
    quality,
    query,
    registry,
    teams,
    users,
    videos,
)
from app.api.v1 import internal as internal_notifications
from app.api.v1 import notifications
from app.api.v1.agent import routes as agent
from app.api.v1 import insights as insights_api
from app.api.v1 import anomalies as anomalies_api
from app.api.v1 import scores as scores_api
from app.api.v1.domains import (
    content,
    core,
    detail,
    diagnostics,
    funnel_efficiency,
    performance,
    trends,
)

api_router = APIRouter()

# ── Public routes (no auth required) ──────────────────────────────────────────
api_router.include_router(auth.router)

# ── Protected dependency shorthand ─────────────────────────────────────────────
_protected = {"dependencies": [Depends(get_current_user)]}

# ══════════════════════════════════════════════════════════════════════════════
# NEW CANONICAL DOMAIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

api_router.include_router(core.router,             prefix="/core",              **_protected)
api_router.include_router(trends.router,           prefix="/trends",            **_protected)
api_router.include_router(performance.router,      prefix="/performance",       **_protected)
api_router.include_router(funnel_efficiency.router, prefix="/funnel-efficiency", **_protected)
api_router.include_router(content.router,          prefix="/content",           **_protected)
api_router.include_router(diagnostics.router,      prefix="/diagnostics",       **_protected)
api_router.include_router(detail.router,           prefix="/detail",            **_protected)
api_router.include_router(insights_api.router,     prefix="/insights",          **_protected)
api_router.include_router(anomalies_api.router,    prefix="/insights",          **_protected)
api_router.include_router(scores_api.router,       prefix="/diagnostics",       **_protected)
api_router.include_router(agent.router,            **_protected)
api_router.include_router(notifications.router,    prefix="/notifications",  **_protected)
api_router.include_router(internal_notifications.router, prefix="/internal/notifications")

# ══════════════════════════════════════════════════════════════════════════════
# DEPRECATED FLAT ROUTES  (old /api/v1/{resource} URLs)
# These are retained for backward compatibility. Consumers should migrate to
# the domain-grouped paths above. Each response carries:
#   X-Deprecated-Endpoint: true
#   X-New-Endpoint: <canonical path>
# ══════════════════════════════════════════════════════════════════════════════

def _deprecated(new_path: str) -> dict:
    """Build include_router kwargs that mark routes as deprecated."""
    return {**_protected, "deprecated": True}


api_router.include_router(kpis.router,              **_deprecated("/core/kpis"))
api_router.include_router(monthly.router,            **_deprecated("/trends/monthly"))
api_router.include_router(funnel.router,             **_deprecated("/funnel-efficiency/funnel"))
api_router.include_router(growth.router,             **_deprecated("/trends/growth"))
api_router.include_router(lag.router,                **_deprecated("/funnel-efficiency/lag"))
api_router.include_router(multi_dimensional.router,  **_deprecated("/detail/multi-dimensional"))
api_router.include_router(analytics.router,          **_deprecated("/performance/analytics"))
api_router.include_router(diagnostics_analytics.router, **_deprecated("/diagnostics"))
api_router.include_router(diagnostics_backlog.router, **_deprecated("/diagnostics"))
api_router.include_router(channels.router,           **_deprecated("/performance/channels"))
api_router.include_router(users.router,              **_deprecated("/performance/users"))
api_router.include_router(teams.router,              **_deprecated("/performance/teams"))
api_router.include_router(languages.router,          **_deprecated("/content/languages"))
api_router.include_router(input_types.router,        **_deprecated("/content/input-types"))
api_router.include_router(output_types.router,       **_deprecated("/content/output-types"))
api_router.include_router(videos.router,             **_deprecated("/detail/videos"))
api_router.include_router(publishing.router,         **_deprecated("/funnel-efficiency/publishing"))
api_router.include_router(quality.router,            **_deprecated("/diagnostics/quality"))
api_router.include_router(clients.router,            **_deprecated("/performance/clients"))
api_router.include_router(forecast.router,           **_deprecated("/trends/forecast"))
api_router.include_router(query.router,              **_deprecated("/detail/query"))
api_router.include_router(dimensions.router,         **_deprecated("/core/dimensions"))
api_router.include_router(processing.router,         **_deprecated("/funnel-efficiency/processing"))
api_router.include_router(registry.router,           **_deprecated("/core/registry"))
