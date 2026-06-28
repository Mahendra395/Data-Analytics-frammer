"""Resolve enforced scope and merge filters for agent execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from app.agent.schemas import AgentPlan
from app.api.deps import FilterParams

_FILTER_KEYS = {
    "date_range",
    "client",
    "channel",
    "language",
    "team_member",
    "input_type",
    "output_type",
    "published_flag",
    "published_platform",
    "billable_flag",
}
_DATE_RANGE_ALIASES = {
    "last_7d": "last_7d",
    "last_7_days": "last_7d",
    "last_30d": "last_30d",
    "last_30_days": "last_30d",
    "last_90d": "last_90d",
    "last_90_days": "last_90d",
    "this_month": "this_month",
    "last_month": "last_month",
    "ytd": "ytd",
    "year_to_date": "ytd",
    "all": "all",
    "all_data": "all",
    "custom": "custom",
}


@dataclass(frozen=True)
class UserScope:
    allowed_client_slugs: tuple[str, ...]
    role: str | None = None


@dataclass(frozen=True)
class ResolvedPlanScope:
    plan: AgentPlan
    metadata_filters: dict[str, Any]
    allowed_client_slugs: tuple[str, ...]


def resolve_user_scope(current_user: dict[str, Any]) -> UserScope:
    app_metadata = current_user.get("app_metadata") or {}
    user_metadata = current_user.get("user_metadata") or {}
    tenant_scope = app_metadata.get("tenant_scope") or user_metadata.get("tenant_scope") or {}
    candidates = (
        app_metadata.get("allowed_client_slugs"),
        app_metadata.get("client_slugs"),
        app_metadata.get("clients"),
        app_metadata.get("client_slug"),
        tenant_scope.get("allowed_client_slugs"),
        tenant_scope.get("client_slugs"),
        tenant_scope.get("clients"),
        tenant_scope.get("client_slug"),
        user_metadata.get("allowed_client_slugs"),
        user_metadata.get("client_slugs"),
        user_metadata.get("clients"),
        user_metadata.get("client_slug"),
    )
    allowed: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, str):
            allowed.extend([candidate])
        elif isinstance(candidate, list):
            allowed.extend([str(item) for item in candidate if item])
    deduped = tuple(dict.fromkeys(item for item in allowed if item))
    return UserScope(allowed_client_slugs=deduped, role=current_user.get("role"))


def resolve_plan_scope(
    *,
    plan: AgentPlan,
    context: dict[str, Any],
    filter_params: FilterParams,
    current_user: dict[str, Any],
) -> ResolvedPlanScope:
    user_scope = resolve_user_scope(current_user)
    merged_filters = _request_filters(filter_params)
    merged_filters.update(_context_filters(context))
    merged_filters.update({key: value for key, value in plan.filters.items() if value is not None})
    merged_filters = _normalize_filters(merged_filters)

    compare_mode = plan.compare_mode or context.get("compare_mode") or filter_params.compare_mode

    _enforce_client_scope(merged_filters, user_scope)
    resolved_plan = plan.model_copy(
        update={
            "filters": merged_filters,
            "compare_mode": compare_mode,
        }
    )

    metadata_filters = dict(merged_filters)
    if compare_mode:
        metadata_filters["compare_mode"] = compare_mode
    if user_scope.allowed_client_slugs:
        metadata_filters["enforced_client_scope"] = list(user_scope.allowed_client_slugs)
    return ResolvedPlanScope(
        plan=resolved_plan,
        metadata_filters=metadata_filters,
        allowed_client_slugs=user_scope.allowed_client_slugs,
    )


def _request_filters(filter_params: FilterParams) -> dict[str, Any]:
    request_filters: dict[str, Any] = {}
    for key in _FILTER_KEYS:
        value = getattr(filter_params, key, None)
        if value is not None:
            request_filters[key] = value
    if filter_params.date_range and filter_params.date_range not in ("all", "all_data"):
        request_filters["date_range"] = filter_params.date_range
    return request_filters


def _context_filters(context: dict[str, Any]) -> dict[str, Any]:
    if not context:
        return {}
    filters = context.get("filters", {})
    merged: dict[str, Any] = {}
    if isinstance(filters, dict):
        for key, value in filters.items():
            if key in _FILTER_KEYS and value is not None:
                merged[key] = value
    for key in _FILTER_KEYS:
        value = context.get(key)
        if value is not None:
            merged[key] = value
    return merged


def _enforce_client_scope(filters: dict[str, Any], user_scope: UserScope) -> None:
    if not user_scope.allowed_client_slugs:
        return

    requested_client = filters.get("client")
    if requested_client and requested_client not in user_scope.allowed_client_slugs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested client is outside the authenticated user scope.",
        )
    if not requested_client and len(user_scope.allowed_client_slugs) == 1:
        filters["client"] = user_scope.allowed_client_slugs[0]


def _normalize_filters(filters: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(filters)
    date_range = normalized.get("date_range")
    if isinstance(date_range, str):
        normalized["date_range"] = _DATE_RANGE_ALIASES.get(date_range.lower(), date_range.lower())
    return normalized
