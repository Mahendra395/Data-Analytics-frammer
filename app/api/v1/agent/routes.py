"""POST /api/v1/agent/* semantic analytics endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import agent_audit_logger, agent_execution_cache, build_agent_cache_key
from app.agent.chart_planner import AgentChartPlanner
from app.agent.executor import AgentExecutor
from app.agent.llm_planner import AgentPlanner
from app.agent.scope import resolve_plan_scope
from app.agent.schemas import (
    AgentExecuteRequest,
    AgentPlan,
    AgentPlanResult,
    AgentQueryRequest,
    AgentQueryResponse,
    NON_SQL_INTENTS,
    ResponseBlock,
)
from app.agent.service_router import ServiceRouter
from app.agent.sql_compiler import AgentSQLCompiler
from app.agent.summarizer import AgentSummarizer
from app.agent.validator import AgentPlanValidator
from app.api.deps import FilterParams, get_current_user, get_db
from app.core.config import get_settings
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY
from app.schemas.responses import ApiResponse, ResponseMetadata

router = APIRouter(prefix="/agent", tags=["Agent"])
logger = logging.getLogger(__name__)
settings = get_settings()

_planner = AgentPlanner()
_validator = AgentPlanValidator()
_compiler = AgentSQLCompiler()
_executor = AgentExecutor()
_chart_planner = AgentChartPlanner()
_summarizer = AgentSummarizer()
_service_router = ServiceRouter()


# ── /plan ──────────────────────────────────────────────────────────────────────

@router.post("/plan", response_model=ApiResponse[AgentPlanResult])
async def plan_query(
    body: AgentQueryRequest,
    f: FilterParams = Depends(),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ApiResponse[AgentPlanResult]:
    base_scope = resolve_plan_scope(
        plan=body.plan or AgentPlan(intent="diagnostic"),
        context=body.context,
        filter_params=f,
        current_user=current_user,
    )
    planned = await _planner.plan(
        body.question,
        supplied_plan=body.plan,
        base_filters=base_scope.plan.filters,
        allowed_client_slugs=base_scope.allowed_client_slugs,
    )
    resolved_scope = resolve_plan_scope(
        plan=planned.plan,
        context=body.context,
        filter_params=f,
        current_user=current_user,
    )
    validation = _validator.validate(resolved_scope.plan)
    data = AgentPlanResult(
        question=body.question,
        interpreted_question=planned.interpreted_question,
        plan=validation.plan,
        resolved_filters=resolved_scope.metadata_filters,
        planner_source=planned.planner_source,
        planner_model=planned.planner_model,
        planner_confidence=planned.planner_confidence,
        planner_fallback_reason=planned.planner_fallback_reason,
        caveats=validation.caveats,
        follow_ups=validation.follow_ups,
        validation_issues=validation.issues,
    )
    audit_id = agent_audit_logger.record(
        {
            "endpoint": "agent.plan",
            "status": "planned" if validation.is_valid else "validation_failed",
            "question": body.question,
            "interpreted_question": planned.interpreted_question,
            "plan": validation.plan.model_dump(mode="json"),
            "resolved_filters": resolved_scope.metadata_filters,
            "planner_source": planned.planner_source,
            "planner_model": planned.planner_model,
            "planner_confidence": planned.planner_confidence,
            "planner_fallback_reason": planned.planner_fallback_reason,
            "validation_issues": [issue.model_dump() for issue in validation.issues],
        }
    )
    return ApiResponse(
        data=data,
        meta=_build_agent_metadata(
            resolved_scope.metadata_filters,
            validation.plan,
            "agent-plan",
            validation.caveats,
            planner_source=planned.planner_source,
            planner_model=planned.planner_model,
            planner_confidence=planned.planner_confidence,
            planner_fallback_reason=planned.planner_fallback_reason,
            audit_id=audit_id,
        ),
    )


# ── /execute ───────────────────────────────────────────────────────────────────

@router.post("/execute", response_model=ApiResponse[AgentQueryResponse])
async def execute_plan(
    body: AgentExecuteRequest,
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ApiResponse[AgentQueryResponse]:
    resolved_scope = resolve_plan_scope(
        plan=body.plan,
        context=body.context,
        filter_params=f,
        current_user=current_user,
    )

    # ── Handle non-SQL intents ─────────────────────────────────────────────
    if resolved_scope.plan.intent in NON_SQL_INTENTS:
        response = _build_non_sql_response(
            question=body.question or "",
            plan=resolved_scope.plan,
            resolved_filters=resolved_scope.metadata_filters,
        )
        audit_id = agent_audit_logger.record({
            "endpoint": "agent.execute",
            "status": "success",
            "question": body.question,
            "plan": resolved_scope.plan.model_dump(mode="json"),
            "resolved_filters": resolved_scope.metadata_filters,
            "planner_source": "supplied_plan",
            "non_sql_intent": resolved_scope.plan.intent,
        })
        return ApiResponse(
            data=response,
            meta=_build_agent_metadata(
                resolved_scope.metadata_filters,
                resolved_scope.plan,
                "agent-query",
                [],
                planner_source="supplied_plan",
                planner_confidence=1.0,
                audit_id=audit_id,
            ),
        )

    # ── Handle service_call strategy ───────────────────────────────────────
    if resolved_scope.plan.execution_strategy == "service_call" and resolved_scope.plan.service_name:
        import time as _time
        started = _time.perf_counter()
        blocks = await _service_router.execute(
            resolved_scope.plan.service_name,
            db,
            _build_filter_params_from_scope(resolved_scope.metadata_filters, f),
        )
        elapsed_ms = round((_time.perf_counter() - started) * 1000, 2)
        summary = await _summarizer.summarize_blocks(
            blocks, question=body.question or "",
            explanation_level=resolved_scope.plan.explanation_level,
        )
        primary_columns, primary_rows, primary_chart = _extract_primary_block(blocks)
        response = AgentQueryResponse(
            question=body.question or "",
            interpreted_question=body.question or "",
            plan=resolved_scope.plan,
            resolved_filters=resolved_scope.metadata_filters,
            planner_source="service",
            planner_confidence=1.0,
            sql=None,
            sql_params={},
            columns=primary_columns,
            rows=primary_rows,
            chart_spec=primary_chart,
            summary=summary,
            caveats=[],
            follow_ups=[],
            execution_time_ms=elapsed_ms,
            row_count=sum(len(b.rows) for b in blocks),
            blocks=blocks,
        )
        audit_id = agent_audit_logger.record({
            "endpoint": "agent.execute",
            "status": "success",
            "question": body.question,
            "plan": resolved_scope.plan.model_dump(mode="json"),
            "resolved_filters": resolved_scope.metadata_filters,
            "planner_source": "service",
            "service_name": resolved_scope.plan.service_name,
            "execution_time_ms": elapsed_ms,
        })
        return ApiResponse(
            data=response,
            meta=_build_agent_metadata(
                resolved_scope.metadata_filters,
                resolved_scope.plan,
                "agent-query",
                [],
                planner_source="service",
                planner_confidence=1.0,
                audit_id=audit_id,
            ),
        )

    # ── Validate + auto-repair ─────────────────────────────────────────────
    validation = _validator.validate(resolved_scope.plan)
    if not validation.is_valid:
        repaired_plan, repair_changes = _validator.auto_repair(resolved_scope.plan)
        validation = _validator.validate(repaired_plan)
        if repair_changes:
            logger.info("agent_auto_repair changes=%s", repair_changes)
        if not validation.is_valid:
            agent_audit_logger.record({
                "endpoint": "agent.execute",
                "status": "validation_failed",
                "question": body.question,
                "plan": validation.plan.model_dump(mode="json"),
                "resolved_filters": resolved_scope.metadata_filters,
                "validation_issues": [issue.model_dump() for issue in validation.issues],
                "repair_attempted": bool(repair_changes),
            })
            raise HTTPException(
                status_code=422,
                detail=[issue.model_dump() for issue in validation.issues],
            )

    cache_key = build_agent_cache_key(
        scope="execute",
        plan=validation.plan.model_dump(mode="json"),
        allowed_client_slugs=resolved_scope.allowed_client_slugs,
    )
    cache_hit = False
    cached_response = agent_execution_cache.get(cache_key)
    if cached_response is not None:
        cache_hit = True
        response = AgentQueryResponse.model_validate(cached_response).model_copy(
            update={
                "question": body.question or "",
                "interpreted_question": body.question or "",
                "plan": validation.plan,
                "resolved_filters": resolved_scope.metadata_filters,
            }
        )
    else:
        try:
            compiled = _compiler.compile(
                validation.plan,
                allowed_client_slugs=resolved_scope.allowed_client_slugs,
            )
            columns, rows, elapsed_ms = await _executor.execute(db, compiled)
            summary = await _summarizer.summarize(
                validation.plan, columns, rows,
                question=body.question or "",
                interpreted_question=body.question or "",
                caveats=validation.caveats,
            )
            response = AgentQueryResponse(
                question=body.question or "",
                interpreted_question=body.question or "",
                plan=validation.plan,
                resolved_filters=resolved_scope.metadata_filters,
                planner_source="supplied_plan",
                planner_model=None,
                planner_confidence=1.0,
                planner_fallback_reason=None,
                sql=compiled.sql,
                sql_params=compiled.params,
                columns=columns,
                rows=rows,
                chart_spec=_chart_planner.build_chart_spec(validation.plan, columns, rows),
                summary=summary,
                caveats=validation.caveats,
                follow_ups=validation.follow_ups,
                execution_time_ms=elapsed_ms,
                row_count=len(rows),
            )
            agent_execution_cache.set(cache_key, response.model_dump(mode="json"))
        except Exception as exc:
            audit_id = agent_audit_logger.record({
                "endpoint": "agent.execute",
                "status": "execution_failed",
                "question": body.question,
                "plan": validation.plan.model_dump(mode="json"),
                "resolved_filters": resolved_scope.metadata_filters,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            logger.exception("agent_execute_failed audit_id=%s", audit_id)
            raise

    audit_id = agent_audit_logger.record({
        "endpoint": "agent.execute",
        "status": "success",
        "question": body.question,
        "plan": validation.plan.model_dump(mode="json"),
        "resolved_filters": resolved_scope.metadata_filters,
        "planner_source": response.planner_source,
        "sql": response.sql,
        "execution_time_ms": response.execution_time_ms,
        "row_count": response.row_count,
        "cache_hit": cache_hit,
    })

    return ApiResponse(
        data=response,
        meta=_build_agent_metadata(
            resolved_scope.metadata_filters,
            validation.plan,
            "agent-query",
            validation.caveats,
            planner_source=response.planner_source,
            planner_model=response.planner_model,
            planner_confidence=response.planner_confidence,
            planner_fallback_reason=response.planner_fallback_reason,
            cache_hit=cache_hit,
            audit_id=audit_id,
        ),
    )


# ── /query (end-to-end: plan → validate → repair → execute → summarize) ──────

@router.post("/query", response_model=ApiResponse[AgentQueryResponse])
async def query_agent(
    body: AgentQueryRequest,
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ApiResponse[AgentQueryResponse]:
    base_scope = resolve_plan_scope(
        plan=body.plan or AgentPlan(intent="diagnostic"),
        context=body.context,
        filter_params=f,
        current_user=current_user,
    )
    planned = await _planner.plan(
        body.question,
        supplied_plan=body.plan,
        base_filters=base_scope.plan.filters,
        allowed_client_slugs=base_scope.allowed_client_slugs,
    )
    resolved_scope = resolve_plan_scope(
        plan=planned.plan,
        context=body.context,
        filter_params=f,
        current_user=current_user,
    )

    # ── Confidence gate ────────────────────────────────────────────────────
    # If confidence is too low, return a clarification instead of garbage data.
    if (
        planned.planner_confidence is not None
        and planned.planner_confidence < settings.AGENT_MIN_CONFIDENCE
        and planned.plan.intent not in NON_SQL_INTENTS
    ):
        response = _build_clarification_response(
            question=body.question,
            plan=planned.plan,
            resolved_filters=resolved_scope.metadata_filters,
            planner_source=planned.planner_source,
            planner_model=planned.planner_model,
            planner_confidence=planned.planner_confidence,
            planner_fallback_reason=planned.planner_fallback_reason,
        )
        audit_id = agent_audit_logger.record({
            "endpoint": "agent.query",
            "status": "low_confidence_clarification",
            "question": body.question,
            "interpreted_question": planned.interpreted_question,
            "plan": planned.plan.model_dump(mode="json"),
            "planner_source": planned.planner_source,
            "planner_confidence": planned.planner_confidence,
            "planner_fallback_reason": planned.planner_fallback_reason,
        })
        return ApiResponse(
            data=response,
            meta=_build_agent_metadata(
                resolved_scope.metadata_filters,
                planned.plan,
                "agent-query",
                [],
                planner_source=planned.planner_source,
                planner_model=planned.planner_model,
                planner_confidence=planned.planner_confidence,
                planner_fallback_reason=planned.planner_fallback_reason,
                audit_id=audit_id,
            ),
        )

    # ── Handle non-SQL intents ─────────────────────────────────────────────
    if resolved_scope.plan.intent in NON_SQL_INTENTS:
        response = _build_non_sql_response(
            question=body.question,
            plan=resolved_scope.plan,
            resolved_filters=resolved_scope.metadata_filters,
            planner_source=planned.planner_source,
            planner_model=planned.planner_model,
            planner_confidence=planned.planner_confidence,
            planner_fallback_reason=planned.planner_fallback_reason,
        )
        audit_id = agent_audit_logger.record({
            "endpoint": "agent.query",
            "status": "success",
            "question": body.question,
            "interpreted_question": planned.interpreted_question,
            "plan": resolved_scope.plan.model_dump(mode="json"),
            "planner_source": planned.planner_source,
            "non_sql_intent": resolved_scope.plan.intent,
        })
        return ApiResponse(
            data=response,
            meta=_build_agent_metadata(
                resolved_scope.metadata_filters,
                resolved_scope.plan,
                "agent-query",
                [],
                planner_source=planned.planner_source,
                planner_model=planned.planner_model,
                planner_confidence=planned.planner_confidence,
                planner_fallback_reason=planned.planner_fallback_reason,
                audit_id=audit_id,
            ),
        )

    # ── Service-call / multi-query bypass ─────────────────────────────────
    # These strategies run their own SQL; skip the SQL-oriented validator.
    _strategy = getattr(resolved_scope.plan, "execution_strategy", None) or "sql_query"
    if _strategy in ("service_call", "multi_query"):
        validation = type(
            "_BypassResult", (),
            {"plan": resolved_scope.plan, "is_valid": True,
             "caveats": [], "follow_ups": [], "issues": []},
        )()
        repair_source = planned.planner_source
        repair_model = planned.planner_model
        repair_confidence = planned.planner_confidence
    else:
        # ── Validate → Repair loop ────────────────────────────────────────
        validation = _validator.validate(resolved_scope.plan)
        repair_source = planned.planner_source
        repair_model = planned.planner_model
        repair_confidence = planned.planner_confidence

    if not validation.is_valid:
        # Round 1: Try LLM-based repair
        for repair_round in range(1, settings.AGENT_MAX_REPAIR_ROUNDS + 1):
            repaired = await _planner.repair_plan(
                question=body.question,
                failed_plan=validation.plan,
                validation_issues=validation.issues,
                base_filters=base_scope.plan.filters,
                allowed_client_slugs=base_scope.allowed_client_slugs,
            )
            if repaired is not None:
                repaired_scope = resolve_plan_scope(
                    plan=repaired.plan,
                    context=body.context,
                    filter_params=f,
                    current_user=current_user,
                )
                validation = _validator.validate(repaired_scope.plan)
                repair_source = repaired.planner_source
                repair_model = repaired.planner_model
                repair_confidence = repaired.planner_confidence
                resolved_scope = repaired_scope
                if validation.is_valid:
                    logger.info(
                        "agent_llm_repair_succeeded round=%d question=%s",
                        repair_round, body.question[:80],
                    )
                    break
            else:
                break

        # Round 2: If LLM repair failed, try deterministic auto-repair
        if not validation.is_valid:
            repaired_plan, repair_changes = _validator.auto_repair(resolved_scope.plan)
            if repair_changes:
                logger.info("agent_auto_repair changes=%s", repair_changes)
                repaired_scope = resolve_plan_scope(
                    plan=repaired_plan,
                    context=body.context,
                    filter_params=f,
                    current_user=current_user,
                )
                validation = _validator.validate(repaired_scope.plan)
                resolved_scope = repaired_scope

        # If still invalid after all repair attempts, return a useful error
        if not validation.is_valid:
            # If the plan is unrecoverable, return clarification instead of 422
            repaired_plan, _ = _validator.auto_repair(validation.plan)
            if repaired_plan.intent == "clarification":
                response = _build_clarification_response(
                    question=body.question,
                    plan=repaired_plan,
                    resolved_filters=resolved_scope.metadata_filters,
                    planner_source=repair_source,
                    planner_model=repair_model,
                    planner_confidence=repair_confidence,
                    planner_fallback_reason="plan_unrecoverable",
                )
                audit_id = agent_audit_logger.record({
                    "endpoint": "agent.query",
                    "status": "plan_unrecoverable",
                    "question": body.question,
                    "plan": validation.plan.model_dump(mode="json"),
                    "validation_issues": [i.model_dump() for i in validation.issues],
                })
                return ApiResponse(
                    data=response,
                    meta=_build_agent_metadata(
                        resolved_scope.metadata_filters,
                        repaired_plan,
                        "agent-query",
                        [],
                        planner_source=repair_source,
                        planner_model=repair_model,
                        planner_confidence=repair_confidence,
                        planner_fallback_reason="plan_unrecoverable",
                        audit_id=audit_id,
                    ),
                )

            agent_audit_logger.record({
                "endpoint": "agent.query",
                "status": "validation_failed",
                "question": body.question,
                "interpreted_question": planned.interpreted_question,
                "plan": validation.plan.model_dump(mode="json"),
                "resolved_filters": resolved_scope.metadata_filters,
                "planner_source": repair_source,
                "validation_issues": [i.model_dump() for i in validation.issues],
            })
            raise HTTPException(
                status_code=422,
                detail=[issue.model_dump() for issue in validation.issues],
            )

    # ── Execute ────────────────────────────────────────────────────────────
    cache_key = build_agent_cache_key(
        scope="query",
        plan=validation.plan.model_dump(mode="json"),
        allowed_client_slugs=resolved_scope.allowed_client_slugs,
    )
    cache_hit = False
    cached_response = agent_execution_cache.get(cache_key)
    if cached_response is not None:
        cache_hit = True
        response = AgentQueryResponse.model_validate(cached_response).model_copy(
            update={
                "question": body.question,
                "interpreted_question": planned.interpreted_question,
                "plan": validation.plan,
                "resolved_filters": resolved_scope.metadata_filters,
                "planner_source": repair_source,
                "planner_model": repair_model,
                "planner_confidence": repair_confidence,
                "planner_fallback_reason": planned.planner_fallback_reason,
            }
        )
    else:
        try:
            strategy = validation.plan.execution_strategy or "sql_query"

            if strategy == "service_call" and validation.plan.service_name:
                # ── Service call strategy ──────────────────────────────────
                import time as _time
                started = _time.perf_counter()
                blocks = await _service_router.execute(
                    validation.plan.service_name,
                    db,
                    _build_filter_params_from_scope(resolved_scope.metadata_filters, f),
                )
                elapsed_ms = round((_time.perf_counter() - started) * 1000, 2)
                summary = await _summarizer.summarize_blocks(
                    blocks, question=body.question,
                    explanation_level=validation.plan.explanation_level,
                )
                # Populate top-level fields from first data block for backward compat
                primary_columns, primary_rows, primary_chart = _extract_primary_block(blocks)
                response = AgentQueryResponse(
                    question=body.question,
                    interpreted_question=planned.interpreted_question,
                    plan=validation.plan,
                    resolved_filters=resolved_scope.metadata_filters,
                    planner_source=repair_source,
                    planner_model=repair_model,
                    planner_confidence=repair_confidence,
                    planner_fallback_reason=planned.planner_fallback_reason,
                    sql=None,
                    sql_params={},
                    columns=primary_columns,
                    rows=primary_rows,
                    chart_spec=primary_chart,
                    summary=summary,
                    caveats=validation.caveats,
                    follow_ups=validation.follow_ups,
                    execution_time_ms=elapsed_ms,
                    row_count=sum(len(b.rows) for b in blocks),
                    blocks=blocks,
                )

            elif strategy == "multi_query" and validation.plan.sub_plans:
                # ── Multi-query strategy ───────────────────────────────────
                import time as _time
                started = _time.perf_counter()
                blocks: list[ResponseBlock] = []
                all_columns: list[str] = []
                all_rows: list[list] = []
                total_elapsed = 0.0
                for sub_plan_dict in validation.plan.sub_plans[:4]:
                    try:
                        sub_plan = AgentPlan(**sub_plan_dict)
                        sub_validation = _validator.validate(sub_plan)
                        if not sub_validation.is_valid:
                            continue
                        sub_compiled = _compiler.compile(
                            sub_validation.plan,
                            allowed_client_slugs=resolved_scope.allowed_client_slugs,
                        )
                        cols, rws, sub_elapsed = await _executor.execute(db, sub_compiled)
                        total_elapsed += sub_elapsed
                        chart = _chart_planner.build_chart_spec(sub_validation.plan, cols, rws)
                        blocks.append(ResponseBlock(
                            block_type="chart" if chart and chart.chart_type != "table" else "table",
                            title=sub_plan_dict.get("interpreted_question", ""),
                            columns=cols,
                            rows=rws,
                            chart_spec=chart,
                            sql=sub_compiled.sql,
                        ))
                        if not all_columns:
                            all_columns = cols
                            all_rows = rws
                    except Exception as sub_exc:
                        logger.warning("agent_sub_plan_failed error=%s", sub_exc)
                        continue

                elapsed_ms = round((_time.perf_counter() - started) * 1000, 2)
                summary = await _summarizer.summarize_blocks(
                    blocks, question=body.question,
                    explanation_level=validation.plan.explanation_level,
                )
                primary_columns, primary_rows, primary_chart = _extract_primary_block(blocks)
                response = AgentQueryResponse(
                    question=body.question,
                    interpreted_question=planned.interpreted_question,
                    plan=validation.plan,
                    resolved_filters=resolved_scope.metadata_filters,
                    planner_source=repair_source,
                    planner_model=repair_model,
                    planner_confidence=repair_confidence,
                    planner_fallback_reason=planned.planner_fallback_reason,
                    sql=None,
                    sql_params={},
                    columns=primary_columns,
                    rows=primary_rows,
                    chart_spec=primary_chart,
                    summary=summary,
                    caveats=validation.caveats,
                    follow_ups=validation.follow_ups,
                    execution_time_ms=elapsed_ms,
                    row_count=sum(len(b.rows) for b in blocks),
                    blocks=blocks,
                )

            else:
                # ── Standard sql_query strategy (existing pipeline) ────────
                compiled = _compiler.compile(
                    validation.plan,
                    allowed_client_slugs=resolved_scope.allowed_client_slugs,
                )
                columns, rows, elapsed_ms = await _executor.execute(db, compiled)
                summary = await _summarizer.summarize(
                    validation.plan, columns, rows,
                    question=body.question,
                    interpreted_question=planned.interpreted_question,
                    caveats=validation.caveats,
                )
                chart_spec = _chart_planner.build_chart_spec(validation.plan, columns, rows)
                # Wrap into blocks for new clients
                blocks = _wrap_sql_result_as_blocks(
                    summary=summary, columns=columns, rows=rows,
                    chart_spec=chart_spec, sql=compiled.sql,
                )
                response = AgentQueryResponse(
                    question=body.question,
                    interpreted_question=planned.interpreted_question,
                    plan=validation.plan,
                    resolved_filters=resolved_scope.metadata_filters,
                    planner_source=repair_source,
                    planner_model=repair_model,
                    planner_confidence=repair_confidence,
                    planner_fallback_reason=planned.planner_fallback_reason,
                    sql=compiled.sql,
                    sql_params=compiled.params,
                    columns=columns,
                    rows=rows,
                    chart_spec=chart_spec,
                    summary=summary,
                    caveats=validation.caveats,
                    follow_ups=validation.follow_ups,
                    execution_time_ms=elapsed_ms,
                    row_count=len(rows),
                    blocks=blocks,
                )

            agent_execution_cache.set(cache_key, response.model_dump(mode="json"))
        except Exception as exc:
            audit_id = agent_audit_logger.record({
                "endpoint": "agent.query",
                "status": "execution_failed",
                "question": body.question,
                "interpreted_question": planned.interpreted_question,
                "plan": validation.plan.model_dump(mode="json"),
                "resolved_filters": resolved_scope.metadata_filters,
                "planner_source": repair_source,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            logger.exception("agent_query_failed audit_id=%s", audit_id)
            raise

    audit_id = agent_audit_logger.record({
        "endpoint": "agent.query",
        "status": "success",
        "question": body.question,
        "interpreted_question": planned.interpreted_question,
        "plan": validation.plan.model_dump(mode="json"),
        "resolved_filters": resolved_scope.metadata_filters,
        "planner_source": response.planner_source,
        "sql": response.sql,
        "execution_time_ms": response.execution_time_ms,
        "row_count": response.row_count,
        "cache_hit": cache_hit,
    })

    return ApiResponse(
        data=response,
        meta=_build_agent_metadata(
            resolved_scope.metadata_filters,
            validation.plan,
            "agent-query",
            validation.caveats,
            planner_source=response.planner_source,
            planner_model=response.planner_model,
            planner_confidence=response.planner_confidence,
            planner_fallback_reason=response.planner_fallback_reason,
            cache_hit=cache_hit,
            audit_id=audit_id,
        ),
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_filter_params_from_scope(
    metadata_filters: dict[str, Any],
    original_f: FilterParams,
) -> FilterParams:
    """Build a FilterParams instance using the agent's resolved scope.

    Reuses the original FilterParams (which has date resolution logic)
    and overrides dimension filters from the agent scope.
    """
    # The original FilterParams already has properly resolved dates.
    # We just return it since the agent scope is already reflected through it.
    return original_f


def _extract_primary_block(
    blocks: list[ResponseBlock],
) -> tuple[list[str], list[list], "ChartSpec | None"]:
    """Extract columns, rows, chart_spec from the first chart/table block for backward compat."""
    from app.agent.schemas import ChartSpec
    for block in blocks:
        if block.block_type in ("chart", "table") and block.columns:
            return block.columns, block.rows, block.chart_spec
    return [], [], None


def _wrap_sql_result_as_blocks(
    *,
    summary: str,
    columns: list[str],
    rows: list[list],
    chart_spec: "ChartSpec | None",
    sql: str | None,
) -> list[ResponseBlock]:
    """Wrap a standard SQL query result into ResponseBlocks for new clients."""
    blocks: list[ResponseBlock] = []
    blocks.append(ResponseBlock(
        block_type="markdown",
        title="Summary",
        content=summary,
    ))
    if columns and rows:
        block_type = "chart" if chart_spec and chart_spec.chart_type != "table" else "table"
        blocks.append(ResponseBlock(
            block_type=block_type,
            title=chart_spec.title if chart_spec else None,
            columns=columns,
            rows=rows,
            chart_spec=chart_spec,
            sql=sql,
        ))
    return blocks


# ── Response builders ──────────────────────────────────────────────────────────

def _build_non_sql_response(
    *,
    question: str,
    plan: AgentPlan,
    resolved_filters: dict[str, Any],
    planner_source: str = "supplied_plan",
    planner_model: str | None = None,
    planner_confidence: float | None = None,
    planner_fallback_reason: str | None = None,
) -> AgentQueryResponse:
    """Build responses for intents that don't need SQL execution."""
    intent = plan.intent
    if intent == "explain_metric" and plan.metrics:
        return _build_explainer_response(
            question=question,
            plan=plan,
            resolved_filters=resolved_filters,
            caveats=[],
            follow_ups=[],
            planner_source=planner_source,
            planner_model=planner_model,
            planner_confidence=planner_confidence,
            planner_fallback_reason=planner_fallback_reason,
        )

    if intent == "schema_info":
        summary = _build_schema_summary()
        follow_ups = [
            "Show me total_uploaded by client.",
            "What is the publish rate?",
            "Show monthly upload trend.",
        ]
    elif intent == "capabilities":
        summary = _build_capabilities_summary()
        follow_ups = [
            "How many videos were uploaded last month?",
            "Top 5 users by publish rate.",
            "Show upload trend weekly.",
        ]
    elif intent == "data_overview":
        summary = _build_data_overview_summary()
        follow_ups = [
            "Show total_uploaded by client.",
            "Show monthly upload trend.",
            "What is the data quality score?",
        ]
    elif intent == "clarification":
        summary = _build_clarification_text(question)
        follow_ups = [
            "How many videos were uploaded?",
            "Show total_published by channel.",
            "What is the publish rate this month?",
        ]
    else:
        summary = "This question type is not yet supported."
        follow_ups = []

    return AgentQueryResponse(
        question=question,
        interpreted_question=question,
        plan=plan,
        resolved_filters=resolved_filters,
        planner_source=planner_source,
        planner_model=planner_model,
        planner_confidence=planner_confidence,
        planner_fallback_reason=planner_fallback_reason,
        sql=None,
        sql_params={},
        columns=[],
        rows=[],
        chart_spec=None,
        summary=summary,
        caveats=[],
        follow_ups=follow_ups,
        execution_time_ms=0.0,
        row_count=0,
    )


