"""Deterministic planner that turns plain-English analytics asks into AgentPlan."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent.schemas import AgentPlan, ChartRequest, SortRule
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY

_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "total_uploaded": ("total uploaded", "uploads", "uploaded count", "uploaded videos", "how many videos"),
    "total_published": ("total published", "published count", "published videos"),
    "total_processed": ("total processed", "processed count", "processed videos"),
    "total_clips_created": ("clips created", "total clips", "total created clips"),
    "total_clips_published": ("clips published", "published clips"),
    "publish_rate": ("publish rate", "publishing rate", "conversion rate"),
    "processing_rate": ("processing rate",),
    "avg_clips_per_video": ("avg clips per video", "average clips per video"),
    "uploaded_duration_hrs": ("uploaded duration", "upload hours", "uploaded hours"),
    "created_duration_hrs": ("created duration", "created hours", "processing hours"),
    "published_duration_hrs": ("published duration", "published hours"),
    "dq_score": ("data quality score", "dq score", "quality score"),
    "avg_processing_lag_min": ("processing lag", "avg processing lag"),
    "avg_publishing_lag_min": ("publishing lag", "avg publishing lag", "turnaround time"),
}

_DIMENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "client": ("client", "clients"),
    "channel": ("channel", "channels"),
    "user": ("user", "users", "team member", "team members"),
    "team": ("team", "teams"),
    "language": ("language", "languages"),
    "input_type": ("input type", "input types"),
    "output_type": ("output type", "output types"),
    "platform": ("platform", "platforms"),
    "billable_flag": ("billable flag", "billable status"),
    "published_flag": ("published flag", "published status"),
}

_DATE_RANGE_ALIASES: dict[str, tuple[str, ...]] = {
    "last_7d": ("last 7 days", "past 7 days", "last week"),
    "last_30d": ("last 30 days", "past 30 days", "last month roughly"),
    "last_90d": ("last 90 days", "past 90 days", "last quarter roughly"),
    "this_month": ("this month", "current month"),
    "last_month": ("last month", "previous month"),
    "ytd": ("year to date", "ytd"),
}

_TIME_GRAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "day": ("daily", "by day", "per day"),
    "week": ("weekly", "by week", "per week"),
    "month": ("monthly", "by month", "per month"),
    "quarter": ("quarterly", "by quarter", "per quarter"),
    "year": ("yearly", "annually", "by year", "per year"),
}

_CHART_ALIASES: dict[str, tuple[str, ...]] = {
    "bar": ("bar chart", "bar graph", "bars"),
    "line": ("line chart", "line graph"),
    "area": ("area chart",),
    "table": ("table", "tabular"),
    "stat": ("stat", "kpi card", "single value"),
    "pie": ("pie chart", "donut", "donut chart"),
}

_TOP_N_RE = re.compile(r"\btop\s+(?P<limit>\d{1,3})\b")

# ── Non-data question patterns ────────────────────────────────────────────────
_SCHEMA_PATTERNS = (
    "data schema", "data model", "what tables", "describe the tables",
    "database structure", "schema", "what columns", "explain the schema",
    "what data do you have", "what data is available",
)

_CAPABILITIES_PATTERNS = (
    "what can you do", "help me", "how do i use", "what are your capabilities",
    "what metrics are available", "available metrics", "available dimensions",
    "what can i ask", "what questions", "how does this work",
)

_OVERVIEW_PATTERNS = (
    "explain the complete data", "explain all data", "give me an overview",
    "summarize everything", "complete overview", "full summary",
    "show me everything", "data overview", "overall summary",
    "explain the data in detail", "complete data",
)

_OVERVIEW_METRICS = ["total_uploaded", "total_published", "total_processed", "publish_rate"]


@dataclass(frozen=True)
class PlannedQuestion:
    interpreted_question: str
    plan: AgentPlan
    planner_source: str = "deterministic"
    planner_model: str | None = None
    planner_confidence: float | None = None
    planner_fallback_reason: str | None = None


class DeterministicPlanner:
    """Registry-aware fallback planner for the agent."""

    def plan(self, question: str, *, supplied_plan: AgentPlan | None = None) -> PlannedQuestion:
        if supplied_plan is not None:
            return PlannedQuestion(
                interpreted_question=question.strip(),
                plan=supplied_plan,
                planner_source="supplied_plan",
            )

        normalized = question.strip().lower()

        # ── Check for non-data questions FIRST ─────────────────────────────
        non_data = self._detect_non_data_intent(normalized)
        if non_data is not None:
            return non_data

        # ── Check for service-call patterns ────────────────────────────────
        service = self._detect_service_call(normalized)
        if service is not None:
            return service

        # ── Standard data-query planning ───────────────────────────────────
        metrics = self._match_metrics(normalized)
        dimensions = self._match_dimensions(normalized)
        filters = self._match_filters(normalized)
        time_grain = self._match_time_grain(normalized)
        chart = self._match_chart(normalized)
        limit = self._match_limit(normalized)
        intent = self._infer_intent(normalized, metrics, dimensions, time_grain, limit)

        # If no metric was matched, decide based on whether the question
        # looks like a data question at all.  Never silently default.
        if not metrics:
            if intent == "explain_metric":
                pass  # handled via explainer response downstream
            elif self._looks_like_data_question(normalized):
                # User seems to want data but we couldn't parse a metric.
                # Return clarification instead of garbage.
                return PlannedQuestion(
                    interpreted_question=question.strip(),
                    plan=AgentPlan(
                        intent="clarification",
                        metrics=[],
                        dimensions=[],
                        filters={},
                        time_grain="all",
                        chart=None,
                        explanation_level="normal",
                    ),
                    planner_source="deterministic",
                    planner_confidence=0.20,
                    planner_fallback_reason="no_metric_matched",
                )
            else:
                return PlannedQuestion(
                    interpreted_question=question.strip(),
                    plan=AgentPlan(
                        intent="clarification",
                        metrics=[],
                        dimensions=[],
                        filters={},
                        time_grain="all",
                        chart=None,
                        explanation_level="normal",
                    ),
                    planner_source="deterministic",
                    planner_confidence=0.15,
                    planner_fallback_reason="question_not_understood",
                )

        if chart is None:
            chart = self._default_chart(intent, dimensions, time_grain, metrics)

        order_by: list[SortRule] = []
        if limit and metrics:
            order_by.append(SortRule(field=metrics[0], direction="desc"))

        # Deterministic plans from keyword matching get moderate confidence.
        confidence = 0.60 if metrics else 0.30

        return PlannedQuestion(
            interpreted_question=question.strip(),
            plan=AgentPlan(
                intent=intent,
                metrics=metrics,
                dimensions=dimensions,
                filters=filters,
                time_grain=time_grain,
                order_by=order_by,
                limit=limit or 50,
                chart=chart,
            ),
            planner_source="deterministic",
            planner_confidence=confidence,
        )

    # ── Non-data intent detection ──────────────────────────────────────────

    def _detect_non_data_intent(self, normalized: str) -> PlannedQuestion | None:
        """Detect questions that should NOT route through the SQL pipeline."""
        if any(pat in normalized for pat in _SCHEMA_PATTERNS):
            return PlannedQuestion(
                interpreted_question="User wants to understand the data model and available tables/metrics.",
                plan=AgentPlan(
                    intent="schema_info",
                    metrics=[],
                    dimensions=[],
                    filters={},
                    time_grain="all",
                    chart=None,
                ),
                planner_source="deterministic",
                planner_confidence=0.85,
            )

        if any(pat in normalized for pat in _CAPABILITIES_PATTERNS):
            return PlannedQuestion(
                interpreted_question="User wants to know what the analytics agent can do.",
                plan=AgentPlan(
                    intent="capabilities",
                    metrics=[],
                    dimensions=[],
                    filters={},
                    time_grain="all",
                    chart=None,
                ),
                planner_source="deterministic",
                planner_confidence=0.85,
            )

        if any(pat in normalized for pat in _OVERVIEW_PATTERNS):
            return PlannedQuestion(
                interpreted_question="User wants a comprehensive overview of all available data.",
                plan=AgentPlan(
                    intent="data_overview",
                    metrics=_OVERVIEW_METRICS,
                    dimensions=[],
                    filters={"date_range": "all"},
                    time_grain="all",
                    chart=None,
                    explanation_level="detailed",
                    execution_strategy="service_call",
                    service_name="kpis",
                ),
                planner_source="deterministic",
                planner_confidence=0.75,
            )

        return None

    # ── Service-call detection ─────────────────────────────────────────────

    _SERVICE_PATTERNS: dict[str, tuple[str, ...]] = {
        "kpis": ("business kpi", "give me kpi", "key performance", "business performance",
                 "show kpi", "main kpi", "dashboard kpi"),
        "growth": ("growth", "mom change", "month over month", "month-over-month",
                   "mom comparison", "period comparison"),
        "quality_summary": ("data quality", "quality score", "dq score", "quality summary",
                            "how is the quality", "quality check"),
        "funnel": ("funnel", "conversion stages", "publish gap", "conversion funnel",
                   "upload to publish"),
        "monthly_trend": ("monthly trend", "trend over months", "month by month trend",
                          "monthly upload trend", "monthly chart"),
    }

    def _detect_service_call(self, normalized: str) -> PlannedQuestion | None:
        """Detect broad business questions that should route to a service."""
        for service_name, patterns in self._SERVICE_PATTERNS.items():
            if any(pat in normalized for pat in patterns):
                return PlannedQuestion(
                    interpreted_question=f"User wants {service_name.replace('_', ' ')} analytics.",
                    plan=AgentPlan(
                        intent="data_overview",
                        metrics=[],
                        dimensions=[],
                        filters={},
                        time_grain="all",
                        chart=None,
                        execution_strategy="service_call",
                        service_name=service_name,
                    ),
                    planner_source="deterministic",
                    planner_confidence=0.80,
                )
        return None

    def _looks_like_data_question(self, normalized: str) -> bool:
        """Heuristic: does this look like the user wants data/numbers?"""
        data_signals = (
            "how many", "how much", "count", "total", "average", "avg",
            "rate", "trend", "growth", "show me", "what is the",
            "breakdown", "compare", "top ", "bottom ", "best ",
            "worst ", "highest", "lowest", "most", "least",
        )
        return any(signal in normalized for signal in data_signals)

    def _match_metrics(self, question: str) -> list[str]:
        matches: list[str] = []
        for metric_name, aliases in _METRIC_ALIASES.items():
            if any(alias in question for alias in aliases):
                matches.append(metric_name)
        for metric_name, metric_def in METRIC_REGISTRY.items():
            if metric_name in question or metric_def.label.lower() in question:
                if metric_name not in matches:
                    matches.append(metric_name)
        return matches

    def _match_dimensions(self, question: str) -> list[str]:
        matches: list[str] = []
        for dim_name, aliases in _DIMENSION_ALIASES.items():
            if any(alias in question for alias in aliases):
                matches.append(dim_name)
        if "by " in question and not matches:
            for dim_name, dim_def in DIMENSION_REGISTRY.items():
                if dim_def.label.lower() in question:
                    matches.append(dim_name)
        return matches

    def _match_filters(self, question: str) -> dict[str, str]:
        filters: dict[str, str] = {}
        for date_range, aliases in _DATE_RANGE_ALIASES.items():
            if any(alias in question for alias in aliases):
                filters["date_range"] = date_range
                break
        return filters

    def _match_time_grain(self, question: str) -> str:
        for grain, aliases in _TIME_GRAIN_ALIASES.items():
            if any(alias in question for alias in aliases):
                return grain
        if any(token in question for token in ("trend", "over time")):
            return "day"
        return "all"

    def _match_chart(self, question: str) -> ChartRequest | None:
        for chart_type, aliases in _CHART_ALIASES.items():
            if any(alias in question for alias in aliases):
                return ChartRequest(type=chart_type)
        return None

    def _match_limit(self, question: str) -> int | None:
        match = _TOP_N_RE.search(question)
        if match:
            return int(match.group("limit"))
        return None

    def _infer_intent(
        self,
        question: str,
        metrics: list[str],
        dimensions: list[str],
        time_grain: str,
        limit: int | None,
    ) -> str:
        if "explain" in question and metrics:
            return "explain_metric"
        if limit is not None:
            return "top_n"
        if time_grain != "all":
            return "trend"
        if dimensions:
            return "breakdown"
        if "compare" in question or " vs " in question:
            return "comparison"
        if "table" in question or "list" in question:
            return "raw_table"
        if metrics:
            return "single_kpi"
        # No metrics matched — the calling code handles this as clarification
        return "clarification"

    def _default_chart(
        self,
        intent: str,
        dimensions: list[str],
        time_grain: str,
        metrics: list[str],
    ) -> ChartRequest | None:
        if intent == "explain_metric":
            return None
        if time_grain != "all" and metrics:
            return ChartRequest(type="line")
        if dimensions and metrics:
            return ChartRequest(type="bar", x=dimensions[0], y=metrics[0])
        if metrics:
            return ChartRequest(type="stat", y=metrics[0])
        return ChartRequest(type="table")
