"""Pydantic models for the insight & recommendation engine."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class InsightItem(BaseModel):
    """A single risk or opportunity insight."""
    title: str
    description: str
    severity: str                       # "critical" | "warning" | "info" | "positive"
    metric: Optional[str] = None        # registry metric key (e.g. "publish_rate")
    dimension: Optional[str] = None     # e.g. "channel", "user", "language"
    segment: Optional[str] = None       # specific segment name
    value: Optional[float] = None       # current metric value
    benchmark: Optional[float] = None   # portfolio average or threshold
    recommended_action: Optional[str] = None


class DriverItem(BaseModel):
    """A segment that contributed to a metric change."""
    dimension: str
    segment: str
    delta: float
    share_of_total: float               # 0-1 fraction of total absolute change
    direction: str                      # "up" | "down"


class InsightResponse(BaseModel):
    """Full insight payload returned by /insights/summary."""
    top_risks: List[InsightItem]
    top_opportunities: List[InsightItem]
    likely_drivers: List[DriverItem]
    executive_summary: str
    generated_at: str


# ── Anomaly schemas ────────────────────────────────────────────────────────────

class AnomalyItem(BaseModel):
    """A detected anomaly for a specific metric × dimension segment."""
    dimension: str
    segment: str
    metric: str
    current_value: float
    expected_value: float
    deviation_pct: float
    severity: str                       # "critical" | "warning" | "info"
    explanation: Optional[str] = None


class AnomalyResponse(BaseModel):
    """Response for /insights/anomalies."""
    anomalies: List[AnomalyItem]
    total_detected: int
    generated_at: str


# ── Waterfall schemas ──────────────────────────────────────────────────────────

class WaterfallSegment(BaseModel):
    """One bar in the waterfall chart."""
    dimension: str
    segment: str
    prev_value: float
    current_value: float
    delta: float
    share_of_total_delta: float         # fraction of total absolute change
    cumulative_share: float             # running cumulative share


class WaterfallResponse(BaseModel):
    """Response for /insights/waterfall."""
    metric: str
    period_current: str
    period_prev: str
    total_delta: float
    segments: List[WaterfallSegment]
    top_contributors: List[WaterfallSegment]
    explanation: Optional[str] = None


# ── Score schemas ──────────────────────────────────────────────────────────────

class ScoreSegment(BaseModel):
    """Health score for a single segment within a dimension."""
    segment: str
    segment_type: str
    health_score: float                 # composite 0-100
    risk_level: str                     # "critical" | "warning" | "healthy"
    grade: str                          # "A" | "B" | "C" | "D" | "F"
    volume_rank: float                  # percentile rank by volume 0-100
    conversion_rate: float              # publish rate %
    lag_score: float                    # 0-100 (lower lag = higher score)
    sla_score: float                    # 0-100 (fewer breaches = higher)
    trend_direction: Optional[str] = None   # "up" | "down" | "flat"
    trend_delta: Optional[float] = None
    # Extra context fields (kept for API richness)
    value: Optional[float] = None
    portfolio_avg: Optional[float] = None
    peer_avg: Optional[float] = None
    percentile: Optional[float] = None
    delta_vs_benchmark: Optional[float] = None


class ScoreResponse(BaseModel):
    """Response for /diagnostics/scores/{dimension}."""
    dimension: str
    segments: List[ScoreSegment]
    portfolio_avg_score: float
    critical_count: int
    warning_count: int
    healthy_count: int


class ScoreOverviewItem(BaseModel):
    """Summary for one dimension in the scores overview."""
    dimension: str
    portfolio_avg_score: float
    critical_count: int
    warning_count: int
    healthy_count: int
    worst_segment: str
    worst_score: float


class ScoreOverviewResponse(BaseModel):
    """Response for /diagnostics/scores/overview."""
    overview: List[ScoreOverviewItem]
