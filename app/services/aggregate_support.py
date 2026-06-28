"""Helpers for routing analytics endpoints to aggregate tables."""
from __future__ import annotations

from datetime import date
from typing import Tuple

from app.api.deps import FilterParams


def _client_supported(client: str | None) -> bool:
    return client in (None, "client-1", "CLIENT 1")


def _has_date_filter(f: FilterParams) -> bool:
    return f.date_from is not None or f.date_to is not None


def _month_clause(
    alias: str,
    date_from: date | None,
    date_to: date | None,
) -> tuple[list[str], dict]:
    clauses: list[str] = []
    params: dict = {}
    if date_from:
        clauses.append(f"({alias}.year > :from_year OR ({alias}.year = :from_year AND {alias}.month >= :from_month))")
        params["from_year"] = date_from.year
        params["from_month"] = date_from.month
    if date_to:
        clauses.append(f"({alias}.year < :to_year OR ({alias}.year = :to_year AND {alias}.month <= :to_month))")
        params["to_year"] = date_to.year
        params["to_month"] = date_to.month
    return clauses, params


def monthly_aggregate_filters(
    f: FilterParams,
    *,
    alias: str = "ams",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[str, dict]:
    clauses, params = _month_clause(alias, date_from if date_from is not None else f.date_from, date_to if date_to is not None else f.date_to)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def supports_monthly_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and f.channel is None
        and f.language is None
        and f.team_member is None
        and f.input_type is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_kpi_aggregate(f: FilterParams) -> bool:
    return supports_monthly_aggregate(f) and not _has_date_filter(f) and not f.compare_mode


def supports_channel_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.language is None
        and f.team_member is None
        and f.input_type is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_channel_user_aggregate(f: FilterParams) -> bool:
    return supports_channel_aggregate(f)


def supports_user_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.channel is None
        and f.language is None
        and f.input_type is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_language_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.channel is None
        and f.team_member is None
        and f.input_type is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_input_type_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.channel is None
        and f.language is None
        and f.team_member is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_output_type_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.channel is None
        and f.language is None
        and f.team_member is None
        and f.input_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )


def supports_publishing_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.language is None
        and f.team_member is None
        and f.input_type is None
        and f.output_type is None
        and f.published_platform is None
        and f.billable_flag is None
        and f.published_flag in (None, True)
    )


def supports_team_aggregate(f: FilterParams) -> bool:
    return (
        _client_supported(f.client)
        and not _has_date_filter(f)
        and f.language is None
        and f.team_member is None
        and f.input_type is None
        and f.output_type is None
        and f.published_flag is None
        and f.published_platform is None
        and f.billable_flag is None
    )
