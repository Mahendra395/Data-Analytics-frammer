"""Semantic validation for agent plans."""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.schemas import AgentPlan, AgentValidationIssue, NON_SQL_INTENTS
from app.core.config import get_settings
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY, MetricDef

_UNSUPPORTED_METRICS = frozenset({"mom_growth_pct", "health_score", "productivity_index"})
_MAX_DIMENSIONS = 2
_VALID_DATE_RANGES = frozenset({"last_7d", "last_30d", "last_90d", "this_month", "last_month", "ytd", "all", "all_data", "custom"})
settings = get_settings()


@dataclass(frozen=True)
class ValidationResult:
    plan: AgentPlan
    caveats: list[str]
    follow_ups: list[str]
    issues: list[AgentValidationIssue]

    @property
    def is_valid(self) -> bool:
        return not self.issues


class AgentPlanValidator:
    """Registry-backed validator for semantic plans."""

    def validate(self, plan: AgentPlan) -> ValidationResult:
        # Non-SQL intents skip SQL-oriented validation entirely.
        if plan.intent in NON_SQL_INTENTS:
            return ValidationResult(plan=plan, caveats=[], follow_ups=[], issues=[])

        issues: list[AgentValidationIssue] = []
        caveats: list[str] = []
        follow_ups: list[str] = []
        metric_time_columns: set[str] = set()

        if len(plan.dimensions) > _MAX_DIMENSIONS:
            issues.append(AgentValidationIssue(
                field="dimensions",
                code="too_many_dimensions",
                message="The MVP compiler supports up to 2 dimensions per query.",
            ))

        if plan.limit > settings.AGENT_MAX_LIMIT:
            issues.append(AgentValidationIssue(
                field="limit",
                code="limit_too_high",
                message=f"limit must be <= {settings.AGENT_MAX_LIMIT}.",
            ))

        date_range = plan.filters.get("date_range")
        if date_range is not None and date_range not in _VALID_DATE_RANGES:
            issues.append(AgentValidationIssue(
                field="filters.date_range",
                code="invalid_date_range",
                message=f"Unsupported date_range '{date_range}'.",
            ))

        if plan.compare_mode is not None and not plan.filters.get("date_range"):
            issues.append(AgentValidationIssue(
                field="compare_mode",
                code="missing_date_range",
                message="Comparison mode requires a resolved date_range filter.",
            ))

        for metric_name in plan.metrics:
            metric_def = METRIC_REGISTRY.get(metric_name)
            if metric_def is None:
                issues.append(AgentValidationIssue(
                    field="metrics",
                    code="unknown_metric",
                    message=f"Metric '{metric_name}' is not registered.",
                ))
                continue

            if metric_name in _UNSUPPORTED_METRICS:
                issues.append(AgentValidationIssue(
                    field="metrics",
                    code="metric_not_compilable",
                    message=f"Metric '{metric_name}' requires a dedicated query builder and is not in the generic compiler yet.",
                ))
                continue

            metric_time_columns.add(metric_def.default_time_column)
            caveats.extend(self._metric_caveats(metric_def))
            for dimension_name in plan.dimensions:
                if not self._is_dimension_allowed(metric_def, dimension_name):
                    issues.append(AgentValidationIssue(
                        field="dimensions",
                        code="invalid_dimension_for_metric",
                        message=f"Metric '{metric_name}' cannot be sliced by '{dimension_name}'.",
                    ))

            if plan.time_grain not in metric_def.valid_time_grains:
                issues.append(AgentValidationIssue(
                    field="time_grain",
                    code="invalid_time_grain",
                    message=f"Metric '{metric_name}' does not support time grain '{plan.time_grain}'.",
                ))

        for dimension_name in plan.dimensions:
            if dimension_name not in DIMENSION_REGISTRY:
                issues.append(AgentValidationIssue(
                    field="dimensions",
                    code="unknown_dimension",
                    message=f"Dimension '{dimension_name}' is not registered.",
                ))

        if not plan.metrics:
            issues.append(AgentValidationIssue(
                field="metrics",
                code="missing_metric",
                message="At least one metric is required.",
            ))

        if len(metric_time_columns) > 1 and (
            plan.time_grain != "all" or plan.filters.get("date_range")
        ):
            issues.append(AgentValidationIssue(
                field="metrics",
                code="mixed_time_anchors",
                message="Selected metrics use different default time anchors and cannot share one time window in the generic compiler.",
            ))

        allowed_sort_fields = set(plan.metrics) | set(plan.dimensions) | {"time"}
        if plan.compare_mode:
            for metric_name in plan.metrics:
                allowed_sort_fields.add(f"comparison_{metric_name}")
                allowed_sort_fields.add(f"delta_{metric_name}_pct")
        for sort_rule in plan.order_by:
            if sort_rule.field not in allowed_sort_fields:
                issues.append(AgentValidationIssue(
                    field="order_by",
                    code="unknown_sort_field",
                    message=f"Sort field '{sort_rule.field}' is not part of the selected result shape.",
                ))

        if plan.chart is not None:
            allowed_fields = set(plan.metrics) | set(plan.dimensions) | {"time"}
            if plan.chart.x and plan.chart.x not in allowed_fields:
                issues.append(AgentValidationIssue(
                    field="chart.x",
                    code="unknown_chart_field",
                    message=f"Chart x field '{plan.chart.x}' is not present in the plan output.",
                ))
            if plan.chart.y and plan.chart.y not in allowed_fields:
                issues.append(AgentValidationIssue(
                    field="chart.y",
                    code="unknown_chart_field",
                    message=f"Chart y field '{plan.chart.y}' is not present in the plan output.",
                ))

        if plan.intent == "explain_metric" and plan.metrics:
            follow_ups.append(f"Ask for a trend of {plan.metrics[0]} over time.")
        elif plan.dimensions and plan.metrics:
            follow_ups.append(f"Break down {plan.metrics[0]} by a different dimension.")
        elif plan.metrics:
            follow_ups.append(f"Show {plan.metrics[0]} by client.")

        return ValidationResult(
            plan=plan,
            caveats=list(dict.fromkeys(item for item in caveats if item)),
            follow_ups=list(dict.fromkeys(follow_ups)),
            issues=issues,
        )

    def auto_repair(self, plan: AgentPlan) -> tuple[AgentPlan, list[str]]:
        """Best-effort deterministic repair of a plan with validation issues.

        Returns the repaired plan and a list of human-readable changes made.
        This does NOT guarantee the plan will be valid — always re-validate.
        """
        changes: list[str] = []
        updates: dict = {}

        # 1. Remove unsupported / unknown metrics
        valid_metrics = [
            m for m in plan.metrics
            if m in METRIC_REGISTRY and m not in _UNSUPPORTED_METRICS
        ]
        removed_metrics = set(plan.metrics) - set(valid_metrics)
        if removed_metrics:
            changes.append(f"Removed unsupported metrics: {', '.join(sorted(removed_metrics))}")
            updates["metrics"] = valid_metrics

        working_metrics = updates.get("metrics", list(plan.metrics))

        # 2. Remove unknown dimensions
        valid_dims = [d for d in plan.dimensions if d in DIMENSION_REGISTRY]
        removed_dims = set(plan.dimensions) - set(valid_dims)
        if removed_dims:
            changes.append(f"Removed unknown dimensions: {', '.join(sorted(removed_dims))}")

        # 3. Remove dimensions invalid for any remaining metric
        if working_metrics:
            filtered_dims: list[str] = []
            for dim in valid_dims:
                allowed_for_all = all(
                    self._is_dimension_allowed(METRIC_REGISTRY[m], dim)
                    for m in working_metrics
                    if m in METRIC_REGISTRY and m not in _UNSUPPORTED_METRICS
                )
                if allowed_for_all:
                    filtered_dims.append(dim)
                else:
                    changes.append(f"Removed dimension '{dim}' (incompatible with selected metrics)")
            valid_dims = filtered_dims

        # 4. Trim to max dimensions
        if len(valid_dims) > _MAX_DIMENSIONS:
            changes.append(f"Trimmed dimensions to first {_MAX_DIMENSIONS}: {', '.join(valid_dims[:_MAX_DIMENSIONS])}")
            valid_dims = valid_dims[:_MAX_DIMENSIONS]

        updates["dimensions"] = valid_dims

        # 5. Fix mixed time anchors — keep only metrics sharing the most common time column
        if working_metrics:
            time_groups: dict[str, list[str]] = {}
            for m in working_metrics:
                mdef = METRIC_REGISTRY.get(m)
                if mdef:
                    time_groups.setdefault(mdef.default_time_column, []).append(m)
            if len(time_groups) > 1 and (plan.time_grain != "all" or plan.filters.get("date_range")):
                largest_group = max(time_groups.values(), key=len)
                dropped = set(working_metrics) - set(largest_group)
                if dropped:
                    changes.append(f"Removed metrics with conflicting time columns: {', '.join(sorted(dropped))}")
                    updates["metrics"] = largest_group

        # 6. Fix time grain incompatibility
        final_metrics = updates.get("metrics", working_metrics)
        if final_metrics and plan.time_grain != "all":
            unsupported_grain = [
                m for m in final_metrics
                if m in METRIC_REGISTRY and plan.time_grain not in METRIC_REGISTRY[m].valid_time_grains
            ]
            if unsupported_grain:
                changes.append(f"Reset time_grain to 'all' (incompatible with {', '.join(unsupported_grain)})")
                updates["time_grain"] = "all"

        # 7. Clamp limit
        if plan.limit > settings.AGENT_MAX_LIMIT:
            changes.append(f"Clamped limit from {plan.limit} to {settings.AGENT_MAX_LIMIT}")
            updates["limit"] = settings.AGENT_MAX_LIMIT

        # 8. Fix invalid date range
        date_range = plan.filters.get("date_range")
        if date_range is not None and date_range not in _VALID_DATE_RANGES:
            new_filters = dict(plan.filters)
            new_filters["date_range"] = "all"
            changes.append(f"Reset invalid date_range '{date_range}' to 'all'")
            updates["filters"] = new_filters

        # 9. Fix order_by references
        final_metrics_set = set(updates.get("metrics", final_metrics))
        final_dims_set = set(updates.get("dimensions", valid_dims))
        allowed_sort = final_metrics_set | final_dims_set | {"time"}
        valid_order = [s for s in plan.order_by if s.field in allowed_sort]
        if len(valid_order) != len(plan.order_by):
            changes.append("Removed invalid order_by fields")
            updates["order_by"] = valid_order

        # 10. If all metrics were removed, this plan is unrecoverable
        if not updates.get("metrics", final_metrics):
            updates["intent"] = "clarification"
            updates["metrics"] = []
            updates["dimensions"] = []
            changes.append("No valid metrics remain; converted to clarification intent")

        repaired = plan.model_copy(update=updates) if updates else plan
        return repaired, changes

    def _is_dimension_allowed(self, metric_def: MetricDef, dimension_name: str) -> bool:
        if metric_def.name == "dq_score":
            return False
        if not metric_def.valid_dimensions:
            return True
        return dimension_name in metric_def.valid_dimensions

    def _metric_caveats(self, metric_def: MetricDef) -> list[str]:
        caveats: list[str] = []
        if metric_def.caveats:
            caveats.append(metric_def.caveats)
        if metric_def.is_proxy and metric_def.proxy_note:
            caveats.append(metric_def.proxy_note)
        return caveats