def _build_clarification_response(
    *,
    question: str,
    plan: AgentPlan,
    resolved_filters: dict[str, Any],
    planner_source: str,
    planner_model: str | None = None,
    planner_confidence: float | None = None,
    planner_fallback_reason: str | None = None,
) -> AgentQueryResponse:
    """Return a helpful clarification when the agent can't confidently answer."""
    return AgentQueryResponse(
        question=question,
        interpreted_question=question,
        plan=plan.model_copy(update={"intent": "clarification"}),
        resolved_filters=resolved_filters,
        planner_source=planner_source,
        planner_model=planner_model,
        planner_confidence=planner_confidence,
        planner_fallback_reason=planner_fallback_reason,
        sql=None,
        sql_params={},
        columns=[],
        rows=[],
        chart_spec=None,
        summary=_build_clarification_text(question),
        caveats=[],
        follow_ups=[
            "How many videos were uploaded?",
            "Show total_published by channel.",
            "What is the publish rate this month?",
            "Top 5 users by total_uploaded.",
            "Show monthly upload trend.",
        ],
        execution_time_ms=0.0,
        row_count=0,
    )


def _build_explainer_response(
    *,
    question: str,
    plan: AgentPlan,
    resolved_filters: dict[str, Any],
    caveats: list[str],
    follow_ups: list[str],
    planner_source: str = "supplied_plan",
    planner_model: str | None = None,
    planner_confidence: float | None = None,
    planner_fallback_reason: str | None = None,
) -> AgentQueryResponse:
    metric_name = plan.metrics[0]
    metric = METRIC_REGISTRY[metric_name]
    summary = f"**{metric.label}**"
    if metric.numerator and metric.denominator:
        summary += f"\n\nFormula: {metric.numerator} divided by {metric.denominator}."
    elif metric.numerator:
        summary += f"\n\nDefinition: {metric.numerator}."
    if metric.caveats:
        summary += f"\n\nNote: {metric.caveats}"
    valid_dims = sorted(metric.valid_dimensions) if metric.valid_dimensions else list(DIMENSION_REGISTRY.keys())
    summary += f"\n\nCan be sliced by: {', '.join(valid_dims)}."
    summary += f"\n\nDisplay unit: {metric.display_unit}."
    return AgentQueryResponse(
        question=question,
        interpreted_question=question,
        plan=plan,
        resolved_filters=resolved_filters,
        planner_source=planner_source,
        planner_model=planner_model,
        planner_confidence=planner_confidence,
        planner_fallback_reason=planner_fallback_reason,
        sql=None,
        sql_params={},
        columns=[],
        rows=[],
        chart_spec=None,
        summary=summary,
        caveats=caveats,
        follow_ups=follow_ups or [f"Show {metric_name} by client.", f"Show {metric_name} trend over time."],
        execution_time_ms=0.0,
        row_count=0,
    )


