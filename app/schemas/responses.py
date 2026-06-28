"""Shared Pydantic response schemas."""
from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


# ── Response Metadata ──────────────────────────────────────────────────────────

class ResponseMetadata(BaseModel):
    """Contextual metadata attached to every API response envelope.

    Fields
    ------
    filters_applied:
        Serialized representation of the FilterParams that produced the data.
    generated_at:
        ISO-8601 UTC timestamp of when the response was assembled.
    metric_definitions_used:
        Registry keys of every KPI/metric computed in this response,
        enabling consumers to look up formal definitions via /core/registry.
    source_grain:
        Describes the unit of the underlying data:
        "video-level" | "monthly-aggregated" | "rule-evaluated" | "raw-sql" | etc.
    caveats:
        Human-readable notes about data limitations, proxy fields, or edge cases.
    unit:
        Primary unit of the returned values: "count" | "hours" | "percent" | "minutes".
        Omitted when the response mixes multiple units.
    currency:
        ISO 4217 currency code when monetary values are included (e.g. "USD").
    """

    filters_applied: Dict[str, Any]
    generated_at: str
    metric_definitions_used: List[str]
    source_grain: str
    caveats: List[str]
    unit: Optional[str] = None
    currency: Optional[str] = None
    planner_source: Optional[str] = None
    planner_model: Optional[str] = None
    planner_confidence: Optional[float] = None
    planner_fallback_reason: Optional[str] = None
    cache_hit: Optional[bool] = None
    audit_id: Optional[str] = None


class ApiResponse(BaseModel, Generic[T]):
    """Universal response envelope wrapping every endpoint's payload with metadata."""

    data: T
    meta: ResponseMetadata


