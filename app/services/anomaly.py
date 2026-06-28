"""Anomaly detection engine.

Detects unusual metric × dimension values using:
1. Percentage-change threshold: flag segments with > 50% MoM change and volume > 5
2. Deviation from portfolio average: flag segments where value deviates significantly

Also generates waterfall (contribution-to-change) data for any metric × dimension.
"""
from __future__ import annotations

import json
import logging
from calendar import monthrange
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams
from app.core.config import get_settings
from app.registry.filters import build_dim_only_where_clause, build_where_clause
from app.schemas.insights import (
    AnomalyItem,
    AnomalyResponse,
    WaterfallResponse,
    WaterfallSegment,
)

settings = get_settings()
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _month_epochs(yr: int, mo: int):
    first = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(yr, mo)[1]
    last = int(datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return first, last


def _prev_month(yr: int, mo: int):
    return (yr - 1, 12) if mo == 1 else (yr, mo - 1)


_DIM_MAP = {
    "client":     ("JOIN dim_client dcl ON dcl.id = fv.client_id",     "dcl.name"),
    "channel":    ("JOIN dim_channel dc ON dc.id = fv.channel_id",     "dc.name"),
    "user":       ("JOIN dim_user du ON du.id = fv.user_id",            "du.name"),
    "language":   ("JOIN dim_language dl ON dl.id = fv.language_id",    "dl.display_name"),
    "input_type": ("JOIN dim_input_type dit ON dit.id = fv.input_type_id", "dit.name"),
    "output_type": (
        "LEFT JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
        "JOIN dim_output_type dot ON dot.id = fvot.output_type_id",
        "dot.name",
    ),
}

_METRIC_MAP = {
    "uploaded":      "COUNT(fv.id)",
    "published":     "SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)",
    "duration_hrs":  "COALESCE(SUM(fv.uploaded_duration_sec),0)/3600.0",
    "publish_rate":  ("CASE WHEN COUNT(fv.id) > 0 "
                      "THEN ROUND(SUM(CASE WHEN fv.published THEN 1.0 ELSE 0 END)"
                      "/COUNT(fv.id)*100,1) ELSE 0 END"),
}


# ── Anomaly detection ─────────────────────────────────────────────────────────

async def detect_anomalies(
    db: AsyncSession,
    f: FilterParams,
    dimension: Optional[str] = None,
) -> AnomalyResponse:
    """Detect anomalies across metric × dimension combinations."""
    dims_to_check = [dimension] if dimension and dimension in _DIM_MAP else ["channel", "user", "language"]
    metrics_to_check = ["uploaded", "published", "publish_rate"]
    anomalies: List[AnomalyItem] = []

    dim_where, dim_params = build_dim_only_where_clause(f)

    # Determine reference month
    ref_yr, ref_mo = await _get_ref_month(db, f)
    prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)
    cur_from, cur_to = _month_epochs(ref_yr, ref_mo)
    prv_from, prv_to = _month_epochs(prev_yr, prev_mo)

    for dim_key in dims_to_check:
        if dim_key not in _DIM_MAP:
            continue
        dim_join, dim_col = _DIM_MAP[dim_key]

        for metric_key in metrics_to_check:
            metric_expr = _METRIC_MAP[metric_key]

            # Current period by segment
            cur_kf = f"_anom_cur_{dim_key}_{metric_key}_from"
            cur_kt = f"_anom_cur_{dim_key}_{metric_key}_to"
            cur_w = dim_where + [f"fv.uploaded_at >= :{cur_kf}", f"fv.uploaded_at <= :{cur_kt}"]
            cur_sql = text(
                f"SELECT {dim_col} AS seg, {metric_expr} AS val "
                f"FROM fact_video fv {dim_join} "
                f"WHERE {' AND '.join(cur_w)} "
                f"GROUP BY {dim_col} HAVING {dim_col} IS NOT NULL"
            )
            cur_rows = {
                r["seg"]: float(r["val"] or 0)
                for r in (await db.execute(cur_sql, {**dim_params, cur_kf: cur_from, cur_kt: cur_to})).mappings().all()
            }

            # Previous period by segment
            prv_kf = f"_anom_prv_{dim_key}_{metric_key}_from"
            prv_kt = f"_anom_prv_{dim_key}_{metric_key}_to"
            prv_w = dim_where + [f"fv.uploaded_at >= :{prv_kf}", f"fv.uploaded_at <= :{prv_kt}"]
            prv_sql = text(
                f"SELECT {dim_col} AS seg, {metric_expr} AS val "
                f"FROM fact_video fv {dim_join} "
                f"WHERE {' AND '.join(prv_w)} "
                f"GROUP BY {dim_col} HAVING {dim_col} IS NOT NULL"
            )
            prv_rows = {
                r["seg"]: float(r["val"] or 0)
                for r in (await db.execute(prv_sql, {**dim_params, prv_kf: prv_from, prv_kt: prv_to})).mappings().all()
            }

            # Portfolio average for current period
            all_vals = [v for v in cur_rows.values() if v > 0]
            portfolio_avg = sum(all_vals) / len(all_vals) if all_vals else 0

            # Detect anomalies
            for seg in set(cur_rows) | set(prv_rows):
                cur_v = cur_rows.get(seg, 0)
                prv_v = prv_rows.get(seg, 0)

                # Skip very small segments
                if metric_key in ("uploaded", "published") and max(cur_v, prv_v) < 5:
                    continue

                # Check 1: Large MoM change (> 50%)
                if prv_v > 0:
                    change_pct = abs((cur_v - prv_v) / prv_v) * 100
                    if change_pct > 50:
                        severity = "critical" if change_pct > 100 else "warning"
                        direction = "increased" if cur_v > prv_v else "decreased"
                        anomalies.append(AnomalyItem(
                            dimension=dim_key,
                            segment=seg,
                            metric=metric_key,
                            current_value=round(cur_v, 1),
                            expected_value=round(prv_v, 1),
                            deviation_pct=round(change_pct, 1),
                            severity=severity,
                            explanation=(
                                f"{seg} {metric_key} {direction} by {round(change_pct, 1)}% MoM "
                                f"(from {round(prv_v, 1)} to {round(cur_v, 1)})"
                            ),
                        ))

                # Check 2: Deviation from portfolio average (> 2x or < 0.3x)
                if portfolio_avg > 0 and metric_key in ("uploaded", "published"):
                    ratio = cur_v / portfolio_avg
                    if ratio > 2.5 or (ratio < 0.3 and cur_v > 0):
                        dev_pct = round(abs(cur_v - portfolio_avg) / portfolio_avg * 100, 1)
                        anomalies.append(AnomalyItem(
                            dimension=dim_key,
                            segment=seg,
                            metric=metric_key,
                            current_value=round(cur_v, 1),
                            expected_value=round(portfolio_avg, 1),
                            deviation_pct=dev_pct,
                            severity="info",
                            explanation=(
                                f"{seg} is {'significantly above' if ratio > 2.5 else 'significantly below'} "
                                f"the portfolio average for {metric_key} "
                                f"({round(cur_v, 1)} vs avg {round(portfolio_avg, 1)})"
                            ),
                        ))

    # Deduplicate (same segment+metric may appear from multiple checks)
    seen = set()
    unique = []
    for a in anomalies:
        key = (a.dimension, a.segment, a.metric, a.severity)
        if key not in seen:
            seen.add(key)
            unique.append(a)

    # Sort by severity then deviation
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    unique.sort(key=lambda a: (severity_order.get(a.severity, 3), -a.deviation_pct))

    return AnomalyResponse(
        anomalies=unique[:20],
        total_detected=len(unique),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Waterfall (contribution-to-change) ─────────────────────────────────────────

async def compute_waterfall(
    db: AsyncSession,
    f: FilterParams,
    metric: str = "uploaded",
    dimension: str = "channel",
) -> WaterfallResponse:
    """Compute contribution-to-change waterfall for a metric × dimension."""
    if dimension not in _DIM_MAP:
        dimension = "channel"
    if metric not in _METRIC_MAP:
        metric = "uploaded"

    dim_join, dim_col = _DIM_MAP[dimension]
    metric_expr = _METRIC_MAP[metric]
    dim_where, dim_params = build_dim_only_where_clause(f)

    ref_yr, ref_mo = await _get_ref_month(db, f)
    prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)
    cur_from, cur_to = _month_epochs(ref_yr, ref_mo)
    prv_from, prv_to = _month_epochs(prev_yr, prev_mo)

    _MONTH_LABELS = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }

    def _period_sql(ep_from: int, ep_to: int, suffix: str):
        kf = f"_wf_{suffix}_from"
        kt = f"_wf_{suffix}_to"
        w = dim_where + [f"fv.uploaded_at >= :{kf}", f"fv.uploaded_at <= :{kt}"]
        return (
            f"SELECT {dim_col} AS seg, {metric_expr} AS val "
            f"FROM fact_video fv {dim_join} "
            f"WHERE {' AND '.join(w)} "
            f"GROUP BY {dim_col} HAVING {dim_col} IS NOT NULL",
            {**dim_params, kf: ep_from, kt: ep_to},
        )

    cur_qsql, cur_p = _period_sql(cur_from, cur_to, "cur")
    prv_qsql, prv_p = _period_sql(prv_from, prv_to, "prv")

    cur_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(text(cur_qsql), cur_p)).mappings().all()}
    prv_rows = {r["seg"]: float(r["val"] or 0) for r in (await db.execute(text(prv_qsql), prv_p)).mappings().all()}

    all_segs = set(cur_rows) | set(prv_rows)
    raw: List[Dict[str, Any]] = []
    for seg in all_segs:
        cur_v = cur_rows.get(seg, 0)
        prv_v = prv_rows.get(seg, 0)
        raw.append({"segment": seg, "prev": prv_v, "current": cur_v, "delta": cur_v - prv_v})

    raw.sort(key=lambda x: abs(x["delta"]), reverse=True)
    total_abs = sum(abs(d["delta"]) for d in raw) or 1
    total_delta = sum(d["delta"] for d in raw)

    cumulative = 0.0
    segments: List[WaterfallSegment] = []
    for d in raw:
        share = abs(d["delta"]) / total_abs
        cumulative += share
        segments.append(WaterfallSegment(
            dimension=dimension,
            segment=d["segment"],
            prev_value=round(d["prev"], 2),
            current_value=round(d["current"], 2),
            delta=round(d["delta"], 2),
            share_of_total_delta=round(share, 4),
            cumulative_share=round(cumulative, 4),
        ))

    # Generate explanation
    explanation = await _generate_waterfall_explanation(
        metric, dimension, total_delta, segments[:5],
        f"{_MONTH_LABELS[prev_mo]} {str(prev_yr)[2:]}",
        f"{_MONTH_LABELS[ref_mo]} {str(ref_yr)[2:]}",
    )

    return WaterfallResponse(
        metric=metric,
        period_current=f"{_MONTH_LABELS[ref_mo]} {str(ref_yr)[2:]}",
        period_prev=f"{_MONTH_LABELS[prev_mo]} {str(prev_yr)[2:]}",
        total_delta=round(total_delta, 2),
        segments=segments,
        top_contributors=segments[:3],
        explanation=explanation,
    )