# ── Static content builders ───────────────────────────────────────────────────

def _build_schema_summary() -> str:
    metrics_list = "\n".join(
        f"- **{m.label}** (`{m.name}`): {m.display_unit}"
        + (f" — {m.caveats[:80]}..." if m.caveats and len(m.caveats) > 80 else (f" — {m.caveats}" if m.caveats else ""))
        for m in METRIC_REGISTRY.values()
    )
    dims_list = "\n".join(
        f"- **{d.label}** (`{d.name}`): from `{d.db_table}`"
        for d in DIMENSION_REGISTRY.values()
    )
    return (
        "## Data Schema\n\n"
        "The analytics system is built on a star schema with one central fact table:\n\n"
        "- **fact_video**: One row per uploaded video/job event. "
        "Contains timestamps (uploaded_at, processed_at, published_at), "
        "duration fields, and FK references to dimension tables.\n"
        "- **fact_video_output_type**: Bridge table — one row per video × output type.\n\n"
        "### Available Metrics\n" + metrics_list + "\n\n"
        "### Available Dimensions\n" + dims_list + "\n\n"
        "### Supported Date Ranges\n"
        "last_7d, last_30d, last_90d, this_month, last_month, ytd, all\n\n"
        "### Time Granularities\n"
        "day, week, month, quarter, year, all"
    )


