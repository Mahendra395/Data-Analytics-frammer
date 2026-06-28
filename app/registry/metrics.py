"""Canonical metric registry for every KPI exposed by the Frammer analytics API.

This is the single source of truth for KPI definitions.  Every formula,
source table, caveat, and valid dimension is recorded here so that:
  1. Frontend contract docs can be generated from this registry.
  2. Route handlers can import SQL fragments rather than copy-pasting them.
  3. Ambiguous proxy metrics are explicitly flagged rather than silently
     masking missing data.

Business grain
--------------
  fact_video             — one row = one uploaded video/job event
  fact_video_output_type — one row = one video × output-type bridge event

"Processed" semantics (Phase 1)
---------------------------------
  ``is_processed`` is a materialized Boolean column on fact_video.
  It is set by the ingest pipeline as ``(created_duration_sec > 0)``.
  All "processed" counts in the API now use ``is_processed = TRUE`` directly —
  no more inline CASE expressions in route SQL.

  When ``processed_at`` is reliably populated by the source system, the ingest
  script will recompute ``is_processed`` as ``processed_at IS NOT NULL``.  No
  route changes will be needed.

Disambiguation: total_created
------------------------------
  In earlier versions of the API, ``total_created`` was overloaded:
    • In output-type context it meant clip count from the bridge table.
    • In all other contexts it was a proxy alias for total_uploaded.
  Both usages are disambiguated here as two separate metrics:
    • ``total_clips_created``  — clip count from fact_video_output_type
    • ``total_uploaded``       — video count from fact_video  (replaces proxy)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


# ---------------------------------------------------------------------------
# MetricDef dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricDef:
    """Complete definition of a single KPI.

    Attributes
    ----------
    name:
        Machine-readable key used in API responses and registry lookups.
    label:
        Human-readable display name for reports and UI.
    formula_sql:
        The SQL expression (aggregate or scalar) that computes this metric.
        May reference ``{alias}`` placeholder for the fact_video table alias.
        SQL fragments that need a bridge join are annotated with
        ``requires_bridge=True``.
    numerator:
        Description of the numerator (for ratio metrics), or ``None``.
    denominator:
        Description of the denominator (for ratio metrics), or ``None``.
    source_tables:
        Fact / dimension tables that must be present in the query.
    caveats:
        Plain-text warning about proxies, missing data, or scope limits.
    valid_dimensions:
        Dimension keys from DIMENSION_REGISTRY that can slice this metric.
        Empty frozenset means "all dimensions are valid".
    valid_time_grains:
        Supported aggregation granularities: ``day``, ``week``, ``month``,
        ``quarter``, ``year``, ``all``.
    null_handling:
        How NULL / zero values are treated in responses.
    requires_bridge:
        True when the formula requires a JOIN to fact_video_output_type.
    is_proxy:
        True when the formula is a documented stand-in for a more accurate
        future implementation.
    proxy_note:
        Brief explanation of what the proxy stands in for.
    display_unit:
        Rendering hint used by charts and summaries.
    default_time_column:
        Preferred fact timestamp column for date filtering and time bucketing.
    """

    name: str
    label: str
    formula_sql: str
    numerator: str | None = None
    denominator: str | None = None
    source_tables: tuple[str, ...] = field(default_factory=tuple)
    caveats: str = ""
    valid_dimensions: FrozenSet[str] = field(default_factory=frozenset)
    valid_time_grains: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"day", "week", "month", "quarter", "year", "all"})
    )
    null_handling: str = "treat as zero"
    requires_bridge: bool = False
    is_proxy: bool = False
    proxy_note: str = ""
    display_unit: str = "number"
    default_time_column: str = "uploaded_at"


# ---------------------------------------------------------------------------
# All valid dimension keys (mirrors DIMENSION_REGISTRY keys)
# ---------------------------------------------------------------------------

_ALL_DIMS: FrozenSet[str] = frozenset({
    "client", "channel", "user", "team", "language",
    "input_type", "output_type", "platform",
    "billable_flag", "published_flag",
})

_NON_BRIDGE_DIMS: FrozenSet[str] = _ALL_DIMS - {"output_type"}

# ---------------------------------------------------------------------------
# Metric Registry
# ---------------------------------------------------------------------------

METRIC_REGISTRY: dict[str, MetricDef] = {

    # ── Volume ─────────────────────────────────────────────────────────────

    "total_uploaded": MetricDef(
        name="total_uploaded",
        label="Total Uploaded",
        formula_sql="COUNT(*)",
        numerator="All rows in fact_video matching the applied filters",
        denominator=None,
        source_tables=("fact_video",),
        caveats="Counts every job event, including jobs with no output clips.",
        valid_dimensions=_ALL_DIMS,
        null_handling="always 0 or greater",
        display_unit="count",
    ),

    "total_published": MetricDef(
        name="total_published",
        label="Total Published",
        formula_sql="SUM(CASE WHEN {alias}.published THEN 1 ELSE 0 END)",
        numerator="Rows where published = TRUE",
        denominator=None,
        source_tables=("fact_video",),
        caveats="",
        valid_dimensions=_ALL_DIMS,
        null_handling="treat as zero",
        display_unit="count",
        default_time_column="published_at",
    ),

    "total_processed": MetricDef(
        name="total_processed",
        label="Total Processed",
        formula_sql="SUM(CASE WHEN {alias}.is_processed THEN 1 ELSE 0 END)",
        numerator="Rows where is_processed = TRUE (created_duration_sec > 0, materialized at ingest)",
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "is_processed is a materialized Boolean set by the ingest pipeline as "
            "(created_duration_sec > 0).  When processed_at is backfilled by the source "
            "system, the ingest script will recompute it as (processed_at IS NOT NULL)."
        ),
        valid_dimensions=_NON_BRIDGE_DIMS,
        null_handling="treat as zero",
        display_unit="count",
        default_time_column="processed_at",
    ),

    "total_clips_created": MetricDef(
        name="total_clips_created",
        label="Total Clips Created",
        formula_sql="SUM(fvot.created_count)",
        numerator="Sum of created_count across all output-type bridge rows",
        denominator=None,
        source_tables=("fact_video", "fact_video_output_type"),
        caveats=(
            "Requires a JOIN to fact_video_output_type. "
            "One video may produce multiple clips across different output types. "
            "Previously named 'total_created' in output-type context — now disambiguated."
        ),
        valid_dimensions=_ALL_DIMS,
        requires_bridge=True,
        null_handling="treat as zero",
    ),

    "total_clips_published": MetricDef(
        name="total_clips_published",
        label="Total Clips Published",
        formula_sql="SUM(fvot.published_count)",
        numerator="Sum of published_count across all output-type bridge rows",
        denominator=None,
        source_tables=("fact_video", "fact_video_output_type"),
        caveats="Requires a JOIN to fact_video_output_type.",
        valid_dimensions=_ALL_DIMS,
        requires_bridge=True,
        null_handling="treat as zero",
    ),

    # ── Rates ───────────────────────────────────────────────────────────────

    "publish_rate": MetricDef(
        name="publish_rate",
        label="Publish Rate",
        formula_sql="SUM(CASE WHEN {alias}.published THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0)",
        numerator="total_published",
        denominator="total_uploaded",
        source_tables=("fact_video",),
        caveats="Returns 0.0 when total_uploaded is zero.",
        valid_dimensions=_ALL_DIMS,
        null_handling="treat as 0.0",
        display_unit="percent",
    ),

    "processing_rate": MetricDef(
        name="processing_rate",
        label="Processing Rate",
        formula_sql=(
            "SUM(CASE WHEN {alias}.is_processed THEN 1 ELSE 0 END)::float "
            "/ NULLIF(COUNT(*), 0)"
        ),
        numerator="total_processed",
        denominator="total_uploaded",
        source_tables=("fact_video",),
        caveats="Returns 0.0 when total_uploaded is zero.",
        valid_dimensions=_NON_BRIDGE_DIMS,
        null_handling="treat as 0.0",
        display_unit="percent",
    ),

    "avg_clips_per_video": MetricDef(
        name="avg_clips_per_video",
        label="Avg Clips per Video",
        formula_sql="SUM(fvot.created_count)::float / NULLIF(COUNT(DISTINCT fv.id), 0)",
        numerator="total_clips_created",
        denominator="total_uploaded",
        source_tables=("fact_video", "fact_video_output_type"),
        caveats="Requires bridge join. Returns 1.0 when no clip data is available.",
        valid_dimensions=_NON_BRIDGE_DIMS,
        requires_bridge=True,
        null_handling="falls back to 1.0 when no bridge rows exist",
    ),

    # ── Duration ────────────────────────────────────────────────────────────

    "uploaded_duration_hrs": MetricDef(
        name="uploaded_duration_hrs",
        label="Uploaded Duration (hrs)",
        formula_sql="COALESCE(SUM({alias}.uploaded_duration_sec), 0) / 3600.0",
        numerator="Sum of uploaded_duration_sec in seconds, converted to hours",
        denominator=None,
        source_tables=("fact_video",),
        caveats="uploaded_duration_sec may be NULL for legacy rows.",
        valid_dimensions=_ALL_DIMS,
        null_handling="COALESCE to 0",
        display_unit="hours",
    ),

    "created_duration_hrs": MetricDef(
        name="created_duration_hrs",
        label="Created Duration (hrs)",
        formula_sql="COALESCE(SUM({alias}.created_duration_sec), 0) / 3600.0",
        numerator="Sum of created_duration_sec in seconds, converted to hours",
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "created_duration_sec is NULL for unprocessed videos. "
            "Doubles as the proxy indicator for 'processed' status (>0 means processed)."
        ),
        valid_dimensions=_ALL_DIMS,
        null_handling="COALESCE to 0",
        display_unit="hours",
        default_time_column="processed_at",
    ),

    "published_duration_hrs": MetricDef(
        name="published_duration_hrs",
        label="Published Duration (hrs)",
        formula_sql="COALESCE(SUM({alias}.published_duration_sec), 0) / 3600.0",
        numerator="Sum of published_duration_sec in seconds, converted to hours",
        denominator=None,
        source_tables=("fact_video",),
        caveats="published_duration_sec is NULL for unpublished videos.",
        valid_dimensions=_ALL_DIMS,
        null_handling="COALESCE to 0",
        display_unit="hours",
        default_time_column="published_at",
    ),

    # ── Growth ──────────────────────────────────────────────────────────────

    "mom_growth_pct": MetricDef(
        name="mom_growth_pct",
        label="MoM Growth %",
        formula_sql=(
            "(curr_month_count - prev_month_count)::float "
            "/ NULLIF(prev_month_count, 0) * 100"
        ),
        numerator="current month total_uploaded - previous month total_uploaded",
        denominator="previous month total_uploaded",
        source_tables=("fact_video",),
        caveats=(
            "Computed over the last two calendar months with any data — not necessarily "
            "the current calendar month. Returns NULL when fewer than 2 months exist."
        ),
        valid_dimensions=_NON_BRIDGE_DIMS,
        valid_time_grains=frozenset({"month"}),
        null_handling="NULL when fewer than 2 months of data",
    ),

    # ── Data quality ─────────────────────────────────────────────────────────

    "dq_score": MetricDef(
        name="dq_score",
        label="Data Quality Score",
        formula_sql=(
            "100.0 - ("
            "  (SUM(CASE WHEN channel_id   IS NULL THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 25)"
            "+ (SUM(CASE WHEN user_id      IS NULL THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 25)"
            "+ (SUM(CASE WHEN language_id  IS NULL THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 25)"
            "+ (SUM(CASE WHEN input_type_id IS NULL THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) * 25)"
            ")"
        ),
        numerator=(
            "100 minus weighted null-rate penalties on 4 FK columns "
            "(channel_id, user_id, language_id, input_type_id) — each worth 25 points"
        ),
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "Score range: 0–100. Each missing FK deducts up to 25 points proportionally. "
            "Does not penalise missing output_type links because those are populated on "
            "demand via the bridge table."
        ),
        valid_dimensions=frozenset(),  # global only; not sliceable
        valid_time_grains=frozenset({"all"}),
        null_handling="treat as 0.0",
        display_unit="percent",
    ),

    # ── Composite / analytics ─────────────────────────────────────────────────

    "health_score": MetricDef(
        name="health_score",
        label="Health Score",
        formula_sql="PERCENT_RANK() OVER (ORDER BY COUNT(*)) * 50 + (pub_rate * 50)",
        numerator="volume percentile rank (0–50) + publish rate contribution (0–50)",
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "Window-function based — requires a per-entity subquery. "
            "Score range: 0–100. Computed per channel or user entity."
        ),
        valid_dimensions=frozenset({"channel", "user", "team"}),
        valid_time_grains=frozenset({"month", "quarter", "year", "all"}),
        null_handling="treat as 0.0",
    ),

    "productivity_index": MetricDef(
        name="productivity_index",
        label="Productivity Index",
        formula_sql="vol_rank * 0.4 + conv_rate * 0.3 + consistency * 0.3",
        numerator=(
            "weighted sum: volume rank (40%) + publish conversion rate (30%) "
            "+ upload consistency score (30%)"
        ),
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "Composite index — all three components are independently normalised "
            "before weighting. Score is relative to the current filtered cohort, "
            "not absolute."
        ),
        valid_dimensions=frozenset({"user", "team"}),
        valid_time_grains=frozenset({"month", "quarter", "year", "all"}),
        null_handling="treat as 0.0",
    ),

    # ── Lag / efficiency ──────────────────────────────────────────────────────

    "avg_processing_lag_min": MetricDef(
        name="avg_processing_lag_min",
        label="Avg Processing Lag (min)",
        formula_sql=(
            "ROUND(AVG(COALESCE(fv.processing_lag_sec, "
            "  CASE WHEN fv.processed_at IS NOT NULL AND fv.uploaded_at IS NOT NULL "
            "       THEN fv.processed_at - fv.uploaded_at ELSE NULL END"
            ")) / 60.0, 1)"
        ),
        numerator="processing_lag_sec if populated, else processed_at - uploaded_at",
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "For the obfuscated demo dataset, processed_at is synthesized at ingest from "
            "monthly volume, channel allocation, and processing-duration signals. "
            "Values are internally consistent but should be treated as synthetic timings."
        ),
        valid_dimensions=_NON_BRIDGE_DIMS,
        is_proxy=True,
        proxy_note=(
            "The source feed is aggregate-heavy, so per-video lag is reconstructed "
            "during ingest rather than copied from an operational event stream."
        ),
        null_handling="NULL when both lag fields are absent",
        display_unit="minutes",
        default_time_column="processed_at",
    ),

    "avg_publishing_lag_min": MetricDef(
        name="avg_publishing_lag_min",
        label="Avg Publishing Lag (min)",
        formula_sql=(
            "ROUND(AVG(COALESCE(fv.publishing_lag_sec, "
            "  CASE WHEN fv.published_at IS NOT NULL AND fv.uploaded_at IS NOT NULL "
            "       THEN fv.published_at - fv.uploaded_at ELSE NULL END"
            ")) / 60.0, 1)"
        ),
        numerator="publishing_lag_sec if populated, else published_at - uploaded_at",
        denominator=None,
        source_tables=("fact_video",),
        caveats=(
            "For the obfuscated demo dataset, published_at and publishing_lag_sec are "
            "synthesized at ingest to match channel/platform publishing totals. "
            "Use them as realistic demo timings, not operational ground truth."
        ),
        valid_dimensions=_NON_BRIDGE_DIMS,
        null_handling="NULL when both lag fields are absent",
        display_unit="minutes",
        default_time_column="published_at",
    ),
}