async def _generate_waterfall_explanation(
    metric: str,
    dimension: str,
    total_delta: float,
    top_segments: List[WaterfallSegment],
    prev_label: str,
    cur_label: str,
) -> str:
    """Generate a natural-language explanation of the waterfall."""
    if not top_segments:
        return f"No significant changes in {metric} by {dimension} between {prev_label} and {cur_label}."

    # Try LLM
    if settings.OPENAI_API_KEY:
        try:
            prompt = (
                f"Explain this metric change concisely in 1-2 sentences:\n"
                f"Metric: {metric}, Dimension: {dimension}\n"
                f"Total change from {prev_label} to {cur_label}: {total_delta:+.1f}\n"
                f"Top contributors:\n"
            )
            for s in top_segments:
                prompt += f"  - {s.segment}: {s.delta:+.1f} ({round(s.share_of_total_delta * 100, 1)}% of change)\n"
            prompt += "Write the explanation as if briefing an executive. Use actual segment names and numbers."

            payload = {
                "model": settings.OPENAI_SUMMARIZER_MODEL,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            }
            headers = {
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{settings.OPENAI_BASE_URL.rstrip('/')}/responses",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
            for item in body.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") == "output_text" and c.get("text", "").strip():
                        return c["text"].strip()
        except Exception as exc:
            logger.warning("waterfall_explanation_llm_failed error=%s", type(exc).__name__)

    # Deterministic fallback
    direction = "increased" if total_delta > 0 else "decreased"
    parts = []
    for s in top_segments[:3]:
        pct = round(s.share_of_total_delta * 100, 1)
        parts.append(f"{s.segment} ({pct}%)")
    contributors = ", ".join(parts)
    return (
        f"{metric.replace('_', ' ').title()} {direction} by {abs(total_delta):.0f} "
        f"from {prev_label} to {cur_label}. "
        f"Main contributors: {contributors}."
    )


async def _get_ref_month(db: AsyncSession, f: FilterParams) -> tuple[int, int]:
    if f.date_to:
        return f.date_to.year, f.date_to.month
    latest = (await db.execute(text("""
        SELECT EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int AS yr,
               EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo
        FROM fact_video WHERE uploaded_at IS NOT NULL ORDER BY uploaded_at DESC LIMIT 1
    """))).mappings().first()
    if latest:
        return int(latest["yr"]), int(latest["mo"])
    from datetime import date
    today = date.today()
    return today.year, today.month