def _build_capabilities_summary() -> str:
    metric_names = ", ".join(f"`{m}`" for m in METRIC_REGISTRY.keys())
    dim_names = ", ".join(f"`{d}`" for d in DIMENSION_REGISTRY.keys())
    return (
        "## What I Can Do\n\n"
        "I'm an analytics agent that answers data questions about video processing operations. "
        "Ask me questions in plain English and I'll query the database for you.\n\n"
        "**Examples:**\n"
        '- "How many videos were uploaded last month?"\n'
        '- "Show publish rate by client"\n'
        '- "Top 5 users by total_uploaded this month"\n'
        '- "Monthly upload trend"\n'
        '- "What is the data quality score?"\n'
        '- "Compare uploads this month vs last month"\n\n'
        f"**Available metrics:** {metric_names}\n\n"
        f"**Available dimensions:** {dim_names}\n\n"
        "**Date ranges:** last_7d, last_30d, last_90d, this_month, last_month, ytd, all\n\n"
        "**I can produce:** KPI cards, bar charts, line charts, tables, and comparisons."
    )


def _build_data_overview_summary() -> str:
    metric_names = [m.label for m in METRIC_REGISTRY.values()]
    return (
        "## Data Overview\n\n"
        "The system tracks video processing operations across multiple dimensions. "
        "To see actual numbers, try asking specific questions like:\n\n"
        '- "How many videos were uploaded?" (total count)\n'
        '- "What is the publish rate?" (published / uploaded)\n'
        '- "Show uploads by client" (breakdown by client)\n'
        '- "Monthly upload trend" (time series)\n\n'
        f"**{len(metric_names)} metrics available:** {', '.join(metric_names[:8])}, and more.\n\n"
        f"**{len(DIMENSION_REGISTRY)} dimensions:** you can slice data by client, channel, user, team, language, "
        "input type, output type, platform, and status flags."
    )