class PaginatedResponse(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── KPI ────────────────────────────────────────────────────────────────────────
class KPIResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_uploaded: int
    total_created: int
    total_published: int
    total_processed: int = 0
    publish_rate: float          # published / uploaded
    processing_rate: float       # created / uploaded
    total_uploaded_duration_hrs: float
    total_created_duration_hrs: float
    total_published_duration_hrs: float
    active_channels: int
    active_users: int
    active_clients: int
    active_teams: int = 0
    mom_growth_pct: Optional[float] = None   # month-over-month uploaded count growth
    avg_clips_per_video: float = 1.0
    top_channel: str = ""
    top_language: str = ""
    dq_score: float = 0.0

    # ── Comparison period fields (populated when compareMode query param is set) ──
    # Set to None when no comparison was requested.
    compare_mode: Optional[str] = None
    compare_period_label: Optional[str] = None
    comparison_total_uploaded: Optional[int] = None
    comparison_total_published: Optional[int] = None
    comparison_total_processed: Optional[int] = None
    comparison_uploaded_duration_hrs: Optional[float] = None
    comparison_published_duration_hrs: Optional[float] = None
    # Percentage change: (current - comparison) / comparison * 100
    delta_uploaded_pct: Optional[float] = None
    delta_published_pct: Optional[float] = None
    delta_processed_pct: Optional[float] = None
    delta_duration_pct: Optional[float] = None


# ── Monthly ────────────────────────────────────────────────────────────────────
class MonthlyRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    month_label: str        # e.g. "Mar 25"
    year: int
    month: int
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float
    avg_duration_min: float = 0.0


# ── Channel ────────────────────────────────────────────────────────────────────
class ChannelRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    channel: str
    obfuscated_code: Optional[str] = None
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float
    avg_duration_min: float = 0.0


class ChannelUserRow(BaseModel):
    channel: str
    user: str
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float


# ── User / Team ────────────────────────────────────────────────────────────────
class UserRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user: str
    team_name: Optional[str] = None
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float
    avg_duration_min: float = 0.0


# ── Language ───────────────────────────────────────────────────────────────────
class LanguageRow(BaseModel):
    iso_code: str
    display_name: str
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float
    percentage: float  # share of total uploaded


# ── Input Type ─────────────────────────────────────────────────────────────────
class InputTypeRow(BaseModel):
    input_type: str
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float


# ── Output Type ────────────────────────────────────────────────────────────────
class OutputTypeRow(BaseModel):
    output_type: str
    total_uploaded: int
    total_created: int
    total_published: int
    uploaded_duration_hrs: float
    created_duration_hrs: float
    published_duration_hrs: float


# ── Video (paginated list) ─────────────────────────────────────────────────────
class VideoRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: Optional[str]
    headline: Optional[str]
    client: Optional[str]
    channel: Optional[str]
    user: Optional[str]
    language: Optional[str]
    input_type: Optional[str]
    output_types: List[str]
    published: bool
    published_platform: Optional[str]
    uploaded_at: Optional[int]    # epoch seconds
    uploaded_duration_hrs: Optional[float]
    created_duration_hrs: Optional[float]
    published_duration_hrs: Optional[float]


# ── Publishing ─────────────────────────────────────────────────────────────────
class PublishingPlatformCount(BaseModel):
    channel: str
    facebook: int = 0
    instagram: int = 0
    linkedin: int = 0
    reels: int = 0
    shorts: int = 0
    x: int = 0
    youtube: int = 0
    threads: int = 0
    total: int = 0


class PublishingPlatformDuration(BaseModel):
    channel: str
    facebook_hrs: float = 0
    instagram_hrs: float = 0
    linkedin_hrs: float = 0
    reels_hrs: float = 0
    shorts_hrs: float = 0
    x_hrs: float = 0
    youtube_hrs: float = 0
    threads_hrs: float = 0


# ── Data Quality ───────────────────────────────────────────────────────────────
class QualityColumnReport(BaseModel):
    column: str
    total_rows: int
    null_count: int
    null_pct: float
    distinct_count: int
    has_issues: bool
    issue_description: Optional[str] = None
    status: str = "good"  # 'good' | 'warning' | 'critical'


class QualitySummary(BaseModel):
    overall_score: float           # 0–100
    total_rows: int
    columns: List[QualityColumnReport]
    duplicate_video_ids: int
    unknown_team_names: int


# ── Forecast ───────────────────────────────────────────────────────────────────
class ForecastPoint(BaseModel):
    month_label: str
    year: int
    month: int
    actual: Optional[float] = None
    forecast: Optional[float] = None
    upper: Optional[float] = None
    lower: Optional[float] = None
    is_forecast: bool = False


class ForecastResponse(BaseModel):
    metric: str
    horizon_months: int
    monthly_growth_rate: float
    model_confidence: float
    data: List[ForecastPoint]


# ── Query (SQL sandbox) ────────────────────────────────────────────────────────
class QueryRequest(BaseModel):
    sql: str
    limit: int = 500


class QueryResponse(BaseModel):
    columns: List[str]
    rows: List[List[Any]]
    row_count: int
    execution_time_ms: float


# ── Processing / Duration Buckets ────────────────────────────────────────────
class DurationBucketRow(BaseModel):
    range: str
    count: int


# ── Dimension lists (for filter dropdowns) ─────────────────────────────────────
class DimensionItem(BaseModel):
    value: str
    label: str


class DimensionsResponse(BaseModel):
    clients: List[DimensionItem]
    channels: List[DimensionItem]
    users: List[DimensionItem]
    teams: List[DimensionItem]
    languages: List[DimensionItem]
    input_types: List[DimensionItem]
    output_types: List[DimensionItem]
    platforms: List[DimensionItem]
    billable_flag_options: List[DimensionItem]
    published_flag_options: List[DimensionItem]
    date_range_options: List[DimensionItem]


# ── Client Summary ─────────────────────────────────────────────────────────────
class ClientSummaryRow(BaseModel):
    slug: str
    name: str
    total_uploaded: int
    total_processed: int = 0
    total_published: int
    total_clips: int
    publish_rate: float
    active_channels: int
    active_users: int
    uploaded_duration_hrs: float


# ── Funnel ─────────────────────────────────────────────────────────────────────
class FunnelStage(BaseModel):
    stage: str
    count: int
    duration_hrs: float
    conversion_from_prev: Optional[float] = None
    conversion_from_first: Optional[float] = None


class FunnelResponse(BaseModel):
    stages: List[FunnelStage]
    publish_gap_count: int          # processed - published
    publish_gap_duration_hrs: float


# ── Growth ─────────────────────────────────────────────────────────────────────
class GrowthPeriod(BaseModel):
    period_label: str
    year: int
    month: int
    uploaded: int
    processed: int
    published: int
    uploaded_duration_hrs: float
    published_duration_hrs: float


class GrowthResponse(BaseModel):
    current: GrowthPeriod
    previous: Optional[GrowthPeriod] = None
    compare_mode: Optional[str] = None    # previous_month | previous_year | previous_period
    mom_uploaded_pct: Optional[float] = None
    mom_published_pct: Optional[float] = None
    mom_duration_pct: Optional[float] = None
    rolling_30d_uploaded: int = 0
    rolling_30d_published: int = 0
    rolling_30d_prev_uploaded: int = 0


# ── Lag / Time Efficiency ──────────────────────────────────────────────────────
class LagMetricsRow(BaseModel):
    segment: Optional[str] = None            # channel / user / type name
    segment_type: Optional[str] = None       # "channel" | "user" | "output_type" | "overall"
    avg_processing_lag_min: Optional[float] = None
    median_processing_lag_min: Optional[float] = None
    p90_processing_lag_min: Optional[float] = None
    avg_publishing_lag_min: Optional[float] = None
    median_publishing_lag_min: Optional[float] = None
    p90_publishing_lag_min: Optional[float] = None
    avg_cycle_lag_min: Optional[float] = None
    count: int = 0


class LagResponse(BaseModel):
    overall: LagMetricsRow
    by_channel: List[LagMetricsRow] = []
    by_user: List[LagMetricsRow] = []


# ── Multi-Dimensional Analysis ─────────────────────────────────────────────────
class MultiDimensionalCell(BaseModel):
    dim1: str
    dim2: str
    uploaded: int = 0
    published: int = 0
    duration_hrs: float = 0.0
    publish_conversion_pct: float = 0.0
    contribution_pct: float = 0.0


class MultiDimensionalResponse(BaseModel):
    dim1: str
    dim2: str
    metric: str
    cells: List[MultiDimensionalCell]
    dim1_values: List[str]
    dim2_values: List[str]


# ── Teams ──────────────────────────────────────────────────────────────────────
class TeamRow(BaseModel):
    team_name: str
    total_uploaded: int
    total_published: int
    total_users: int
    uploaded_duration_hrs: float
    published_duration_hrs: float
    publish_rate: float
    avg_duration_min: float = 0.0


# ── Channel Health ─────────────────────────────────────────────────────────────
class ChannelHealthRow(BaseModel):
    channel: str
    obfuscated_code: Optional[str] = None
    total_uploaded: int
    total_published: int
    publish_conversion_pct: float
    avg_duration_min: float
    processed_not_published: int
    health_quadrant: str   # "star" | "high_volume" | "high_efficiency" | "underperforming"
    health_score: float


# ── User Productivity ──────────────────────────────────────────────────────────
class UserProductivityRow(BaseModel):
    user: str
    team_name: Optional[str] = None
    total_uploaded: int
    total_published: int
    publish_conversion_pct: float
    uploaded_duration_hrs: float
    avg_duration_min: float
    productivity_index: float   # composite score 0–100


# ── Quality Trend ──────────────────────────────────────────────────────────────
class QualityTrendPoint(BaseModel):
    month_label: str
    year: int
    month: int
    total_rows: int
    null_channel_pct: float
    null_user_pct: float
    null_language_pct: float
    null_input_type_pct: float
    overall_score: float


class QualityTrendResponse(BaseModel):
    trend: List[QualityTrendPoint]
    by_client: List[dict] = []
    invalid_url_count: int = 0
    duplicate_job_id_count: int = 0
    unknown_language_pct: float = 0.0
    unknown_input_type_pct: float = 0.0
    unknown_output_type_pct: float = 0.0
    null_platform_pct: float = 0.0


# ── Data Quality v2 ────────────────────────────────────────────────────────────
class DQRuleResult(BaseModel):
    rule_id: str
    rule_name: str
    description: str
    affected_count: int
    total_rows: int
    affected_pct: float
    severity: str           # "critical" | "warning" | "info" | "ok"
    category: str           # "null" | "duplicate" | "invalid" | "consistency" | "completeness"


class DQFieldReport(BaseModel):
    field: str
    table: str
    total_rows: int
    null_count: int
    null_pct: float
    unknown_count: int
    unknown_pct: float
    distinct_count: int
    status: str             # "good" | "warning" | "critical"


class DQIssueRow(BaseModel):
    row_id: str
    video_id: Optional[str]
    headline: Optional[str]
    channel: Optional[str]
    user: Optional[str]
    issue_category: str
    issue_detail: str
    severity: str           # "critical" | "warning" | "info"
    uploaded_at: Optional[int] = None


class DQRulesResponse(BaseModel):
    rules: List[DQRuleResult]
    overall_score: float
    total_rows: int
    critical_count: int
    warning_count: int


# ── Benchmarks ─────────────────────────────────────────────────────────────────
class BenchmarkSegmentRow(BaseModel):
    segment: str
    segment_type: str           # "client" | "channel" | "user" | "type" | "language"
    metric: str                 # "uploaded" | "published" | "publish_rate" | "duration_hrs"
    value: float
    portfolio_avg: float
    peer_avg: float
    percentile: float           # 0–100
    trend_delta: Optional[float] = None   # current vs previous period delta
    health_score: Optional[float] = None  # 0–100 composite score
    risk_level: Optional[str] = None      # "critical" | "warning" | "healthy"
    grade: Optional[str] = None           # A–F letter grade


class BenchmarkResponse(BaseModel):
    dimension: str
    metric: str
    segments: List[BenchmarkSegmentRow]
    portfolio_avg: float
    portfolio_median: float


# ── Growth Drivers ──────────────────────────────────────────────────────────────
class GrowthDriverRow(BaseModel):
    segment: str
    current_value: float
    prev_value: float
    delta: float
    share_of_total_delta: float     # fraction of total absolute change explained by this segment


class GrowthDriversResponse(BaseModel):
    dimension: str
    metric: str = "uploaded"
    period_current: str
    period_prev: str
    total_delta: float
    drivers: List[GrowthDriverRow]


# ── Lag Backlog / SLA / Aging ──────────────────────────────────────────────────
class BacklogItem(BaseModel):
    row_id: str
    video_id: Optional[str]
    headline: Optional[str]
    client: Optional[str]
    channel: Optional[str]
    user: Optional[str]
    uploaded_at: Optional[int]
    days_in_backlog: float


class AgingBucket(BaseModel):
    bucket_label: str       # e.g. "0–7 days"
    min_days: int
    max_days: Optional[int]
    count: int
    pct: float


class BacklogResponse(BaseModel):
    total_backlog: int
    oldest_days: float
    avg_days: float
    buckets: List[AgingBucket]
    oldest_items: List[BacklogItem]


class SLABreachRow(BaseModel):
    segment: str
    segment_type: str           # "channel" | "user" | "client" | "overall"
    breach_count: int
    total_count: int
    breach_pct: float
    avg_lag_min: Optional[float] = None
    sla_threshold_days: float = 7.0


class SLABreachResponse(BaseModel):
    sla_threshold_days: float
    overall_breach_count: int
    overall_breach_pct: float
    by_channel: List[SLABreachRow]
    by_user: List[SLABreachRow]
    by_client: List[SLABreachRow]


class AgingResponse(BaseModel):
    buckets: List[AgingBucket]
    total_backlog: int
    oldest_item: Optional[BacklogItem] = None


# ── Extended Video Row ─────────────────────────────────────────────────────────
class VideoRowExtended(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: Optional[str]
    headline: Optional[str]
    client: Optional[str]
    client_slug: Optional[str] = None
    channel: Optional[str]
    user: Optional[str]
    team_name: Optional[str] = None
    language: Optional[str]
    input_type: Optional[str]
    output_types: List[str]
    platform: Optional[str] = None
    published: bool
    published_platform: Optional[str]
    source_url: Optional[str] = None
    published_url: Optional[str] = None
    billable_flag: bool = False
    uploaded_at: Optional[int]
    processed_at: Optional[int] = None
    published_at: Optional[int] = None
    uploaded_duration_hrs: Optional[float]
    created_duration_hrs: Optional[float]
    published_duration_hrs: Optional[float]
    processing_lag_min: Optional[float] = None
    publishing_lag_min: Optional[float] = None
    total_cycle_lag_min: Optional[float] = None
    # DQ flags
    missing_team_flag: bool = False
    missing_platform_flag: bool = False
    invalid_url_flag: bool = False
    duplicate_video_id_flag: bool = False
    # Derived
    issue_category: Optional[str] = None   # "missing_metadata" | "invalid_url" | "high_lag" | "duplicate" | None


class VideoExplorerResponse(BaseModel):
    items: List[VideoRowExtended]
    total: int
    page: int
    page_size: int
    total_pages: int
    preset: Optional[str] = None
