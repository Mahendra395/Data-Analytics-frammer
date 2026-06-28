"""LLM-powered insight generator with deterministic fallback.

Takes an InsightContext (assembled by insight_engine.py) and produces
structured InsightResponse with top risks, opportunities, drivers, and
an executive summary.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from app.core.config import get_settings
from app.schemas.insights import DriverItem, InsightItem, InsightResponse

settings = get_settings()
logger = logging.getLogger(__name__)


_INSIGHT_SYSTEM_PROMPT = """\
You are an analytics advisor for a video content operations platform (Frammer).
You analyse production pipeline data: uploads, processing, publishing, data quality,
and channel/user/language performance.

Given the analytics context below, produce a JSON object with EXACTLY this structure:
{
  "top_risks": [
    {
      "title": "Short risk title",
      "description": "1-2 sentence explanation of what's happening and why it matters",
      "severity": "critical|warning|info",
      "metric": "registry metric key or null",
      "dimension": "channel|user|language|client or null",
      "segment": "specific segment name or null",
      "value": numeric value or null,
      "benchmark": portfolio average or threshold or null,
      "recommended_action": "Specific actionable recommendation"
    }
  ],
  "top_opportunities": [ same structure as risks but severity is always "positive" ],
  "likely_drivers": [
    {
      "dimension": "channel|user|language",
      "segment": "segment name",
      "delta": numeric change,
      "share_of_total": 0.0 to 1.0,
      "direction": "up|down"
    }
  ],
  "executive_summary": "2-3 sentence executive briefing covering the most important findings"
}

