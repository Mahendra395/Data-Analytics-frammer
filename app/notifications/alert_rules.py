"""Threshold-based alert rule evaluators."""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams
from app.notifications.digest_builders import _filters_from_json
from app.registry.filters import build_where_clause

logger = logging.getLogger(__name__)

# Operator functions
_OPS = {
    "lt": lambda val, thr: val < thr,
    "gt": lambda val, thr: val > thr,
    "lte": lambda val, thr: val <= thr,
    "gte": lambda val, thr: val >= thr,
}


def _check(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get(operator)
    if fn is None:
        raise ValueError(f"Unknown operator: {operator}")
    return fn(value, threshold)


async def evaluate_publish_conversion_drop(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Check if publish conversion rate breaches threshold.

    Returns (triggered, current_value)."""
    f = _filters_from_json(rule.get("filters"))
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT SUM(CASE WHEN published THEN 1 ELSE 0 END)::float
                   / NULLIF(COUNT(*), 0) AS publish_rate
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    value = float(row["publish_rate"] or 0)
    triggered = _check(value, rule["comparison_operator"], rule["threshold_value"])
    return triggered, value


async def evaluate_processed_published_gap(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Check if gap between processed and published exceeds threshold."""
    f = _filters_from_json(rule.get("filters"))
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END) AS processed,
            SUM(CASE WHEN published THEN 1 ELSE 0 END) AS published
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    gap = int(row["processed"] or 0) - int(row["published"] or 0)
    value = float(gap)
    triggered = _check(value, rule["comparison_operator"], rule["threshold_value"])
    return triggered, value


async def evaluate_backlog_high(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Check if backlog (processed but not published) exceeds threshold."""
    f = _filters_from_json(rule.get("filters"))
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT COUNT(*) FILTER (WHERE is_processed AND NOT published) AS pending
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    value = float(row["pending"] or 0)
    triggered = _check(value, rule["comparison_operator"], rule["threshold_value"])
    return triggered, value


async def evaluate_dq_score_low(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Check if DQ score is below threshold."""
    f = _filters_from_json(rule.get("filters"))
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE channel_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS ch_null,
            COUNT(*) FILTER (WHERE user_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS usr_null,
            COUNT(*) FILTER (WHERE language_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS lang_null,
            COUNT(*) FILTER (WHERE input_type_id IS NULL)::float / NULLIF(COUNT(*), 0) * 100 AS it_null
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    penalties = sum(float(row[c] or 0) * 0.25 for c in ("ch_null", "usr_null", "lang_null", "it_null"))
    value = round(100.0 - penalties, 1)
    triggered = _check(value, rule["comparison_operator"], rule["threshold_value"])
    return triggered, value


async def evaluate_missing_metadata_spike(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Check if missing team/platform rate exceeds threshold."""
    f = _filters_from_json(rule.get("filters"))
    where, params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    row = (await db.execute(text(f"""
        SELECT
            (SUM(CASE WHEN missing_team_flag THEN 1 ELSE 0 END)
             + SUM(CASE WHEN missing_platform_flag THEN 1 ELSE 0 END))::float
            / NULLIF(COUNT(*) * 2, 0) * 100 AS missing_rate
        FROM fact_video fv
        {where_sql}
    """), params)).mappings().one()

    value = float(row["missing_rate"] or 0)
    triggered = _check(value, rule["comparison_operator"], rule["threshold_value"])
    return triggered, value


# ── Evaluator registry ─────────────────────────────────────────────────────────

EVALUATORS = {
    "publish_conversion_drop": evaluate_publish_conversion_drop,
    "gap_too_high": evaluate_processed_published_gap,
    "backlog_high": evaluate_backlog_high,
    "dq_low": evaluate_dq_score_low,
    "missing_metadata_spike": evaluate_missing_metadata_spike,
}


async def evaluate_rule(
    db: AsyncSession,
    rule: dict[str, Any],
) -> tuple[bool, float | None]:
    """Evaluate a single alert rule.

    Returns ``(triggered, current_value)``."""
    evaluator = EVALUATORS.get(rule["rule_type"])
    if evaluator is None:
        logger.warning("No evaluator for rule_type=%s", rule["rule_type"])
        return False, None
    return await evaluator(db, rule)