def _build_clarification_text(question: str) -> str:
    return (
        f"I wasn't able to determine exactly what data you're looking for from: \"{question}\"\n\n"
        "Could you try rephrasing? Here are some examples of questions I can answer:\n"
        '- "How many videos were uploaded last month?"\n'
        '- "Show publish rate by client"\n'
        '- "Top 5 channels by total_published"\n'
        '- "What is the data quality score?"\n'
        '- "Explain the data schema"\n\n'
        "You can ask about any metric (uploads, published, processing rate, etc.) "
        "and break it down by client, channel, user, team, language, or other dimensions."
    )


# ── Metadata builder ──────────────────────────────────────────────────────────

def _build_agent_metadata(
    resolved_filters: dict[str, Any],
    plan: AgentPlan,
    grain: str,
    caveats: list[str],
    *,
    planner_source: str | None = None,
    planner_model: str | None = None,
    planner_confidence: float | None = None,
    planner_fallback_reason: str | None = None,
    cache_hit: bool | None = None,
    audit_id: str | None = None,
) -> ResponseMetadata:
    return ResponseMetadata(
        filters_applied=resolved_filters,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metric_definitions_used=plan.metrics,
        source_grain=grain,
        caveats=caveats,
        unit=None,
        currency=None,
        planner_source=planner_source,
        planner_model=planner_model,
        planner_confidence=planner_confidence,
        planner_fallback_reason=planner_fallback_reason,
        cache_hit=cache_hit,
        audit_id=audit_id,
    )
