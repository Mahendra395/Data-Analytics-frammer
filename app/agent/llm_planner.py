"""OpenAI-backed structured planner with retry, repair, and deterministic fallback."""
from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.agent.planner import DeterministicPlanner, PlannedQuestion
from app.agent.prompts import build_planner_prompt, build_repair_prompt
from app.agent.schemas import AgentPlan, AgentValidationIssue, ChartRequest, SortRule
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class _PlannerResponse(BaseModel):
    interpreted_question: str
    intent: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    time_grain: str = "all"
    compare_mode: str | None = None
    order_by: list[SortRule] = Field(default_factory=list)
    limit: int = 50
    chart: ChartRequest | None = None
    explanation_level: str = "normal"
    execution_strategy: str = "sql_query"
    service_name: str | None = None
    sub_plans: list[dict[str, Any]] | None = None


class OpenAIPlanner:
    async def plan(
        self,
        *,
        question: str,
        base_filters: dict[str, Any],
        allowed_client_slugs: tuple[str, ...],
        model: str | None = None,
    ) -> PlannedQuestion:
        used_model = model or settings.OPENAI_PLANNER_MODEL
        payload = {
            "model": used_model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": build_planner_prompt(
                                base_filters=base_filters,
                                allowed_client_slugs=allowed_client_slugs,
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": question}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_plan",
                    "strict": True,
                    "schema": _planner_json_schema(),
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_S) as client:
            response = await client.post(
                f"{settings.OPENAI_BASE_URL.rstrip('/')}/responses",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        text_payload = _extract_output_text(body)
        parsed = _PlannerResponse.model_validate_json(text_payload)
        cleaned_filters = {
            key: value for key, value in parsed.filters.items() if value is not None
        }
        plan = AgentPlan(
            intent=parsed.intent,
            metrics=parsed.metrics,
            dimensions=parsed.dimensions,
            filters=cleaned_filters,
            time_grain=parsed.time_grain,
            compare_mode=parsed.compare_mode,
            order_by=parsed.order_by,
            limit=parsed.limit,
            chart=parsed.chart,
            explanation_level=parsed.explanation_level,
            execution_strategy=parsed.execution_strategy or "sql_query",
            service_name=parsed.service_name,
            sub_plans=parsed.sub_plans,
        )
        return PlannedQuestion(
            interpreted_question=parsed.interpreted_question,
            plan=plan,
            planner_source="openai",
            planner_model=used_model,
            planner_confidence=0.92,
        )

    async def repair(
        self,
        *,
        original_question: str,
        failed_plan: dict[str, Any],
        validation_errors: list[dict[str, Any]],
        base_filters: dict[str, Any],
        allowed_client_slugs: tuple[str, ...],
        model: str | None = None,
    ) -> PlannedQuestion:
        """Ask the LLM to fix a plan that failed validation."""
        used_model = model or settings.OPENAI_PLANNER_MODEL
        repair_prompt = build_repair_prompt(
            original_question=original_question,
            failed_plan=failed_plan,
            validation_errors=validation_errors,
            base_filters=base_filters,
            allowed_client_slugs=allowed_client_slugs,
        )
        payload = {
            "model": used_model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": repair_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the plan now."}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_plan",
                    "strict": True,
                    "schema": _planner_json_schema(),
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_S) as client:
            response = await client.post(
                f"{settings.OPENAI_BASE_URL.rstrip('/')}/responses",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        text_payload = _extract_output_text(body)
        parsed = _PlannerResponse.model_validate_json(text_payload)
        cleaned_filters = {
            key: value for key, value in parsed.filters.items() if value is not None
        }
        plan = AgentPlan(
            intent=parsed.intent,
            metrics=parsed.metrics,
            dimensions=parsed.dimensions,
            filters=cleaned_filters,
            time_grain=parsed.time_grain,
            compare_mode=parsed.compare_mode,
            order_by=parsed.order_by,
            limit=parsed.limit,
            chart=parsed.chart,
            explanation_level=parsed.explanation_level,
            execution_strategy=parsed.execution_strategy or "sql_query",
            service_name=parsed.service_name,
            sub_plans=parsed.sub_plans,
        )
        return PlannedQuestion(
            interpreted_question=parsed.interpreted_question,
            plan=plan,
            planner_source="repair",
            planner_model=used_model,
            planner_confidence=0.80,
        )


class AgentPlanner:
    """Hybrid planner: OpenAI with retry + secondary model fallback + deterministic last resort."""

    def __init__(self) -> None:
        self._fallback = DeterministicPlanner()
        self._openai = OpenAIPlanner()

    async def plan(
        self,
        question: str,
        *,
        supplied_plan: AgentPlan | None = None,
        base_filters: dict[str, Any] | None = None,
        allowed_client_slugs: tuple[str, ...] = (),
    ) -> PlannedQuestion:
        if supplied_plan is not None:
            planned = self._fallback.plan(question, supplied_plan=supplied_plan)
            return PlannedQuestion(
                interpreted_question=planned.interpreted_question,
                plan=planned.plan,
                planner_source=planned.planner_source,
                planner_model=planned.planner_model,
                planner_confidence=1.0,
            )

        base_filters = base_filters or {}
        fallback_reason: str | None = None

        if settings.OPENAI_API_KEY:
            # ── Try primary model with retries ─────────────────────────────
            result = await self._try_openai_with_retries(
                question=question,
                base_filters=base_filters,
                allowed_client_slugs=allowed_client_slugs,
                model=settings.OPENAI_PLANNER_MODEL,
                label="primary",
            )
            if result is not None:
                return result

            # ── Try secondary model (single attempt) ──────────────────────
            if settings.OPENAI_SECONDARY_MODEL and settings.OPENAI_SECONDARY_MODEL != settings.OPENAI_PLANNER_MODEL:
                result = await self._try_openai_with_retries(
                    question=question,
                    base_filters=base_filters,
                    allowed_client_slugs=allowed_client_slugs,
                    model=settings.OPENAI_SECONDARY_MODEL,
                    label="secondary",
                    max_retries=1,
                )
                if result is not None:
                    return result

            fallback_reason = "all_llm_attempts_exhausted"
        else:
            fallback_reason = "openai_not_configured"

        # ── Deterministic fallback ─────────────────────────────────────────
        fallback = self._fallback.plan(question)
        merged_filters = dict(base_filters)
        merged_filters.update(fallback.plan.filters)
        return PlannedQuestion(
            interpreted_question=fallback.interpreted_question,
            plan=fallback.plan.model_copy(update={"filters": merged_filters}),
            planner_source=fallback.planner_source,
            planner_model=fallback.planner_model,
            planner_confidence=fallback.planner_confidence or 0.30,
            planner_fallback_reason=fallback_reason,
        )

    async def repair_plan(
        self,
        *,
        question: str,
        failed_plan: AgentPlan,
        validation_issues: list[AgentValidationIssue],
        base_filters: dict[str, Any],
        allowed_client_slugs: tuple[str, ...],
    ) -> PlannedQuestion | None:
        """Ask the LLM to fix a plan that failed validation. Returns None if LLM unavailable."""
        if not settings.OPENAI_API_KEY:
            return None
        try:
            return await self._openai.repair(
                original_question=question,
                failed_plan=failed_plan.model_dump(mode="json"),
                validation_errors=[issue.model_dump() for issue in validation_issues],
                base_filters=base_filters,
                allowed_client_slugs=allowed_client_slugs,
            )
        except Exception as exc:
            logger.warning(
                "agent_repair_failed error_type=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            return None

    async def _try_openai_with_retries(
        self,
        *,
        question: str,
        base_filters: dict[str, Any],
        allowed_client_slugs: tuple[str, ...],
        model: str,
        label: str,
        max_retries: int | None = None,
    ) -> PlannedQuestion | None:
        """Attempt OpenAI planning with exponential backoff retries."""
        retries = max_retries if max_retries is not None else settings.OPENAI_MAX_RETRIES
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                result = await self._openai.plan(
                    question=question,
                    base_filters=base_filters,
                    allowed_client_slugs=allowed_client_slugs,
                    model=model,
                )
                if attempt > 1:
                    logger.info(
                        "agent_planner_recovered model=%s label=%s attempt=%d",
                        model, label, attempt,
                    )
                return result
            except Exception as exc:
                last_exc = exc
                reason = self._classify_openai_failure(exc)
                logger.warning(
                    "agent_planner_attempt_failed label=%s model=%s attempt=%d/%d reason=%s error_type=%s",
                    label, model, attempt, retries, reason, type(exc).__name__,
                )
                # Don't retry on auth errors or bad requests — they won't self-heal
                if reason in ("openai_auth_error", "openai_bad_request"):
                    break
                if attempt < retries:
                    backoff = min(2 ** (attempt - 1), 4)  # 1s, 2s, 4s cap
                    await asyncio.sleep(backoff)

        return None

    def _classify_openai_failure(self, exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "openai_timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 400:
                return "openai_bad_request"
            if status_code == 401:
                return "openai_auth_error"
            if status_code == 429:
                return "openai_rate_limit"
            if 500 <= status_code <= 599:
                return "openai_server_error"
            return f"openai_http_{status_code}"
        if isinstance(exc, ValueError):
            return "openai_schema_error"
        return "openai_unknown_error"


def _extract_output_text(body: dict[str, Any]) -> str:
    for item in body.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                return content.get("text", "")
            if content.get("type") == "refusal":
                raise ValueError(content.get("refusal", "Planner refused request"))
    raise ValueError("Responses API returned no output_text content")


def _planner_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "interpreted_question": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": [
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
                ],
            },
            "metrics": {"type": "array", "items": {"type": "string"}},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "filters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date_range": {"type": ["string", "null"]},
                    "client": {"type": ["string", "null"]},
                    "channel": {"type": ["string", "null"]},
                    "language": {"type": ["string", "null"]},
                    "team_member": {"type": ["string", "null"]},
                    "input_type": {"type": ["string", "null"]},
                    "output_type": {"type": ["string", "null"]},
                    "published_flag": {"type": ["boolean", "null"]},
                    "published_platform": {"type": ["string", "null"]},
                    "billable_flag": {"type": ["boolean", "null"]},
                },
                "required": [
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
                ],
            },
            "time_grain": {
                "type": "string",
                "enum": ["day", "week", "month", "quarter", "year", "all"],
            },
            "compare_mode": {
                "type": ["string", "null"],
                "enum": ["previous_period", "previous_month", "previous_year", None],
            },
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "field": {"type": "string"},
                        "direction": {"type": "string", "enum": ["asc", "desc"]},
                    },
                    "required": ["field", "direction"],
                },
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": settings.AGENT_MAX_LIMIT},
            "chart": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["auto", "bar", "line", "area", "table", "stat", "pie"],
                    },
                    "x": {"type": ["string", "null"]},
                    "y": {"type": ["string", "null"]},
                    "series": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": ["string", "null"]},
                },
                "required": ["type", "x", "y", "series", "title"],
            },
            "explanation_level": {
                "type": "string",
                "enum": ["short", "normal", "detailed"],
            },
            "execution_strategy": {
                "type": "string",
                "enum": ["sql_query", "service_call", "multi_query"],
            },
            "service_name": {
                "type": ["string", "null"],
                "enum": ["kpis", "growth", "quality_summary", "funnel", "monthly_trend", None],
            },
            "sub_plans": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "interpreted_question": {"type": "string"},
                        "intent": {"type": "string"},
                        "metrics": {"type": "array", "items": {"type": "string"}},
                        "dimensions": {"type": "array", "items": {"type": "string"}},
                        "filters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "date_range": {"type": ["string", "null"]},
                                "client": {"type": ["string", "null"]},
                                "channel": {"type": ["string", "null"]},
                                "language": {"type": ["string", "null"]},
                                "team_member": {"type": ["string", "null"]},
                                "input_type": {"type": ["string", "null"]},
                                "output_type": {"type": ["string", "null"]},
                                "published_flag": {"type": ["boolean", "null"]},
                                "published_platform": {"type": ["string", "null"]},
                                "billable_flag": {"type": ["boolean", "null"]},
                            },
                            "required": [
                                "date_range", "client", "channel", "language",
                                "team_member", "input_type", "output_type",
                                "published_flag", "published_platform", "billable_flag",
                            ],
                        },
                        "time_grain": {"type": "string"},
                        "compare_mode": {"type": ["string", "null"]},
                        "order_by": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "field": {"type": "string"},
                                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                                },
                                "required": ["field", "direction"],
                            },
                        },
                        "limit": {"type": "integer"},
                        "chart": {
                            "type": ["object", "null"],
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string", "enum": ["auto", "bar", "line", "area", "table", "stat", "pie"]},
                                "x": {"type": ["string", "null"]},
                                "y": {"type": ["string", "null"]},
                                "series": {"type": "array", "items": {"type": "string"}},
                                "title": {"type": ["string", "null"]},
                            },
                            "required": ["type", "x", "y", "series", "title"],
                        },
                        "explanation_level": {"type": "string"},
                    },
                    "required": [
                        "interpreted_question", "intent", "metrics", "dimensions",
                        "filters", "time_grain", "compare_mode", "order_by",
                        "limit", "chart", "explanation_level",
                    ],
                },
            },
        },
        "required": [
            "interpreted_question",
            "intent",
            "metrics",
            "dimensions",
            "filters",
            "time_grain",
            "compare_mode",
            "order_by",
            "limit",
            "chart",
            "explanation_level",
            "execution_strategy",
            "service_name",
            "sub_plans",
        ],
    }
