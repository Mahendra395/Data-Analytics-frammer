"""Shared WHERE-clause builder for all analytics endpoints.

Previously each route file contained its own ``_build_where`` / ``_dim_where``
helper that duplicated the same ~50 lines of filter mapping logic.  This module
provides a single canonical implementation that every route can import.

Usage
-----
    from app.registry.filters import build_where_clause

    where_clauses, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    rows = await db.execute(text(f"SELECT ... FROM fact_video fv {where_sql}"), params)

The returned ``params`` dict uses named bind parameters compatible with
SQLAlchemy's ``text()`` queries.

Three public builder functions are provided:

build_where_clause(f)
    Dimension filters + date range from the main window.
    Used by the vast majority of aggregate endpoints.

build_dim_only_where_clause(f)
    Dimension filters only — no date range.
    Used by MoM-growth queries that manage their own date windows.

build_compare_where_clause(f)
    Dimension filters + comparison-period date range.
    Populated only when ``f.compare_mode`` is set and the main date range
    resolves a comparison window (see ``deps._compare_window``).
    Returns identical dimension filters to the main window so that both
    periods are scoped to the same segments.

Postgres-specific date handling
--------------------------------
``fact_video.uploaded_at`` is stored as epoch seconds (INTEGER).
Date filters are converted to epoch integers before binding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.api.deps import FilterParams


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _dim_filters(f: "FilterParams", exclude_dimensions: set | None = None) -> tuple[list[str], dict]:
    """Build dimension-only WHERE clauses (client, channel, language … flags).

    Internal helper consumed by all three public builders.  All SQL
    expressions assume ``fv`` as the ``fact_video`` table alias and use
    correlated sub-selects so they work regardless of which dimension tables
    the outer query joins.

    ``exclude_dimensions``: optional set of dimension names (e.g. {"user"})
    to skip, useful for benchmark peer-group queries where the benchmarked
    dimension should not be pre-filtered.
    """
    skip = exclude_dimensions or set()
    where: list[str] = []
    params: dict = {}

    if f.client and "client" not in skip:
        where.append(
            "fv.client_id IN ("
            "  SELECT id FROM dim_client"
            "  WHERE slug = :client OR name = :client"
            ")"
        )
        params["client"] = f.client

    if f.channel and "channel" not in skip:
        where.append(
            "fv.channel_id IN ("
            "  SELECT id FROM dim_channel"
            "  WHERE obfuscated_code = :channel OR name = :channel"
            ")"
        )
        params["channel"] = f.channel

    if f.language and "language" not in skip:
        where.append(
            "fv.language_id IN ("
            "  SELECT id FROM dim_language"
            "  WHERE iso_code = :language OR display_name = :language"
            ")"
        )
        params["language"] = f.language

    if f.team_member and "user" not in skip:
        where.append(
            "fv.user_id IN (SELECT id FROM dim_user WHERE name = :team_member)"
        )
        params["team_member"] = f.team_member

    if f.input_type:
        where.append(
            "fv.input_type_id IN (SELECT id FROM dim_input_type WHERE name = :input_type)"
        )
        params["input_type"] = f.input_type

    if f.output_type:
        # output_type lives on the bridge; use an EXISTS sub-select to avoid
        # row-multiplication that a direct JOIN would cause on fact_video aggregates
        where.append(
            "EXISTS ("
            "  SELECT 1 FROM fact_video_output_type fvot2 "
            "  JOIN dim_output_type dot2 ON dot2.id = fvot2.output_type_id "
            "  WHERE fvot2.video_id = fv.id AND dot2.name = :output_type"
            ")"
        )
        params["output_type"] = f.output_type

    if f.published_flag is not None:
        where.append("fv.published = :pub_flag")
        params["pub_flag"] = f.published_flag

    if f.published_platform:
        where.append("LOWER(fv.published_platform) = LOWER(:published_platform)")
        params["published_platform"] = f.published_platform

    if f.billable_flag is not None:
        where.append("fv.billable_flag = :billable_flag")
        params["billable_flag"] = f.billable_flag

    return where, params


def _epoch(d, end_of_day: bool = False) -> int:
    """Convert a ``datetime.date`` to a UTC epoch integer."""
    hour, minute, second = (23, 59, 59) if end_of_day else (0, 0, 0)
    return int(
        datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=timezone.utc).timestamp()
    )


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_where_clause(
    f: "FilterParams",
    *,
    date_column: str = "fv.uploaded_at",
) -> tuple[list[str], dict]:
    """Build a (where_clauses, params) pair from a ``FilterParams`` instance.

    Covers all dimension filters **and** the main date range.
    """
    where, params = _dim_filters(f)

    if f.date_from:
        params["date_from_epoch"] = _epoch(f.date_from)
        where.append(f"{date_column} >= :date_from_epoch")

    if f.date_to:
        params["date_to_epoch"] = _epoch(f.date_to, end_of_day=True)
        where.append(f"{date_column} <= :date_to_epoch")

    return where, params


def build_dim_only_where_clause(f: "FilterParams", exclude_dimensions: set | None = None) -> tuple[list[str], dict]:
    """Like ``build_where_clause`` but EXCLUDES date range filters.

    Useful for MoM growth queries and other queries that manage their own
    date windows and only want dimension-scoping applied globally.

    ``exclude_dimensions``: forwarded to ``_dim_filters`` to skip specific
    dimension filters (e.g. {"user"} when building benchmark peer groups).
    """
    return _dim_filters(f, exclude_dimensions=exclude_dimensions)


def build_compare_where_clause(
    f: "FilterParams",
    *,
    date_column: str = "fv.uploaded_at",
) -> tuple[list[str], dict]:
    """Build a (where_clauses, params) pair for the **comparison period**.

    Identical dimension filters to ``build_where_clause`` but the date bounds
    are taken from ``f.compare_date_from`` / ``f.compare_date_to`` (computed
    by ``deps._compare_window`` from ``f.compare_mode``).

    If ``f.compare_mode`` is ``None`` or dates could not be resolved, this
    returns the dimension-only clauses (no date restriction) — callers should
    guard with ``if f.compare_mode`` before calling.
    """
    where, params = _dim_filters(f)

    if getattr(f, "compare_date_from", None):
        params["cmp_from_epoch"] = _epoch(f.compare_date_from)
        where.append(f"{date_column} >= :cmp_from_epoch")

    if getattr(f, "compare_date_to", None):
        params["cmp_to_epoch"] = _epoch(f.compare_date_to, end_of_day=True)
        where.append(f"{date_column} <= :cmp_to_epoch")

    return where, params
