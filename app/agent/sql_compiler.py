"""Compile validated semantic plans into safe parameterized SQL."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.schemas import AgentPlan, CompiledQuery
from app.api.deps import _compare_window, _resolve_date_range
from app.core.config import get_settings
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.filters import build_compare_where_clause, build_where_clause
from app.registry.metrics import METRIC_REGISTRY

settings = get_settings()

_TIME_BUCKET_SQL: dict[str, str] = {
    "day": "DATE_TRUNC('day', TO_TIMESTAMP({column}) AT TIME ZONE 'UTC')",
    "week": "DATE_TRUNC('week', TO_TIMESTAMP({column}) AT TIME ZONE 'UTC')",
    "month": "DATE_TRUNC('month', TO_TIMESTAMP({column}) AT TIME ZONE 'UTC')",
    "quarter": "DATE_TRUNC('quarter', TO_TIMESTAMP({column}) AT TIME ZONE 'UTC')",
    "year": "DATE_TRUNC('year', TO_TIMESTAMP({column}) AT TIME ZONE 'UTC')",
}

_DIRECT_BRIDGE_JOIN = "LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id"


@dataclass
class _FilterAdapter:
    date_range: str = "all"
    client: str | None = None
    channel: str | None = None
    language: str | None = None
    team_member: str | None = None
    input_type: str | None = None
    output_type: str | None = None
    published_flag: bool | None = None
    published_platform: str | None = None
    billable_flag: bool | None = None
    compare_mode: str | None = None

    def __post_init__(self) -> None:
        self.date_from = None
        self.date_to = None
        self.compare_date_from = None
        self.compare_date_to = None
        self.compare_period_label = ""
        if self.date_range and self.date_range != "all":
            self.date_from, self.date_to = _resolve_date_range(self.date_range)
        if self.compare_mode:
            (
                self.compare_date_from,
                self.compare_date_to,
                self.compare_period_label,
            ) = _compare_window(self.compare_mode, self.date_from, self.date_to)


class AgentSQLCompiler:
    """Build SQL only from the semantic registry, never from model-authored SQL."""

    def compile(
        self,
        plan: AgentPlan,
        *,
        allowed_client_slugs: tuple[str, ...] = (),
    ) -> CompiledQuery:
        time_column = self._resolve_time_column(plan)
        if plan.compare_mode:
            return self._compile_comparison(plan, time_column, allowed_client_slugs)
        return self._compile_standard(plan, time_column, allowed_client_slugs)

    def _compile_standard(
        self,
        plan: AgentPlan,
        time_column: str,
        allowed_client_slugs: tuple[str, ...],
    ) -> CompiledQuery:
        select_parts, join_parts, group_by_parts, dimension_aliases, time_axis = self._shape_query(
            plan,
            time_column,
        )
        params: dict[str, Any] = {}

        for metric_name in plan.metrics:
            metric_def = METRIC_REGISTRY[metric_name]
            select_parts.append(f"{metric_def.formula_sql.format(alias='fv')} AS {metric_name}")

        filter_context = _FilterAdapter(**plan.filters, compare_mode=plan.compare_mode)
        where_clauses, where_params = build_where_clause(
            filter_context,
            date_column=f"fv.{time_column}",
        )
        params.update(where_params)
        scope_sql, scope_params = self._scope_clause(allowed_client_slugs)
        where_clauses.extend(scope_sql)
        params.update(scope_params)

        sql = self._assemble_sql(
            select_parts=select_parts,
            join_parts=join_parts,
            where_clauses=where_clauses,
            group_by_parts=group_by_parts,
            order_by_parts=self._order_by(plan),
            limit=min(plan.limit, settings.AGENT_MAX_LIMIT),
        )

        return CompiledQuery(
            sql=sql,
            params=params,
            metrics=plan.metrics,
            dimensions=dimension_aliases,
            limit=min(plan.limit, settings.AGENT_MAX_LIMIT),
            join_count=len(join_parts),
            time_axis=time_axis,
            time_column=time_column,
        )

    def _compile_comparison(
        self,
        plan: AgentPlan,
        time_column: str,
        allowed_client_slugs: tuple[str, ...],
    ) -> CompiledQuery:
        filter_context = _FilterAdapter(**plan.filters, compare_mode=plan.compare_mode)
        current_where, current_params = build_where_clause(
            filter_context,
            date_column=f"fv.{time_column}",
        )
        compare_where, compare_params = build_compare_where_clause(
            filter_context,
            date_column=f"fv.{time_column}",
        )
        scope_sql, scope_params = self._scope_clause(allowed_client_slugs)
        current_where.extend(scope_sql)
        compare_where.extend(scope_sql)

        current_select, join_parts, current_group, dimension_aliases, time_axis = self._shape_query(
            plan,
            time_column,
        )
        compare_select, _, compare_group, _, _ = self._shape_query(plan, time_column)

        join_keys = list(dimension_aliases)
        if time_axis:
            join_keys = [time_axis, *join_keys]

        for metric_name in plan.metrics:
            metric_sql = METRIC_REGISTRY[metric_name].formula_sql.format(alias="fv")
            current_select.append(f"{metric_sql} AS {metric_name}")
            compare_select.append(f"{metric_sql} AS {metric_name}")

        current_sql = self._assemble_sql(
            select_parts=current_select,
            join_parts=join_parts,
            where_clauses=current_where,
            group_by_parts=current_group,
            order_by_parts=[],
            limit=min(plan.limit, settings.AGENT_MAX_LIMIT),
            for_cte=True,
        )
        compare_sql = self._assemble_sql(
            select_parts=compare_select,
            join_parts=join_parts,
            where_clauses=compare_where,
            group_by_parts=compare_group,
            order_by_parts=[],
            limit=min(plan.limit, settings.AGENT_MAX_LIMIT),
            for_cte=True,
        )

        select_cols = []
        join_predicates = []
        for key in join_keys:
            select_cols.append(f"COALESCE(curr.{key}, cmp.{key}) AS {key}")
            join_predicates.append(f"curr.{key} IS NOT DISTINCT FROM cmp.{key}")
        for metric_name in plan.metrics:
            select_cols.append(f"curr.{metric_name} AS {metric_name}")
            select_cols.append(f"cmp.{metric_name} AS comparison_{metric_name}")
            select_cols.append(
                f"CASE WHEN cmp.{metric_name} IS NULL OR cmp.{metric_name} = 0 "
                f"THEN NULL ELSE ((curr.{metric_name} - cmp.{metric_name})::float / cmp.{metric_name}) * 100 END "
                f"AS delta_{metric_name}_pct"
            )

        if join_predicates:
            from_sql = f"FROM curr FULL OUTER JOIN cmp ON {' AND '.join(join_predicates)}"
        else:
            from_sql = "FROM curr CROSS JOIN cmp"
        sql = (
            f"WITH curr AS ({current_sql}), cmp AS ({compare_sql}) "
            f"SELECT {', '.join(select_cols)} "
            f"{from_sql} "
            f"{self._final_order_sql(plan)} LIMIT {min(plan.limit, settings.AGENT_MAX_LIMIT)}"
        )

        params = {**current_params, **compare_params, **scope_params}
        return CompiledQuery(
            sql=" ".join(sql.split()),
            params=params,
            metrics=plan.metrics,
            dimensions=dimension_aliases,
            limit=min(plan.limit, settings.AGENT_MAX_LIMIT),
            join_count=len(join_parts),
            time_axis=time_axis,
            time_column=time_column,
        )

    def _shape_query(
        self,
        plan: AgentPlan,
        time_column: str,
    ) -> tuple[list[str], list[str], list[str], list[str], str | None]:
        select_parts: list[str] = []
        join_parts: list[str] = []
        group_by_parts: list[str] = []
        dimension_aliases: list[str] = []
        needs_bridge = any(METRIC_REGISTRY[m].requires_bridge for m in plan.metrics)

        if plan.time_grain != "all":
            time_sql = _TIME_BUCKET_SQL[plan.time_grain].format(column=f"fv.{time_column}")
            select_parts.append(f"{time_sql} AS time")
            group_by_parts.append(time_sql)
            time_axis = "time"
        else:
            time_axis = None

        for index, dimension_name in enumerate(plan.dimensions, start=1):
            dim_def = DIMENSION_REGISTRY[dimension_name]
            alias = f"d{index}"
            if dim_def.join_template:
                rendered_join = dim_def.join_sql(alias)
                if rendered_join not in join_parts:
                    join_parts.append(rendered_join)
            rendered_name = dim_def.name_sql(alias)
            select_parts.append(f"{rendered_name} AS {dimension_name}")
            group_by_parts.append(rendered_name)
            dimension_aliases.append(dimension_name)

        if needs_bridge and not any("fact_video_output_type fvot" in join for join in join_parts):
            join_parts.append(_DIRECT_BRIDGE_JOIN)

        if len(join_parts) > settings.AGENT_MAX_JOINS:
            raise ValueError("Compiled query exceeds the maximum allowed join count.")

        return select_parts, join_parts, group_by_parts, dimension_aliases, time_axis

    def _resolve_time_column(self, plan: AgentPlan) -> str:
        if not plan.metrics:
            return "uploaded_at"
        time_columns = sorted({METRIC_REGISTRY[metric].default_time_column for metric in plan.metrics})
        if len(time_columns) > 1:
            raise ValueError(
                "Selected metrics use multiple default time anchors and cannot share one compiled time column."
            )
        return time_columns[0]

    def _assemble_sql(
        self,
        *,
        select_parts: list[str],
        join_parts: list[str],
        where_clauses: list[str],
        group_by_parts: list[str],
        order_by_parts: list[str],
        limit: int,
        for_cte: bool = False,
    ) -> str:
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        group_sql = f"GROUP BY {', '.join(group_by_parts)}" if group_by_parts else ""
        order_sql = f"ORDER BY {', '.join(order_by_parts)}" if order_by_parts else ""
        sql = (
            "SELECT "
            + ", ".join(select_parts)
            + " FROM fact_video fv "
            + " ".join(join_parts)
            + " "
            + where_sql
            + " "
            + group_sql
            + " "
            + order_sql
        ).strip()
        if not for_cte:
            sql += f" LIMIT {limit}"
        return " ".join(sql.split())

    def _order_by(self, plan: AgentPlan) -> list[str]:
        order_by_parts: list[str] = []
        for sort_rule in plan.order_by:
            direction = "DESC" if sort_rule.direction == "desc" else "ASC"
            order_by_parts.append(f"{sort_rule.field} {direction}")
        if not order_by_parts:
            if plan.time_grain != "all":
                order_by_parts.append("time ASC")
            elif plan.compare_mode and plan.metrics:
                order_by_parts.append(f"delta_{plan.metrics[0]}_pct DESC NULLS LAST")
                order_by_parts.append(f"{plan.metrics[0]} DESC NULLS LAST")
            elif plan.metrics and (plan.intent in {"top_n", "breakdown", "comparison"}):
                order_by_parts.append(f"{plan.metrics[0]} DESC")
        return order_by_parts

    def _final_order_sql(self, plan: AgentPlan) -> str:
        order_by = self._order_by(plan)
        return f"ORDER BY {', '.join(order_by)}" if order_by else ""

    def _scope_clause(self, allowed_client_slugs: tuple[str, ...]) -> tuple[list[str], dict[str, Any]]:
        if not allowed_client_slugs:
            return [], {}
        params: dict[str, Any] = {}
        bind_names: list[str] = []
        for index, slug in enumerate(allowed_client_slugs):
            key = f"allowed_client_slug_{index}"
            params[key] = slug
            bind_names.append(f":{key}")
        clause = (
            "fv.client_id IN (SELECT id FROM dim_client WHERE slug IN ("
            + ", ".join(bind_names)
            + "))"
        )
        return [clause], params