Rules:
- Return EXACTLY 3 risks and 3 opportunities (or fewer if data doesn't support it)
- Risks should focus on: declining conversion, funnel breakdowns, DQ deterioration,
  SLA breaches, backlog growth, underperforming channels/users
- Opportunities should focus on: growing segments, high-efficiency areas that could
  scale, quick wins from fixing DQ or converting backlog
- Drivers should explain the biggest MoM changes
- Executive summary should be concise, data-backed, and actionable
- Use actual numbers from the context, don't invent values
- recommended_action must be specific and actionable, not generic advice
"""


async def generate_insights(context: Dict[str, Any]) -> InsightResponse:
    """Generate insights from analytics context. Tries LLM, falls back to rules."""
    if settings.OPENAI_API_KEY:
        try:
            return await _llm_generate(context)
        except Exception as exc:
            logger.warning("insight_llm_failed error=%s, falling back to rules", type(exc).__name__)

    return _deterministic_generate(context)


async def _llm_generate(context: Dict[str, Any]) -> InsightResponse:
    """Call OpenAI to generate structured insights."""
    user_prompt = f"Analytics context:\n```json\n{json.dumps(context, indent=2, default=str)}\n```"

    payload = {
        "model": settings.OPENAI_SUMMARIZER_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": _INSIGHT_SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_S) as client:
        resp = await client.post(
            f"{settings.OPENAI_BASE_URL.rstrip('/')}/responses",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()

    # Extract text from response
    raw_text = ""
    for item in body.get("output", []):
        for content_block in item.get("content", []):
            if content_block.get("type") == "output_text":
                raw_text = content_block.get("text", "").strip()
                break
        if raw_text:
            break

    if not raw_text:
        raise ValueError("Empty LLM response")

    parsed = json.loads(raw_text)

    return InsightResponse(
        top_risks=[InsightItem(**r) for r in parsed.get("top_risks", [])[:3]],
        top_opportunities=[InsightItem(**o) for o in parsed.get("top_opportunities", [])[:3]],
        likely_drivers=[DriverItem(**d) for d in parsed.get("likely_drivers", [])[:5]],
        executive_summary=parsed.get("executive_summary", ""),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Deterministic fallback ────────────────────────────────────────────────────

def _deterministic_generate(context: Dict[str, Any]) -> InsightResponse:
    """Rule-based insight generation when LLM is unavailable."""
    risks: List[InsightItem] = []
    opportunities: List[InsightItem] = []
    drivers: List[DriverItem] = []

    kpis = context.get("kpis", {})
    funnel = context.get("funnel", {})
    dq = context.get("dq_summary", {})
    lag = context.get("lag_summary", {})
    channels = context.get("channel_health", [])
    growth = context.get("growth_drivers", {})
    low_conv = context.get("low_conversion_segments", [])

    publish_rate = kpis.get("publish_rate", 0)
    dq_score = dq.get("overall_score", 100)
    backlog = lag.get("backlog_count", 0)
    sla_breaches = lag.get("sla_breaches", 0)

    # ── Risks ─────────────────────────────────────────────────────────────────

    # R1: Low overall publish conversion
    if publish_rate < 50:
        risks.append(InsightItem(
            title="Low publish conversion rate",
            description=f"Only {publish_rate}% of uploaded content gets published. "
                        f"The funnel has a gap of {funnel.get('publish_gap', 0)} items between processed and published.",
            severity="critical" if publish_rate < 30 else "warning",
            metric="publish_rate",
            value=publish_rate,
            benchmark=50.0,
            recommended_action="Investigate bottlenecks between processing and publishing stages. "
                               "Review the top channels with lowest conversion rates.",
        ))

    # R2: DQ score deterioration
    if dq_score < 80:
        worst = dq.get("worst_fields", [])
        field_detail = f" Worst fields: {', '.join(f['field'] for f in worst[:3])}." if worst else ""
        risks.append(InsightItem(
            title="Data quality below threshold",
            description=f"DQ score is {dq_score}/100.{field_detail}",
            severity="critical" if dq_score < 60 else "warning",
            metric="dq_score",
            value=dq_score,
            benchmark=80.0,
            recommended_action="Prioritize fixing null values in the worst fields. "
                               "Review ingestion pipeline for missing metadata.",
        ))

    # R3: Backlog / SLA issues
    if backlog > 50 or sla_breaches > 10:
        risks.append(InsightItem(
            title="High backlog or SLA breaches",
            description=f"{backlog} items in backlog, {sla_breaches} SLA breaches (>7 day lag).",
            severity="critical" if backlog > 200 or sla_breaches > 50 else "warning",
            metric="total_uploaded",
            dimension="overall",
            value=float(backlog),
            benchmark=50.0,
            recommended_action="Review lagging channels and assign additional processing capacity. "
                               "Prioritize oldest backlog items first.",
        ))

    # R4: Underperforming channels
    if channels:
        worst_channels = [ch for ch in channels if ch["conversion_pct"] < 30 and ch["volume"] >= 10]
        if worst_channels:
            ch = worst_channels[0]
            risks.append(InsightItem(
                title=f"Channel '{ch['channel']}' has very low conversion",
                description=f"Channel '{ch['channel']}' uploaded {ch['volume']} videos but only "
                            f"published {ch['published']} ({ch['conversion_pct']}% conversion).",
                severity="warning",
                metric="publish_rate",
                dimension="channel",
                segment=ch["channel"],
                value=ch["conversion_pct"],
                benchmark=publish_rate,
                recommended_action=f"Review why {ch['channel']} has such low publish conversion. "
                                   f"Check if there are processing blockers or quality issues.",
            ))

    # R5: Low-conversion language × channel combos
    if low_conv:
        lc = low_conv[0]
        risks.append(InsightItem(
            title=f"Low conversion: {lc['language']} content on {lc['channel']}",
            description=f"{lc['language']} content on channel '{lc['channel']}' has "
                        f"{lc['conversion_pct']}% publish conversion ({lc['volume']} videos).",
            severity="warning",
            metric="publish_rate",
            dimension="language",
            segment=f"{lc['language']} × {lc['channel']}",
            value=lc["conversion_pct"],
            benchmark=publish_rate,
            recommended_action="Investigate if this language-channel combination has "
                               "specific processing or editorial challenges.",
        ))

    # ── Opportunities ─────────────────────────────────────────────────────────

    # O1: High-efficiency channels that could scale
    if channels:
        high_eff = [ch for ch in channels if ch["conversion_pct"] > 70 and ch["volume"] < 50]
        if high_eff:
            ch = sorted(high_eff, key=lambda x: x["conversion_pct"], reverse=True)[0]
            opportunities.append(InsightItem(
                title=f"Scale high-efficiency channel '{ch['channel']}'",
                description=f"Channel '{ch['channel']}' has {ch['conversion_pct']}% conversion "
                            f"but only {ch['volume']} uploads. Increasing volume could yield "
                            f"proportional published output.",
                severity="positive",
                metric="publish_rate",
                dimension="channel",
                segment=ch["channel"],
                value=ch["conversion_pct"],
                benchmark=publish_rate,
                recommended_action=f"Allocate more content to {ch['channel']} — high conversion "
                                   f"suggests efficient editorial workflow.",
            ))

    # O2: Growing segments
    uploaded_by_channel = growth.get("uploaded_by_channel", {})
    if uploaded_by_channel and uploaded_by_channel.get("drivers"):
        top_grower = next(
            (d for d in uploaded_by_channel["drivers"] if d["delta"] > 0),
            None,
        )
        if top_grower:
            opportunities.append(InsightItem(
                title=f"Strong growth in channel '{top_grower['segment']}'",
                description=f"Channel '{top_grower['segment']}' grew by {int(top_grower['delta'])} uploads MoM "
                            f"({round(top_grower['share'] * 100, 1)}% of total change).",
                severity="positive",
                metric="total_uploaded",
                dimension="channel",
                segment=top_grower["segment"],
                value=top_grower["current"],
                benchmark=top_grower["previous"],
                recommended_action="Ensure processing capacity keeps up with this channel's growth trajectory.",
            ))

    # O3: Convert backlog to published
    if funnel.get("publish_gap", 0) > 20:
        gap = funnel["publish_gap"]
        opportunities.append(InsightItem(
            title=f"Convert {gap} processed-but-unpublished items",
            description=f"There are {gap} videos that have been processed but not yet published. "
                        f"Converting these would increase publish rate.",
            severity="positive",
            metric="total_published",
            value=float(gap),
            recommended_action="Review processed-but-unpublished queue and expedite publishing "
                               "for high-priority content.",
        ))

    # O4: DQ quick wins
    worst_fields = dq.get("worst_fields", [])
    if worst_fields:
        field = worst_fields[0]
        opportunities.append(InsightItem(
            title=f"Fix '{field['field']}' to boost DQ score",
            description=f"Field '{field['field']}' has {field['null_pct']}% null values. "
                        f"Fixing this single field would improve the overall DQ score.",
            severity="positive",
            metric="dq_score",
            value=field["null_pct"],
            benchmark=5.0,
            recommended_action=f"Add validation for '{field['field']}' in the ingestion pipeline "
                               f"to prevent null values.",
        ))

    # ── Drivers ───────────────────────────────────────────────────────────────
    for driver_key in ["uploaded_by_channel", "published_by_channel", "uploaded_by_language"]:
        driver_data = growth.get(driver_key, {})
        for d in driver_data.get("drivers", [])[:2]:
            drivers.append(DriverItem(
                dimension=driver_key.split("_by_")[1] if "_by_" in driver_key else "channel",
                segment=d["segment"],
                delta=d["delta"],
                share_of_total=d["share"],
                direction="up" if d["delta"] > 0 else "down",
            ))

    # ── Executive summary ─────────────────────────────────────────────────────
    total = kpis.get("total_uploaded", 0)
    total_pub = kpis.get("total_published", 0)
    summary_parts = [
        f"Portfolio: {total} uploaded, {total_pub} published ({publish_rate}% conversion).",
    ]
    if dq_score < 80:
        summary_parts.append(f"Data quality needs attention at {dq_score}/100.")
    if backlog > 50:
        summary_parts.append(f"Backlog of {backlog} items requires processing attention.")

    # Add MoM growth note
    upl_drivers = growth.get("uploaded_by_channel", {})
    total_delta = upl_drivers.get("total_delta", 0)
    if total_delta != 0:
        direction = "increased" if total_delta > 0 else "decreased"
        summary_parts.append(f"MoM uploads {direction} by {abs(int(total_delta))} videos.")

    return InsightResponse(
        top_risks=risks[:3],
        top_opportunities=opportunities[:3],
        likely_drivers=drivers[:5],
        executive_summary=" ".join(summary_parts),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
