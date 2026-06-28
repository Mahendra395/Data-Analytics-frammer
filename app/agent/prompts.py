"""Prompt builders for the semantic analytics planner."""
from __future__ import annotations

import json
from typing import Any

from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY

# Metrics that exist in the registry but cannot be compiled by the generic SQL
# compiler yet.  The LLM must NEVER include these in a plan.
_UNSUPPORTED_METRICS = frozenset({"mom_growth_pct", "health_score", "productivity_index"})

# Maximum dimensions the compiler can handle in one query.
_MAX_DIMENSIONS = 2


def _metric_inventory() -> list[dict[str, Any]]:
    """Build the metric catalogue the LLM sees, excluding unsupported ones."""
    items: list[dict[str, Any]] = []
    for metric in METRIC_REGISTRY.values():
        if metric.name in _UNSUPPORTED_METRICS:
            continue
        items.append({
            "name": metric.name,
            "label": metric.label,
            "valid_dimensions": sorted(metric.valid_dimensions) if metric.valid_dimensions else "all",
            "valid_time_grains": sorted(metric.valid_time_grains),
            "display_unit": metric.display_unit,
            "default_time_column": metric.default_time_column,
            "requires_bridge": metric.requires_bridge,
        })
    return items


def _dimension_inventory() -> list[dict[str, Any]]:
    return [
        {
            "name": dim.name,
            "label": dim.label,
            "filter_param": dim.filter_param,
            "supports_bridge": dim.supports_bridge,
            "is_direct": dim.is_direct,
            "is_flag": dim.is_flag,
        }
        for dim in DIMENSION_REGISTRY.values()
    ]


