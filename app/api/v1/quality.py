"""GET /api/v1/quality — comprehensive data quality & governance endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_where_clause
from app.schemas.responses import (
    ApiResponse,
    DQFieldReport,
    DQIssueRow,
    DQRuleResult,
    DQRulesResponse,
    QualityColumnReport,
    QualitySummary,
    QualityTrendPoint,
    QualityTrendResponse,
)
from app.utils.response import build_metadata

router = APIRouter(prefix="/quality", tags=["Data Quality"])

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _status(pct: float) -> str:
    if pct > 20:
        return "critical"
    if pct > 5:
        return "warning"
    return "good"


def _severity(pct: float) -> str:
    if pct > 20:
        return "critical"
    if pct > 5:
        return "warning"
    if pct > 0:
        return "info"
    return "ok"


async def _total(db: AsyncSession, where: list[str] | None = None, params: dict | None = None) -> int:
    fv_where = ("WHERE " + " AND ".join(where)) if where else ""
    return int((await db.execute(text(f"SELECT COUNT(*) FROM fact_video fv {fv_where}"), params or {})).scalar_one() or 0)


async def _published_total(db: AsyncSession, where: list[str] | None = None, params: dict | None = None) -> int:
    pub_clauses = (where or []) + ["fv.published = TRUE"]
    fv_where = "WHERE " + " AND ".join(pub_clauses)
    return int(
        (await db.execute(text(f"SELECT COUNT(*) FROM fact_video fv {fv_where}"), params or {})).scalar_one() or 0
    )


# ── /quality/summary ───────────────────────────────────────────────────────────

_CHECKED_COLUMNS = [
    ("video_id",           "fact_video"),
    ("headline",           "fact_video"),
    ("source_url",         "fact_video"),
    ("channel_id",         "fact_video"),
    ("user_id",            "fact_video"),
    ("language_id",        "fact_video"),
    ("input_type_id",      "fact_video"),
    ("uploaded_at",        "fact_video"),
    ("published_platform", "fact_video"),
    ("published_url",      "fact_video"),
    ("uploaded_duration_sec",  "fact_video"),
    ("created_duration_sec",   "fact_video"),
]


@router.get("/summary", response_model=ApiResponse[QualitySummary])
async def quality_summary(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[QualitySummary]:
    where, params = build_where_clause(f)
    fv_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = await _total(db, where, params)

    dup_result = await db.execute(text(f"""
        SELECT COALESCE(SUM(cnt - 1), 0)
        FROM (
            SELECT video_id, COUNT(*) AS cnt
            FROM fact_video fv
            {fv_where}
            {'AND' if fv_where else 'WHERE'} video_id IS NOT NULL
            GROUP BY video_id
            HAVING COUNT(*) > 1
        ) dups
    """), params)
    duplicate_video_ids = int(dup_result.scalar_one() or 0)

    unk_result = await db.execute(text("""
        SELECT COUNT(*) FROM dim_user
        WHERE LOWER(COALESCE(team_name,'')) IN ('unknown','')
    """))
    unknown_team_names = int(unk_result.scalar_one() or 0)

    column_reports: list[QualityColumnReport] = []
    score_total = 0.0

    for col, table in _CHECKED_COLUMNS:
        if table == "fact_video":
            res = await db.execute(text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {col} IS NULL OR {col}::text = '') AS null_count,
                    COUNT(DISTINCT {col})                                      AS distinct_count
                FROM fact_video fv
                {fv_where}
            """), params)
        else:
            res = await db.execute(text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {col} IS NULL OR {col}::text = '') AS null_count,
                    COUNT(DISTINCT {col})                                      AS distinct_count
                FROM {table}
            """))
        r = res.one()
        null_count     = int(r.null_count or 0)
        distinct_count = int(r.distinct_count or 0)
        null_pct       = round(null_count * 100.0 / total, 2) if total else 0.0
        has_issues     = null_pct > 5.0

        issue = None
        if null_pct > 50:
            issue = f"{null_pct:.1f}% missing — critical"
        elif null_pct > 10:
            issue = f"{null_pct:.1f}% missing — warning"
        elif null_pct > 5:
            issue = f"{null_pct:.1f}% missing — low"

        status = _status(null_pct)
        score_total += max(0, 100 - null_pct)

        column_reports.append(
            QualityColumnReport(
                column=col,
                total_rows=total,
                null_count=null_count,
                null_pct=null_pct,
                distinct_count=distinct_count,
                has_issues=has_issues,
                issue_description=issue,
                status=status,
            )
        )

    overall_score = round(score_total / len(_CHECKED_COLUMNS), 1) if _CHECKED_COLUMNS else 100.0

    data = QualitySummary(
        overall_score=overall_score,
        total_rows=total,
        columns=column_reports,
        duplicate_video_ids=duplicate_video_ids,
        unknown_team_names=unknown_team_names,
    )
    return ApiResponse(data=data, meta=build_metadata(
        f, grain="rule-evaluated",
        caveats=["Quality score = average of (100 - null_pct) across all checked columns"],
        unit="percent",
    ))


# ── /quality/fields ────────────────────────────────────────────────────────────

_FIELDS_CONFIG: list[dict] = [
    {"field": "video_id",            "table": "fact_video",  "unknown_expr": None},
    {"field": "headline",            "table": "fact_video",  "unknown_expr": None},
    {"field": "source_url",          "table": "fact_video",  "unknown_expr": None},
    {"field": "published_url",       "table": "fact_video",  "unknown_expr": "published_url NOT LIKE 'http%' AND published_url IS NOT NULL"},
    {"field": "published_platform",  "table": "fact_video",  "unknown_expr": None},
    {"field": "channel_id",          "table": "fact_video",  "unknown_expr": None},
    {"field": "user_id",             "table": "fact_video",  "unknown_expr": None},
    {"field": "language_id",         "table": "fact_video",  "unknown_expr": None},
    {"field": "input_type_id",       "table": "fact_video",  "unknown_expr": None},
    {"field": "uploaded_at",         "table": "fact_video",  "unknown_expr": None},
    {"field": "processed_at",        "table": "fact_video",  "unknown_expr": None},
    {"field": "published_at",        "table": "fact_video",  "unknown_expr": None},
    {"field": "uploaded_duration_sec",  "table": "fact_video","unknown_expr": "uploaded_duration_sec < 0"},
    {"field": "created_duration_sec",   "table": "fact_video","unknown_expr": "created_duration_sec < 0"},
    {"field": "published_duration_sec", "table": "fact_video","unknown_expr": "published_duration_sec < 0"},
    {"field": "team_name",           "table": "dim_user",    "unknown_expr": "LOWER(COALESCE(team_name,'')) IN ('unknown','')"},
]


@router.get("/fields", response_model=ApiResponse[List[DQFieldReport]])
async def quality_fields(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[DQFieldReport]]:
    """Per-field null rates and unknown / invalid value rates."""
    where, params = build_where_clause(f)
    fv_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = await _total(db, where, params)
    dim_user_total = int(
        (await db.execute(text("SELECT COUNT(*) FROM dim_user"))).scalar_one() or 0
    )

    results: List[DQFieldReport] = []

    for cfg in _FIELDS_CONFIG:
        field   = cfg["field"]
        table   = cfg["table"]
        unk_exp = cfg["unknown_expr"]
        row_count = total if table == "fact_video" else dim_user_total

        if table == "fact_video":
            null_sql = text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {field} IS NULL OR {field}::text = '') AS null_count,
                    COUNT(DISTINCT {field})                                        AS distinct_count
                FROM fact_video fv
                {fv_where}
            """)
            r = (await db.execute(null_sql, params)).one()
        else:
            null_sql = text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {field} IS NULL OR {field}::text = '') AS null_count,
                    COUNT(DISTINCT {field})                                        AS distinct_count
                FROM {table}
            """)
            r = (await db.execute(null_sql)).one()
        null_count     = int(r.null_count or 0)
        distinct_count = int(r.distinct_count or 0)
        null_pct       = round(null_count * 100.0 / row_count, 2) if row_count else 0.0

        unknown_count = 0
        unknown_pct   = 0.0
        if unk_exp:
            unk_sql = text(f"SELECT COUNT(*) FROM {table} WHERE {unk_exp}")
            unknown_count = int((await db.execute(unk_sql)).scalar_one() or 0)
            unknown_pct   = round(unknown_count * 100.0 / row_count, 2) if row_count else 0.0

        results.append(
            DQFieldReport(
                field=field,
                table=table,
                total_rows=row_count,
                null_count=null_count,
                null_pct=null_pct,
                unknown_count=unknown_count,
                unknown_pct=unknown_pct,
                distinct_count=distinct_count,
                status=_status(null_pct + unknown_pct),
            )
        )

    return ApiResponse(data=results, meta=build_metadata(
        f, grain="rule-evaluated",
        unit="percent",
    ))


# ── /quality/trend ─────────────────────────────────────────────────────────────

@router.get("/trend", response_model=ApiResponse[QualityTrendResponse])
async def quality_trend(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[QualityTrendResponse]:
    """Month-over-month quality score trend + per-client null metrics."""
    where, params = build_where_clause(f)
    fv_where = ("WHERE " + " AND ".join(where)) if where else ""
    pub_clauses = where + ["fv.published = TRUE"]
    pub_where = "WHERE " + " AND ".join(pub_clauses)
    total     = await _total(db, where, params)
    pub_total = await _published_total(db, where, params)

    inv_url = int((await db.execute(text(f"""
        SELECT COUNT(*) FROM fact_video fv
        {pub_where}
          AND (fv.published_url IS NULL OR fv.published_url = '' OR fv.published_url NOT LIKE 'http%')
    """), params)).scalar_one() or 0)

    null_plat = int((await db.execute(text(f"""
        SELECT COUNT(*) FROM fact_video fv
        {pub_where}
          AND (fv.published_platform IS NULL OR fv.published_platform = '')
    """), params)).scalar_one() or 0)
    null_platform_pct = round(null_plat / pub_total * 100, 2) if pub_total else 0.0

    unk_lang = int((await db.execute(text(f"""
        SELECT COUNT(*) FROM fact_video fv
        LEFT JOIN dim_language dl ON dl.id = fv.language_id
        {fv_where}
        {'AND' if fv_where else 'WHERE'} (fv.language_id IS NULL OR LOWER(dl.iso_code) IN ('unknown','unk',''))
    """), params)).scalar_one() or 0)
    unk_lang_pct = round(unk_lang / total * 100, 2) if total else 0.0

    unk_input = int((await db.execute(text(f"""
        SELECT COUNT(*) FROM fact_video fv
        LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
        {fv_where}
        {'AND' if fv_where else 'WHERE'} (fv.input_type_id IS NULL OR LOWER(COALESCE(dit.name,'')) IN ('unknown',''))
    """), params)).scalar_one() or 0)
    unk_input_pct = round(unk_input / total * 100, 2) if total else 0.0

    unk_output = int((await db.execute(text(f"""
        SELECT COUNT(*) FROM fact_video fv
        {fv_where}
        {'AND' if fv_where else 'WHERE'} NOT EXISTS (
            SELECT 1 FROM fact_video_output_type fvot WHERE fvot.video_id = fv.id
        )
    """), params)).scalar_one() or 0)
    unk_output_pct = round(unk_output / total * 100, 2) if total else 0.0

    trend_and = (" AND " + " AND ".join(where)) if where else ""
    trend_sql = text(f"""
        SELECT
            EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int AS yr,
            EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo,
            COUNT(*)                                            AS total,
            SUM(CASE WHEN channel_id IS NULL  THEN 1 ELSE 0 END) AS null_ch,
            SUM(CASE WHEN user_id IS NULL     THEN 1 ELSE 0 END) AS null_usr,
            SUM(CASE WHEN language_id IS NULL THEN 1 ELSE 0 END) AS null_lang,
            SUM(CASE WHEN input_type_id IS NULL THEN 1 ELSE 0 END) AS null_inp
        FROM fact_video fv
        WHERE uploaded_at IS NOT NULL{trend_and}
        GROUP BY yr, mo
        ORDER BY yr, mo
    """)
    trend_rows = (await db.execute(trend_sql, params)).mappings().all()

    trend: List[QualityTrendPoint] = []
    for r in trend_rows:
        cnt = int(r["total"] or 1)
        yr  = int(r["yr"])
        mo  = int(r["mo"])
        n_ch   = round(int(r["null_ch"]   or 0) / cnt * 100, 1)
        n_usr  = round(int(r["null_usr"]  or 0) / cnt * 100, 1)
        n_lang = round(int(r["null_lang"] or 0) / cnt * 100, 1)
        n_inp  = round(int(r["null_inp"]  or 0) / cnt * 100, 1)
        score  = round(100 - (n_ch + n_usr + n_lang + n_inp) / 4, 1)
        trend.append(
            QualityTrendPoint(
                month_label=f"{_MONTH_LABELS[mo]} {str(yr)[2:]}",
                year=yr,
                month=mo,
                total_rows=cnt,
                null_channel_pct=n_ch,
                null_user_pct=n_usr,
                null_language_pct=n_lang,
                null_input_type_pct=n_inp,
                overall_score=score,
            )
        )

    by_client_sql = text(f"""
        SELECT
            dc.name                                                         AS client_name,
            COUNT(fv.id)                                                    AS total,
            SUM(CASE WHEN fv.channel_id IS NULL   THEN 1 ELSE 0 END)       AS null_ch,
            SUM(CASE WHEN fv.user_id IS NULL       THEN 1 ELSE 0 END)      AS null_usr,
            SUM(CASE WHEN fv.language_id IS NULL   THEN 1 ELSE 0 END)      AS null_lang,
            SUM(CASE WHEN fv.published = TRUE
                     AND (fv.published_platform IS NULL OR fv.published_platform = '')
                     THEN 1 ELSE 0 END)                                     AS null_platform_pub,
            SUM(CASE WHEN fv.published = TRUE
                     AND (fv.published_url IS NULL OR fv.published_url NOT LIKE 'http%')
                     THEN 1 ELSE 0 END)                                     AS invalid_url_pub
        FROM fact_video fv
        JOIN dim_client dc ON dc.id = fv.client_id
        {fv_where}
        GROUP BY dc.name
        ORDER BY total DESC
    """)
    by_client_rows = (await db.execute(by_client_sql, params)).mappings().all()
    by_client: List[Dict] = []
    for r in by_client_rows:
        cnt = int(r["total"] or 1)
        by_client.append({
            "client":            r["client_name"],
            "total":             cnt,
            "null_channel_pct":  round(int(r["null_ch"]   or 0) / cnt * 100, 1),
            "null_user_pct":     round(int(r["null_usr"]  or 0) / cnt * 100, 1),
            "null_language_pct": round(int(r["null_lang"] or 0) / cnt * 100, 1),
            "null_platform_pct": round(int(r["null_platform_pub"] or 0) / cnt * 100, 1),
            "invalid_url_pct":   round(int(r["invalid_url_pub"] or 0) / cnt * 100, 1),
        })

    data = QualityTrendResponse(
        trend=trend,
        by_client=by_client,
        invalid_url_count=inv_url,
        duplicate_job_id_count=0,
        unknown_language_pct=unk_lang_pct,
        unknown_input_type_pct=unk_input_pct,
        unknown_output_type_pct=unk_output_pct,
        null_platform_pct=null_platform_pct,
    )
    return ApiResponse(data=data, meta=build_metadata(
        f, grain="monthly-aggregated",
        caveats=["Trend DQ score uses 4 core fields only (channel, user, language, input_type)"],
        unit="percent",
    ))


# ── /quality/issues ────────────────────────────────────────────────────────────

@router.get("/issues", response_model=ApiResponse[List[DQIssueRow]])
async def quality_issues(
    limit: int = Query(default=200, ge=1, le=1000),
    category: Optional[str] = Query(default=None, description=(
        "Filter: null_metadata | invalid_url | missing_platform | "
        "missing_team | duplicate | timestamp_inconsistency | "
        "negative_duration | missing_bridge"
    )),
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[List[DQIssueRow]]:
    """Returns rows with specific data quality issues."""
    where, params = build_where_clause(f)
    fv_filter_and = (" AND " + " AND ".join(where)) if where else ""
    combined_params = {**params, "lim": limit}
    issues: List[DQIssueRow] = []

    def _row_to_issue(r: Any, cat: str, detail: str, sev: str) -> DQIssueRow:
        return DQIssueRow(
            row_id=str(r["id"]),
            video_id=r.get("video_id"),
            headline=r.get("headline"),
            channel=r.get("channel"),
            user=r.get("user"),
            issue_category=cat,
            issue_detail=detail,
            severity=sev,
            uploaded_at=r.get("uploaded_at"),
        )

    base_join = """
        FROM fact_video fv
        LEFT JOIN dim_channel dc ON dc.id = fv.channel_id
        LEFT JOIN dim_user    du ON du.id = fv.user_id
    """
    select_cols = """
        SELECT fv.id, fv.video_id, fv.headline,
               dc.name AS channel, du.name AS "user",
               fv.uploaded_at
    """

    if category in (None, "null_metadata"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE (fv.channel_id IS NULL OR fv.user_id IS NULL
               OR fv.language_id IS NULL OR fv.input_type_id IS NULL){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "null_metadata",
                "One or more of: channel_id, user_id, language_id, input_type_id is NULL", "warning"))

    if category in (None, "invalid_url"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE fv.published = TRUE
              AND (fv.published_url IS NULL OR fv.published_url = ''
                   OR fv.published_url NOT LIKE 'http%'){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "invalid_url",
                "Published row missing or invalid published_url", "critical"))

    if category in (None, "missing_platform"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE fv.published = TRUE
              AND (fv.published_platform IS NULL OR fv.published_platform = ''){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "missing_platform",
                "Published row missing published_platform", "warning"))

    if category in (None, "missing_team"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE LOWER(COALESCE(du.team_name,'')) IN ('unknown',''){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "missing_team",
                "User has no team_name or team_name is 'unknown'", "info"))

    if category in (None, "duplicate"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE fv.video_id IN (
                SELECT video_id FROM fact_video fv2
                WHERE video_id IS NOT NULL{fv_filter_and.replace('fv.', 'fv2.')}
                GROUP BY video_id HAVING COUNT(*) > 1
            )
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "duplicate",
                "video_id appears more than once in fact_video", "warning"))

    if category in (None, "timestamp_inconsistency"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE fv.processed_at IS NOT NULL AND fv.uploaded_at IS NOT NULL
              AND fv.processed_at < fv.uploaded_at{fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "timestamp_inconsistency",
                "processed_at is earlier than uploaded_at", "critical"))
        rows2 = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE fv.published_at IS NOT NULL
              AND (
                (fv.processed_at IS NOT NULL AND fv.published_at < fv.processed_at)
                OR (fv.uploaded_at IS NOT NULL AND fv.published_at < fv.uploaded_at)
              ){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows2:
            issues.append(_row_to_issue(r, "timestamp_inconsistency",
                "published_at is earlier than processed_at or uploaded_at", "critical"))

    if category in (None, "negative_duration"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE ((fv.uploaded_duration_sec IS NOT NULL AND fv.uploaded_duration_sec < 0)
               OR (fv.created_duration_sec  IS NOT NULL AND fv.created_duration_sec  < 0)
               OR (fv.published_duration_sec IS NOT NULL AND fv.published_duration_sec < 0)){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "negative_duration",
                "One or more duration fields are negative", "critical"))

    if category in (None, "missing_bridge"):
        rows = (await db.execute(text(f"""
            {select_cols} {base_join}
            WHERE NOT EXISTS (
                SELECT 1 FROM fact_video_output_type fvot WHERE fvot.video_id = fv.id
            ){fv_filter_and}
            LIMIT :lim
        """), combined_params)).mappings().all()
        for r in rows:
            issues.append(_row_to_issue(r, "missing_bridge",
                "No output type records in fact_video_output_type for this video", "info"))

    return ApiResponse(data=issues[:limit], meta=build_metadata(
        f, grain="video-level",
        caveats=["Issue rows are sampled up to the limit parameter; "
                 "full counts are available via /quality/rules"],
    ))


# ── /quality/rules ─────────────────────────────────────────────────────────────

@router.get("/rules", response_model=ApiResponse[DQRulesResponse])
async def quality_rules(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[DQRulesResponse]:
    """Evaluates all registered DQ rules and returns counts, percentages, and severity."""
    where, params = build_where_clause(f)
    total = await _total(db, where, params)
    pub   = await _published_total(db, where, params)

    rules: List[DQRuleResult] = []

    async def _rule(
        rule_id: str,
        rule_name: str,
        description: str,
        sql: str,
        category: str,
        denominator: Optional[int] = None,
    ) -> None:
        denom = denominator if denominator is not None else total
        count = int((await db.execute(text(sql))).scalar_one() or 0)
        pct   = round(count * 100.0 / denom, 2) if denom else 0.0
        rules.append(DQRuleResult(
            rule_id=rule_id,
            rule_name=rule_name,
            description=description,
            affected_count=count,
            total_rows=denom,
            affected_pct=pct,
            severity=_severity(pct),
            category=category,
        ))

    await _rule("R01", "Null channel_id",
        "Rows where channel_id is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE channel_id IS NULL", "null")
    await _rule("R02", "Null user_id",
        "Rows where user_id is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE user_id IS NULL", "null")
    await _rule("R03", "Null language_id",
        "Rows where language_id is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE language_id IS NULL", "null")
    await _rule("R04", "Null input_type_id",
        "Rows where input_type_id is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE input_type_id IS NULL", "null")
    await _rule("R05", "Null uploaded_at",
        "Rows where uploaded_at is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE uploaded_at IS NULL", "null")
    await _rule("R06", "Null processed_at",
        "Rows where processed_at is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE processed_at IS NULL", "null")
    await _rule("R07", "Null published_at on published rows",
        "Published rows where published_at is NULL",
        "SELECT COUNT(*) FROM fact_video WHERE published = TRUE AND published_at IS NULL",
        "null", denominator=pub)
    await _rule("R08", "Unknown language",
        "Videos linked to a language coded as unknown",
        """SELECT COUNT(*) FROM fact_video fv
           LEFT JOIN dim_language dl ON dl.id = fv.language_id
           WHERE fv.language_id IS NULL OR LOWER(dl.iso_code) IN ('unknown','unk','')""",
        "null")
    await _rule("R09", "Unknown input type",
        "Videos linked to an input_type named unknown",
        """SELECT COUNT(*) FROM fact_video fv
           LEFT JOIN dim_input_type dit ON dit.id = fv.input_type_id
           WHERE fv.input_type_id IS NULL OR LOWER(COALESCE(dit.name,'')) IN ('unknown','')""",
        "null")
    await _rule("R10", "Duplicate video_id",
        "Rows sharing a non-unique video_id",
        """SELECT COALESCE(SUM(cnt - 1), 0) FROM (
               SELECT video_id, COUNT(*) AS cnt FROM fact_video
               WHERE video_id IS NOT NULL GROUP BY video_id HAVING COUNT(*) > 1
           ) d""",
        "duplicate")
    await _rule("R11", "Invalid published_url",
        "Published rows with missing or non-HTTP published_url",
        """SELECT COUNT(*) FROM fact_video
           WHERE published = TRUE
             AND (published_url IS NULL OR published_url = '' OR published_url NOT LIKE 'http%')""",
        "invalid", denominator=pub)
    await _rule("R12", "Missing published_platform",
        "Published rows with no platform tag",
        """SELECT COUNT(*) FROM fact_video
           WHERE published = TRUE
             AND (published_platform IS NULL OR published_platform = '')""",
        "completeness", denominator=pub)

    user_total = int(
        (await db.execute(text("SELECT COUNT(*) FROM dim_user"))).scalar_one() or 1
    )
    await _rule("R13", "Missing team_name",
        "Users with no team_name or team_name = unknown",
        "SELECT COUNT(*) FROM dim_user WHERE LOWER(COALESCE(team_name,'')) IN ('unknown','')",
        "completeness", denominator=user_total)
    await _rule("R14", "Processed before uploaded",
        "Rows where processed_at < uploaded_at",
        """SELECT COUNT(*) FROM fact_video
           WHERE processed_at IS NOT NULL AND uploaded_at IS NOT NULL
             AND processed_at < uploaded_at""",
        "consistency")
    await _rule("R15", "Published before processed/uploaded",
        "Rows where published_at < processed_at or uploaded_at",
        """SELECT COUNT(*) FROM fact_video
           WHERE published_at IS NOT NULL
             AND (
               (processed_at IS NOT NULL AND published_at < processed_at)
               OR (uploaded_at IS NOT NULL AND published_at < uploaded_at)
             )""",
        "consistency")
    await _rule("R16", "Negative uploaded_duration_sec",
        "Rows with uploaded_duration_sec < 0",
        "SELECT COUNT(*) FROM fact_video WHERE uploaded_duration_sec < 0",
        "consistency")
    await _rule("R17", "Negative created_duration_sec",
        "Rows with created_duration_sec < 0",
        "SELECT COUNT(*) FROM fact_video WHERE created_duration_sec < 0",
        "consistency")
    await _rule("R18", "Missing bridge rows",
        "Videos with no entry in fact_video_output_type",
        """SELECT COUNT(*) FROM fact_video fv
           WHERE NOT EXISTS (
               SELECT 1 FROM fact_video_output_type fvot WHERE fvot.video_id = fv.id
           )""",
        "completeness")

    bridge_total = int(
        (await db.execute(text("SELECT COUNT(*) FROM fact_video_output_type"))).scalar_one() or 1
    )
    await _rule("R19", "Inconsistent output totals",
        "Bridge rows where published_count > created_count",
        "SELECT COUNT(*) FROM fact_video_output_type WHERE published_count > created_count",
        "consistency", denominator=bridge_total)

    critical_count = sum(1 for r in rules if r.severity == "critical")
    warning_count  = sum(1 for r in rules if r.severity == "warning")

    penalty = sum(
        r.affected_pct * (3 if r.severity == "critical" else 1)
        for r in rules if r.severity in ("critical", "warning")
    )
    overall_score = round(max(0.0, 100.0 - penalty / len(rules)), 1) if rules else 100.0

    data = DQRulesResponse(
        rules=rules,
        overall_score=overall_score,
        total_rows=total,
        critical_count=critical_count,
        warning_count=warning_count,
    )
    rule_ids = [r.rule_id for r in rules]
    return ApiResponse(data=data, meta=build_metadata(
        f, grain="rule-evaluated",
        metrics=rule_ids,
        caveats=[
            "R07, R11, R12 denominators use published row count, not total row count",
            "R13 denominator is dim_user count, not fact_video count",
            "R19 denominator is fact_video_output_type row count",
            "Overall score penalises critical breaches 3× more than warnings",
        ],
        unit="percent",
    ))


# ── legacy /extended alias ─────────────────────────────────────────────────────
@router.get("/extended", response_model=ApiResponse[QualityTrendResponse])
async def quality_extended(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[QualityTrendResponse]:
    """Alias to /quality/trend for backwards compatibility."""
    return await quality_trend(f, db)
