"""Deterministic chart selection for agent query outputs."""
from __future__ import annotations

from app.agent.schemas import AgentPlan, ChartSpec
from app.core.config import get_settings
from app.registry.metrics import METRIC_REGISTRY

settings = get_settings()


class AgentChartPlanner:
    def build_chart_spec(self, plan: AgentPlan, columns: list[str], rows: list[list[object]]) -> ChartSpec | None:
        chart = plan.chart
        if chart is None:
            return None

        x = chart.x or ("time" if plan.time_grain != "all" else (plan.dimensions[0] if plan.dimensions else None))
        y = chart.y or (plan.metrics[0] if plan.metrics else None)
        series = chart.series
        if plan.compare_mode and plan.metrics and not series:
            metric = plan.metrics[0]
            series = [metric, f"comparison_{metric}"]

        row_objects = [
            {column: row[index] for index, column in enumerate(columns)}
            for row in rows
        ]
        x_values = [row.get(x) for row in row_objects] if x else []
        category_count = len({value for value in x_values if value is not None})
        is_temporal_x = bool(x == "time" or any(hasattr(value, "isoformat") for value in x_values))
        value_fields = series or ([y] if y else [])
        non_null_values = [
            row.get(field)
            for row in row_objects
            for field in value_fields
            if field is not None
        ]
        null_ratio = 1.0
        if value_fields and row_objects:
            total_slots = len(value_fields) * len(row_objects)
            populated = sum(1 for value in non_null_values if value is not None)
            null_ratio = 1 - (populated / total_slots)

        chart_type = chart.type
        if chart_type == "auto":
            if not rows:
                chart_type = "table"
            elif null_ratio > 0.5:
                chart_type = "table"
            elif len(plan.metrics) > 1 and not is_temporal_x and category_count > 12:
                chart_type = "table"
            elif plan.compare_mode and is_temporal_x and plan.metrics:
                chart_type = "line"
            elif plan.compare_mode and plan.dimensions and plan.metrics:
                chart_type = "bar"
            elif plan.time_grain != "all" and plan.metrics:
                chart_type = "line"
            elif len(plan.metrics) > 1 and category_count <= 12:
                chart_type = "bar"
            elif len(plan.metrics) > 1:
                chart_type = "table"
            elif plan.dimensions and len(rows) > settings.AGENT_MAX_CHART_CATEGORIES:
                chart_type = "table"
            elif plan.dimensions and plan.metrics:
                chart_type = "bar"
            elif plan.metrics and not plan.dimensions:
                chart_type = "stat"
            else:
                chart_type = "table"

        formatters: dict[str, str] = {}
        for metric in plan.metrics:
            formatters[metric] = METRIC_REGISTRY[metric].display_unit
            if plan.compare_mode:
                formatters[f"comparison_{metric}"] = METRIC_REGISTRY[metric].display_unit
                formatters[f"delta_{metric}_pct"] = "percent"

        return ChartSpec(
            chart_type=chart_type,
            x=x,
            y=y,
            series=series,
            title=chart.title or self._default_title(plan),
            dataset_columns=columns,
            formatters=formatters,
        )

    def _default_title(self, plan: AgentPlan) -> str:
        metric_label = plan.metrics[0].replace("_", " ") if plan.metrics else "result"
        if plan.time_grain != "all":
            return f"{metric_label.title()} over time"
        if plan.dimensions:
            return f"{metric_label.title()} by {plan.dimensions[0].replace('_', ' ')}"
        return metric_label.title()