def build_planner_prompt(
    *,
    base_filters: dict[str, Any],
    allowed_client_slugs: tuple[str, ...],
) -> str:
    metric_inventory = _metric_inventory()
    dimension_inventory = _dimension_inventory()

    return (
        "You are the planning layer for a governed analytics agent.\n"
        "Your job is to convert a user's natural-language question into a structured execution plan.\n"
        "Return ONLY a valid JSON plan. Never write SQL.\n\n"

        "## CRITICAL RULES\n"
        "1. Use ONLY metric and dimension names from the inventories below.\n"
        "2. Maximum " + str(_MAX_DIMENSIONS) + " dimensions per query. If the question implies more, pick the 2 most relevant.\n"
        "3. Each metric has a `valid_dimensions` list. If it says `[]` (empty), that metric CANNOT be sliced by ANY dimension — use `dimensions: []`.\n"
        "4. Each metric has `valid_time_grains`. Only use a time_grain that appears in that list.\n"
        "5. When combining metrics, ALL selected metrics MUST share the same `default_time_column`. If they don't, split into separate queries by picking only metrics with the same time column.\n"
        "6. Preserve the base filters unless the user explicitly overrides them.\n"
        "7. If the user asks for a comparison, set compare_mode only when a date_range filter exists.\n"
        "8. For ambiguous requests, prefer a compact plan with one clear metric.\n\n"

        "## NON-DATA QUESTIONS\n"
        "Not every question requires a SQL query. Classify the intent correctly:\n"
        '- "What metrics are available?" / "What can you do?" / "Help" → intent: "capabilities"\n'
        '- "Explain the data schema" / "What tables exist?" / "Describe the data model" → intent: "schema_info"\n'
        '- "Explain the complete data" / "Give me an overview" / "Summarize everything" → intent: "data_overview", execution_strategy: "service_call", service_name: "kpis"\n'
        '- If the question is genuinely ambiguous and you cannot determine what metric/dimension the user wants → intent: "clarification" with metrics: []\n'
        "For schema_info and capabilities intents, set metrics: [], dimensions: [], time_grain: \"all\".\n\n"

        "## EXECUTION STRATEGIES\n"
        "Every plan MUST include an `execution_strategy` field. Choose one:\n\n"
        "1. `sql_query` (default): Single SQL query. Use for specific metric + dimension questions.\n"
        "   Example: \"Show total_uploaded by client\" → sql_query\n\n"
        "2. `service_call`: Route to a pre-computed analytics service. Use for broad business questions.\n"
        "   When using service_call, set `service_name` to one of the available services.\n"
        "   Available services:\n"
        "   - `kpis`: Business KPIs overview (uploads, published, rates, growth)\n"
        "   - `growth`: MoM growth comparison (current vs previous month)\n"
        "   - `quality_summary`: Data quality score and per-field analysis\n"
        "   - `funnel`: Upload → Process → Publish conversion funnel\n"
        "   - `monthly_trend`: Monthly upload and publish trend line chart\n"
        "   - `insights`: AI-powered risk/opportunity/driver analysis with executive summary\n"
        "   - `scores`: Health scores and grades across channels, users, and languages\n"
        "   - `anomalies`: Automatic anomaly detection for MoM spikes/drops\n\n"
        "3. `multi_query`: 2-4 parallel SQL queries when a single query can't answer. Provide `sub_plans` array.\n"
        "   Each sub_plan is a full plan object (same schema, but without sub_plans).\n\n"

        "## STRATEGY SELECTION RULES\n"
        "- \"Give me KPIs\" / \"business performance\" / \"overview\" / \"data overview\" → service_call, service_name: \"kpis\"\n"
        "- \"How's data quality?\" / \"quality score\" / \"DQ summary\" → service_call, service_name: \"quality_summary\"\n"
        "- \"Growth\" / \"MoM change\" / \"month over month\" → service_call, service_name: \"growth\"\n"
        "- \"Show the funnel\" / \"conversion stages\" / \"publish gap\" → service_call, service_name: \"funnel\"\n"
        "- \"Monthly trend\" / \"trend over months\" → service_call, service_name: \"monthly_trend\"\n"
        "- \"What are the risks?\" / \"opportunities\" / \"key insights\" → service_call, service_name: \"insights\"\n"
        "- \"Health scores\" / \"scorecards\" / \"grades\" → service_call, service_name: \"scores\"\n"
        "- \"Anomalies\" / \"outliers\" / \"unusual changes\" / \"spikes\" → service_call, service_name: \"anomalies\"\n"
        "- \"Show total_uploaded by client\" → sql_query (specific metric + dimension)\n"
        "- \"Compare uploads by client AND publish rate by channel\" → multi_query with 2 sub_plans\n\n"

        "## DATE RANGES\n"
        "Allowed date_range slugs: last_7d, last_30d, last_90d, this_month, last_month, ytd, all.\n"
        "Default to `all` when no date context is specified.\n\n"

        "## EXAMPLES\n\n"

        "User: \"How many videos were uploaded last month?\"\n"
        "Plan: {\"interpreted_question\": \"Total videos uploaded in the last month\", \"intent\": \"single_kpi\", "
        "\"metrics\": [\"total_uploaded\"], \"dimensions\": [], \"filters\": {\"date_range\": \"last_month\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": {\"type\": \"stat\", \"x\": null, \"y\": \"total_uploaded\", \"series\": [], \"title\": null}, "
        "\"explanation_level\": \"normal\", \"execution_strategy\": \"sql_query\", \"service_name\": null, \"sub_plans\": null}\n\n"

        "User: \"Show me uploads by client for the last 30 days\"\n"
        "Plan: {\"interpreted_question\": \"Upload count broken down by client in the last 30 days\", \"intent\": \"breakdown\", "
        "\"metrics\": [\"total_uploaded\"], \"dimensions\": [\"client\"], \"filters\": {\"date_range\": \"last_30d\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [{\"field\": \"total_uploaded\", \"direction\": \"desc\"}], \"limit\": 50, "
        "\"chart\": {\"type\": \"bar\", \"x\": \"client\", \"y\": \"total_uploaded\", \"series\": [], \"title\": null}, "
        "\"explanation_level\": \"normal\", \"execution_strategy\": \"sql_query\", \"service_name\": null, \"sub_plans\": null}\n\n"

        "User: \"What is the data quality score?\"\n"
        "Plan: {\"interpreted_question\": \"Current data quality score (global, no dimensions because dq_score cannot be sliced)\", \"intent\": \"single_kpi\", "
        "\"metrics\": [\"dq_score\"], \"dimensions\": [], \"filters\": {\"date_range\": \"all\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": {\"type\": \"stat\", \"x\": null, \"y\": \"dq_score\", \"series\": [], \"title\": null}, "
        "\"explanation_level\": \"normal\", \"execution_strategy\": \"sql_query\", \"service_name\": null, \"sub_plans\": null}\n\n"

        "User: \"Explain the data schema\"\n"
        "Plan: {\"interpreted_question\": \"User wants to understand the data model and available tables/metrics\", \"intent\": \"schema_info\", "
        "\"metrics\": [], \"dimensions\": [], \"filters\": {\"date_range\": null}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": null, \"explanation_level\": \"normal\", "
        "\"execution_strategy\": \"sql_query\", \"service_name\": null, \"sub_plans\": null}\n\n"

        "User: \"Explain the complete data in detail\"\n"
        "Plan: {\"interpreted_question\": \"User wants a comprehensive overview of all available data\", \"intent\": \"data_overview\", "
        "\"metrics\": [\"total_uploaded\", \"total_published\", \"total_processed\", \"publish_rate\"], \"dimensions\": [], \"filters\": {\"date_range\": \"all\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": null, \"explanation_level\": \"detailed\", "
        "\"execution_strategy\": \"service_call\", \"service_name\": \"kpis\", \"sub_plans\": null}\n\n"

        "User: \"Give me the business KPIs\"\n"
        "Plan: {\"interpreted_question\": \"High-level business KPIs overview\", \"intent\": \"data_overview\", "
        "\"metrics\": [\"total_uploaded\", \"total_published\", \"publish_rate\"], \"dimensions\": [], \"filters\": {\"date_range\": \"all\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": null, \"explanation_level\": \"normal\", "
        "\"execution_strategy\": \"service_call\", \"service_name\": \"kpis\", \"sub_plans\": null}\n\n"

        "User: \"How is the data quality?\"\n"
        "Plan: {\"interpreted_question\": \"Data quality score and field-level analysis\", \"intent\": \"data_overview\", "
        "\"metrics\": [\"dq_score\"], \"dimensions\": [], \"filters\": {\"date_range\": \"all\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [], \"limit\": 50, "
        "\"chart\": null, \"explanation_level\": \"normal\", "
        "\"execution_strategy\": \"service_call\", \"service_name\": \"quality_summary\", \"sub_plans\": null}\n\n"

        "User: \"Top 5 users by publish rate this month\"\n"
        "Plan: {\"interpreted_question\": \"Top 5 users ranked by publish rate for this month\", \"intent\": \"top_n\", "
        "\"metrics\": [\"publish_rate\", \"total_published\"], \"dimensions\": [\"user\"], \"filters\": {\"date_range\": \"this_month\"}, "
        "\"time_grain\": \"all\", \"compare_mode\": null, \"order_by\": [{\"field\": \"publish_rate\", \"direction\": \"desc\"}], \"limit\": 5, "
        "\"chart\": {\"type\": \"bar\", \"x\": \"user\", \"y\": \"publish_rate\", \"series\": [], \"title\": null}, "
        "\"explanation_level\": \"normal\", \"execution_strategy\": \"sql_query\", \"service_name\": null, \"sub_plans\": null}\n\n"

        "## METRIC INVENTORY (use ONLY these names)\n"
        f"{json.dumps(metric_inventory, ensure_ascii=True)}\n\n"
        "## DIMENSION INVENTORY\n"
        f"{json.dumps(dimension_inventory, ensure_ascii=True)}\n\n"
        "## BASE FILTERS (preserve unless user overrides)\n"
        f"{json.dumps(base_filters, ensure_ascii=True)}\n\n"
        "## ALLOWED CLIENT SCOPE\n"
        f"{json.dumps(list(allowed_client_slugs), ensure_ascii=True)}"
    )


def build_repair_prompt(
    *,
    original_question: str,
    failed_plan: dict[str, Any],
    validation_errors: list[dict[str, Any]],
    base_filters: dict[str, Any],
    allowed_client_slugs: tuple[str, ...],
) -> str:
    """Build a follow-up prompt that feeds validation errors back to the LLM."""
    metric_inventory = _metric_inventory()
    dimension_inventory = _dimension_inventory()

    return (
        "You previously generated an analytics plan that failed validation.\n"
        "Fix the plan to resolve ALL the issues listed below.\n"
        "Return ONLY the corrected JSON plan.\n\n"

        "## ORIGINAL QUESTION\n"
        f"{original_question}\n\n"

        "## YOUR PREVIOUS PLAN (INVALID)\n"
        f"{json.dumps(failed_plan, ensure_ascii=True)}\n\n"

        "## VALIDATION ERRORS TO FIX\n"
        f"{json.dumps(validation_errors, ensure_ascii=True)}\n\n"

        "## HOW TO FIX COMMON ERRORS\n"
        "- too_many_dimensions: Use at most " + str(_MAX_DIMENSIONS) + " dimensions. Remove the least relevant ones.\n"
        "- invalid_dimension_for_metric: That metric cannot be sliced by that dimension. Remove the dimension or remove the metric.\n"
        "- metric_not_compilable: That metric is not supported. Remove it from the plan.\n"
        "- mixed_time_anchors: The selected metrics use different time columns. Keep only metrics with the same default_time_column.\n"
        "- unknown_metric: Use only metrics from the inventory below.\n"
        "- unknown_dimension: Use only dimensions from the inventory below.\n\n"

        "## METRIC INVENTORY\n"
        f"{json.dumps(metric_inventory, ensure_ascii=True)}\n\n"
        "## DIMENSION INVENTORY\n"
        f"{json.dumps(dimension_inventory, ensure_ascii=True)}\n\n"
        "## BASE FILTERS\n"
        f"{json.dumps(base_filters, ensure_ascii=True)}\n\n"
        "## ALLOWED CLIENT SCOPE\n"
        f"{json.dumps(list(allowed_client_slugs), ensure_ascii=True)}"
    )


def build_summarizer_prompt(
    *,
    question: str,
    interpreted_question: str,
    plan_summary: dict[str, Any],
    columns: list[str],
    rows: list[list[Any]],
    caveats: list[str],
) -> str:
    """Build a prompt for LLM-powered summarization of query results."""
    # Limit rows sent to the LLM to avoid token overflow
    display_rows = rows[:30]
    return (
        "You are a data analyst summarizing query results for a business user.\n"
        "Write a clear, concise, insightful natural-language summary.\n\n"
        "Guidelines:\n"
        "- Start with the key finding or headline number.\n"
        "- Mention notable patterns, outliers, or comparisons if visible.\n"
        "- Use actual numbers from the data. Do not invent data.\n"
        "- Keep it to 2-4 sentences for simple results, up to a short paragraph for complex ones.\n"
        "- If no rows were returned, say so plainly and suggest possible reasons.\n"
        "- Do NOT say 'for the resolved scope' or use internal jargon.\n\n"

        f"## USER QUESTION\n{question}\n\n"
        f"## INTERPRETED AS\n{interpreted_question}\n\n"
        f"## QUERY PLAN\nIntent: {plan_summary.get('intent')}, "
        f"Metrics: {plan_summary.get('metrics')}, "
        f"Dimensions: {plan_summary.get('dimensions')}, "
        f"Time grain: {plan_summary.get('time_grain')}, "
        f"Date range: {plan_summary.get('filters', {}).get('date_range', 'all')}\n\n"
        f"## RESULT COLUMNS\n{json.dumps(columns)}\n\n"
        f"## RESULT DATA ({len(rows)} total rows, showing first {len(display_rows)})\n"
        f"{json.dumps(display_rows, default=str)}\n\n"
        f"## CAVEATS\n{json.dumps(caveats)}\n\n"
        "Write the summary now:"
    )


def build_blocks_summarizer_prompt(
    *,
    question: str,
    blocks_summary: list[dict[str, Any]],
    explanation_level: str = "normal",
) -> str:
    """Build a prompt for analysing multiple ResponseBlocks into a rich narrative."""
    depth_guide = {
        "short": (
            "Write a concise 2-3 sentence executive summary highlighting only the most "
            "critical finding and one key metric."
        ),
        "normal": (
            "Write a thorough analytical summary in **markdown** format. Include:\n"
            "- A headline finding\n"
            "- Key metric call-outs with specific numbers\n"
            "- Notable patterns, comparisons, or concerns\n"
            "- 1-2 actionable observations\n"
            "Aim for 4-8 sentences across 2-3 short paragraphs."
        ),
        "detailed": (
            "Write a comprehensive, detailed business analysis in **markdown** format. "
            "Structure it as:\n\n"
            "### Key Highlights\n"
            "A bold headline finding followed by 2-3 supporting metrics with exact numbers.\n\n"
            "### Detailed Analysis\n"
            "Analyse each data panel in depth. Reference every KPI, explain what the numbers "
            "mean in business context, flag anomalies (e.g. very low publish rate, concentration "
            "risk), compare rates against typical benchmarks when possible, and discuss "
            "operational implications.\n\n"
            "### Observations & Recommendations\n"
            "Provide 2-4 specific, data-backed observations or recommendations.\n\n"
            "Use **bold** for important numbers and metric names. "
            "Be specific — always cite the exact values from the data. "
            "Write 10-20 sentences total across the sections."
        ),
    }

    return (
        "You are a senior data analyst writing a business intelligence report for a stakeholder.\n"
        "You have access to the complete data results below. Analyse them thoroughly.\n\n"
        "## GUIDELINES\n"
        f"{depth_guide.get(explanation_level, depth_guide['normal'])}\n\n"
        "Additional rules:\n"
        "- ALWAYS reference specific numbers from the data — never say 'high' or 'low' without the value.\n"
        "- Interpret what the numbers mean, don't just repeat them.\n"
        "- If rates are unusually low or high, call that out explicitly.\n"
        "- If there are charts or breakdowns, highlight the top/bottom performers.\n"
        "- Use markdown formatting (headers, bold, bullet points) for readability.\n"
        "- Do NOT use internal database jargon or column names — use business-friendly language.\n\n"
        f"## USER QUESTION\n{question}\n\n"
        f"## COMPLETE DATA\n{json.dumps(blocks_summary, default=str)}\n\n"
        "Write your analysis now:"
    )
