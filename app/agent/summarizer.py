"""Summary builder for agent query results — LLM-powered with template fallback."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.agent.prompts import build_summarizer_prompt, build_blocks_summarizer_prompt
from app.agent.schemas import AgentPlan, ResponseBlock
from app.core.config import get_settings
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY

settings = get_settings()
logger = logging.getLogger(__name__)


class AgentSummarizer:
    async def summarize(
        self,
        plan: AgentPlan,
        columns: list[str],
        rows: list[list[Any]],
        *,
        question: str = "",
        interpreted_question: str = "",
        caveats: list[str] | None = None,
    ) -> str:
        """Generate a natural-language summary. Uses LLM when available, template fallback otherwise."""
        if not rows:
            return "No data matched the selected filters and time range. Try broadening the date range or removing filters."

        # Try LLM summarization first
        if settings.OPENAI_API_KEY and question:
            llm_summary = await self._llm_summarize(
                plan=plan,
                columns=columns,
                rows=rows,
                question=question,
                interpreted_question=interpreted_question,
                caveats=caveats or [],
            )
            if llm_summary:
                return llm_summary

        # Template fallback
        return self._template_summarize(plan, columns, rows)

    async def summarize_blocks(
        self,
        blocks: list[ResponseBlock],
        *,
        question: str = "",
        explanation_level: str = "normal",
    ) -> str:
        """Generate a unified summary across multiple ResponseBlocks."""
        if not blocks:
            return "No data available for the selected filters."

        blocks_summary = []
        for b in blocks:
            info: dict[str, Any] = {"type": b.block_type, "title": b.title or ""}
            if b.stats:
                info["stats"] = [
                    {
                        "label": s.label,
                        "value": s.value,
                        "unit": s.unit,
                        "delta_pct": s.delta_pct,
                        "trend": s.trend,
                    }
                    for s in b.stats
                ]
            if b.rows:
                info["row_count"] = len(b.rows)
                info["rows"] = b.rows
                if b.columns:
                    info["columns"] = b.columns
            if b.content:
                info["content"] = b.content
            blocks_summary.append(info)

        # Try LLM
        if settings.OPENAI_API_KEY and question:
            try:
                prompt = build_blocks_summarizer_prompt(
                    question=question,
                    blocks_summary=blocks_summary,
                    explanation_level=explanation_level,
                )
                payload = {
                    "model": settings.OPENAI_SUMMARIZER_MODEL,
                    "input": [
                        {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                    ],
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
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            text = content.get("text", "").strip()
                            if text:
                                return text
            except Exception as exc:
                logger.warning("agent_blocks_summarize_failed error=%s", type(exc).__name__)

        # Template fallback: concatenate block titles + stat highlights
        parts: list[str] = []
        for b in blocks:
            if b.stats:
                highlights = ", ".join(
                    f"{s.label}: {s.value}{(' ' + s.unit) if s.unit else ''}" for s in b.stats[:4]
                )
                parts.append(highlights)
            elif b.rows:
                parts.append(f"{b.title or 'Data'}: {len(b.rows)} rows returned")
            elif b.content:
                parts.append(b.content[:100])
        return ". ".join(parts) if parts else "Results generated successfully."

    async def _llm_summarize(
        self,
        *,
        plan: AgentPlan,
        columns: list[str],
        rows: list[list[Any]],
        question: str,
        interpreted_question: str,
        caveats: list[str],
    ) -> str | None:
        """Call OpenAI to generate a natural-language summary. Returns None on failure."""
        try:
            prompt = build_summarizer_prompt(
                question=question,
                interpreted_question=interpreted_question,
                plan_summary={
                    "intent": plan.intent,
                    "metrics": plan.metrics,
                    "dimensions": plan.dimensions,
                    "time_grain": plan.time_grain,
                    "filters": plan.filters,
                },
                columns=columns,
                rows=rows,
                caveats=caveats,
            )
            payload = {
                "model": settings.OPENAI_SUMMARIZER_MODEL,
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                ],
            }
            headers = {
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{settings.OPENAI_BASE_URL.rstrip('/')}/responses",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()

            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text = content.get("text", "").strip()
                        if text:
                            return text
            return None
        except Exception as exc:
            logger.warning(
                "agent_llm_summarize_failed error_type=%s",
                type(exc).__name__,
            )
            return None

    def _template_summarize(self, plan: AgentPlan, columns: list[str], rows: list[list[Any]]) -> str:
        """Deterministic template-based summarization as fallback."""
        row_objects = [
            {column: row[index] for index, column in enumerate(columns)}
            for row in rows
        ]
        summary = self._comparison_summary(plan, row_objects)
        if summary:
            return self._append_truncation(summary, plan, rows)

        summary = self._trend_summary(plan, row_objects)
        if summary:
            return self._append_truncation(summary, plan, rows)

        summary = self._breakdown_summary(plan, row_objects)
        if summary:
            return self._append_truncation(summary, plan, rows)

        summary = self._scalar_summary(plan, row_objects)
        if summary:
            return summary

        return self._append_truncation(
            f"Returned {len(rows)} rows across {len(columns)} columns.",
            plan,
            rows,
        )

    def _comparison_summary(self, plan: AgentPlan, rows: list[dict[str, Any]]) -> str | None:
        if not (plan.compare_mode and plan.metrics):
            return None
        metric = plan.metrics[0]
        delta_key = f"delta_{metric}_pct"
        current_key = metric
        comparison_key = f"comparison_{metric}"
        comparable = [
            row for row in rows
            if isinstance(row.get(delta_key), (int, float))
        ]
        if not comparable:
            return None
        leader = max(comparable, key=lambda row: float(row[delta_key]))
        laggard = min(comparable, key=lambda row: float(row[delta_key]))
        label_key = "time" if plan.time_grain != "all" else (plan.dimensions[0] if plan.dimensions else None)
        if label_key:
            return (
                f"Best visible change is {leader.get(label_key)} at {round(float(leader[delta_key]), 1)}% "
                f"for {metric.replace('_', ' ')}, versus {leader.get(comparison_key)} in the comparison window. "
                f"Weakest visible change is {laggard.get(label_key)} at {round(float(laggard[delta_key]), 1)}%."
            )
        return (
            f"{metric.replace('_', ' ')} is {leader.get(current_key)} versus {leader.get(comparison_key)} "
            f"in the comparison window, a change of {round(float(leader[delta_key]), 1)}%."
        )

    def _trend_summary(self, plan: AgentPlan, rows: list[dict[str, Any]]) -> str | None:
        if not (plan.time_grain != "all" and plan.metrics):
            return None
        metric = plan.metrics[0]
        numeric_rows = [row for row in rows if isinstance(row.get(metric), (int, float))]
        if not numeric_rows:
            return None
        peak = max(numeric_rows, key=lambda row: float(row[metric]))
        trough = min(numeric_rows, key=lambda row: float(row[metric]))
        return (
            f"{metric.replace('_', ' ')} peaked at {peak.get(metric)} on {peak.get('time')} "
            f"and bottomed at {trough.get(metric)} on {trough.get('time')} "
            f"across {len(numeric_rows)} visible time buckets."
        )

    def _breakdown_summary(self, plan: AgentPlan, rows: list[dict[str, Any]]) -> str | None:
        if not (plan.dimensions and plan.metrics):
            return None
        primary_metric = plan.metrics[0]
        label_key = plan.dimensions[0]
        numeric_rows = [row for row in rows if isinstance(row.get(primary_metric), (int, float))]
        if not numeric_rows:
            return None
        leader = max(numeric_rows, key=lambda row: float(row[primary_metric]))
        summary = (
            f"Top visible {label_key.replace('_', ' ')} is {leader.get(label_key)} "
            f"at {leader.get(primary_metric)} for {primary_metric.replace('_', ' ')}."
        )
        if len(plan.metrics) > 1:
            secondary_metric = plan.metrics[1]
            if isinstance(leader.get(secondary_metric), (int, float)):
                summary += f" The same row reports {leader.get(secondary_metric)} for {secondary_metric.replace('_', ' ')}."
        return summary

    def _scalar_summary(self, plan: AgentPlan, rows: list[dict[str, Any]]) -> str | None:
        if not plan.metrics:
            return None
        metric = plan.metrics[0]
        value = rows[0].get(metric)
        if value is None and len(rows[0]) == 1:
            value = next(iter(rows[0].values()))
        if value is None:
            return None
        label = metric.replace("_", " ").title()
        date_range = plan.filters.get("date_range", "all")
        period = f" ({date_range.replace('_', ' ')})" if date_range and date_range != "all" else ""
        return f"{label} is {value}{period}."

    def _append_truncation(self, summary: str, plan: AgentPlan, rows: list[list[Any]]) -> str:
        if len(rows) >= plan.limit and plan.intent in {"top_n", "breakdown", "comparison", "raw_table"}:
            return summary + " Results shown are limited to the visible capped row set."
        return summary
