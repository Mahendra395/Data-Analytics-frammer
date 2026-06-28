"""Typed contracts for the semantic analytics agent."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AgentIntent = Literal[
    "single_kpi",
    "trend",
    "breakdown",
    "comparison",
    "top_n",
    "diagnostic",
    "raw_table",
    "explain_metric",
    "schema_info",
    "capabilities",
    "data_overview",
    "clarification",
]

# Intents that answer without hitting the SQL pipeline.
NON_SQL_INTENTS: frozenset[str] = frozenset({
    "schema_info",
    "capabilities",
    "clarification",
    "explain_metric",
})

ExecutionStrategy = Literal["sql_query", "service_call", "multi_query"]
BlockType = Literal["markdown", "stat", "chart", "table", "kpi_grid"]
TrendDirection = Literal["up", "down", "flat"]

TimeGrain = Literal["day", "week", "month", "quarter", "year", "all"]
CompareMode = Literal["previous_period", "previous_month", "previous_year"]
ChartType = Literal["auto", "bar", "line", "area", "table", "stat", "pie"]
ResolvedChartType = Literal["bar", "line", "area", "table", "stat", "pie"]
SortDirection = Literal["asc", "desc"]
ExplanationLevel = Literal["short", "normal", "detailed"]
PlannerSource = Literal["openai", "deterministic", "supplied_plan", "repair", "service"]

ALLOWED_FILTER_KEYS = frozenset({
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
})


class SortRule(BaseModel):
    field: str
    direction: SortDirection = "desc"


class ChartRequest(BaseModel):
    type: ChartType = "auto"
    x: str | None = None
    y: str | None = None
    series: list[str] = Field(default_factory=list)
    title: str | None = None


class ChartSpec(BaseModel):
    chart_type: ResolvedChartType
    x: str | None = None
    y: str | None = None
    series: list[str] = Field(default_factory=list)
    title: str | None = None
    dataset_columns: list[str] = Field(default_factory=list)
    formatters: dict[str, str] = Field(default_factory=dict)


class StatValue(BaseModel):
    label: str
    value: Any
    unit: str | None = None
    delta_pct: float | None = None
    trend: TrendDirection | None = None


class ResponseBlock(BaseModel):
    block_type: BlockType
    title: str | None = None
    content: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    chart_spec: ChartSpec | None = None
    stats: list[StatValue] = Field(default_factory=list)
    sql: str | None = None


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: AgentIntent
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    time_grain: TimeGrain = "all"
    compare_mode: CompareMode | None = None
    order_by: list[SortRule] = Field(default_factory=list)
    limit: int = 50
    chart: ChartRequest | None = None
    explanation_level: ExplanationLevel = "normal"
    execution_strategy: ExecutionStrategy = "sql_query"
    service_name: str | None = None
    sub_plans: list[dict[str, Any]] | None = None

    @field_validator("metrics", "dimensions", mode="before")
    @classmethod
    def dedupe_preserve_order(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value) - ALLOWED_FILTER_KEYS)
        if unknown:
            raise ValueError("Unsupported filters: " + ", ".join(unknown))
        return value

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit must be at least 1")
        return value


class AgentValidationIssue(BaseModel):
    field: str
    code: str
    message: str


class AgentPlanResult(BaseModel):
    question: str
    interpreted_question: str
    plan: AgentPlan
    resolved_filters: dict[str, Any] = Field(default_factory=dict)
    planner_source: PlannerSource = "deterministic"
    planner_model: str | None = None
    planner_confidence: float | None = None
    planner_fallback_reason: str | None = None
    caveats: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    validation_issues: list[AgentValidationIssue] = Field(default_factory=list)


class AgentQueryRequest(BaseModel):
    question: str
    conversation_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    plan: AgentPlan | None = None


class AgentExecuteRequest(BaseModel):
    plan: AgentPlan
    question: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class AgentQueryResponse(BaseModel):
    question: str
    interpreted_question: str
    plan: AgentPlan
    resolved_filters: dict[str, Any] = Field(default_factory=dict)
    planner_source: PlannerSource = "deterministic"
    planner_model: str | None = None
    planner_confidence: float | None = None
    planner_fallback_reason: str | None = None
    sql: str | None
    sql_params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    chart_spec: ChartSpec | None = None
    summary: str
    caveats: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    execution_time_ms: float = 0.0
    row_count: int = 0
    blocks: list[ResponseBlock] = Field(default_factory=list)


class CompiledQuery(BaseModel):
    sql: str
    params: dict[str, Any]
    metrics: list[str]
    dimensions: list[str]
    limit: int
    join_count: int = 0
    time_axis: str | None = None
    time_column: str | None = None
