"""GET /api/v1/kpis — headline KPI totals with real MoM growth."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.registry.filters import build_compare_where_clause, build_dim_only_where_clause, build_where_clause
from app.schemas.responses import ApiResponse, KPIResponse
from app.services.aggregate_support import supports_kpi_aggregate
from app.utils.response import wrap

router = APIRouter(prefix="/kpis", tags=["KPIs"])


@router.get("", response_model=ApiResponse[KPIResponse])
async def get_kpis(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[KPIResponse]:
    if supports_kpi_aggregate(f):
        totals_sql = text("""
            SELECT
                COALESCE(SUM(uploaded_count), 0) AS total_uploaded,
                COALESCE(SUM(created_count), 0) AS total_created,
                COALESCE(SUM(published_count), 0) AS total_published,
                COALESCE(SUM(uploaded_duration_sec), 0) AS uploaded_secs,
                COALESCE(SUM(created_duration_sec), 0) AS created_secs,
                COALESCE(SUM(published_duration_sec), 0) AS published_secs
            FROM agg_monthly_stat
        """)
        totals = (await db.execute(totals_sql)).mappings().one()

        total = int(totals["total_uploaded"] or 0)
        total_created = int(totals["total_created"] or 0)
        total_published = int(totals["total_published"] or 0)
        uploaded_secs = int(totals["uploaded_secs"] or 0)
        created_secs = int(totals["created_secs"] or 0)
        published_secs = int(totals["published_secs"] or 0)

        active_sql = text("""
            SELECT
                (SELECT COUNT(*) FROM agg_channel_stat WHERE uploaded_count > 0) AS active_channels,
                (SELECT COUNT(*) FROM agg_user_stat WHERE uploaded_count > 0) AS active_users,
                CASE WHEN EXISTS (SELECT 1 FROM agg_monthly_stat WHERE uploaded_count > 0) THEN 1 ELSE 0 END AS active_clients,
                (
                    SELECT COUNT(DISTINCT du.team_name)
                    FROM agg_user_stat aus
                    JOIN dim_user du ON du.id = aus.user_id
                    WHERE aus.uploaded_count > 0
                      AND du.team_name IS NOT NULL
                      AND LOWER(du.team_name) NOT IN ('unknown', '')
                ) AS active_teams
        """)
        active_row = (await db.execute(active_sql)).mappings().one()

        proxy_sql = text("""
            SELECT
                SUM(CASE WHEN fv.is_processed THEN 1 ELSE 0 END) AS total_processed,
                (
                    SELECT dc.name
                    FROM agg_channel_stat acs
                    JOIN dim_channel dc ON dc.id = acs.channel_id
                    ORDER BY acs.uploaded_count DESC, dc.name
                    LIMIT 1
                ) AS top_channel,
                (
                    SELECT dl.display_name
                    FROM agg_language_stat als
                    JOIN dim_language dl ON dl.id = als.language_id
                    ORDER BY als.uploaded_count DESC, dl.display_name
                    LIMIT 1
                ) AS top_language
            FROM fact_video fv
        """)
        proxy_row = (await db.execute(proxy_sql)).mappings().one()
        total_processed = int(proxy_row["total_processed"] or 0)
        top_channel = proxy_row["top_channel"] or ""
        top_language = proxy_row["top_language"] or ""

        mom_growth_pct: float | None = None
        mom_sql = text("""
            SELECT uploaded_count
            FROM agg_monthly_stat
            ORDER BY year DESC, month DESC
            LIMIT 2
        """)
        mom_rows = (await db.execute(mom_sql)).scalars().all()
        if len(mom_rows) >= 2 and int(mom_rows[1] or 0) > 0:
            curr = int(mom_rows[0] or 0)
            prev = int(mom_rows[1] or 0)
            mom_growth_pct = round((curr - prev) / prev * 100, 1)

        try:
            dq_columns = [
                "video_id", "headline", "source_url", "channel_id", "user_id",
                "language_id", "input_type_id", "uploaded_at", "published_platform",
                "published_url", "uploaded_duration_sec", "created_duration_sec",
            ]
            col_exprs = ", ".join(
                f"SUM(CASE WHEN {col} IS NULL OR {col}::text = '' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) AS np_{i}"
                for i, col in enumerate(dq_columns)
            )
            dq_sql = text(f"""
                SELECT {col_exprs}
                FROM fact_video fv
            """)
            dq_row = (await db.execute(dq_sql)).mappings().one()
            score_total = sum(
                max(0.0, 100.0 - float(dq_row[f"np_{i}"] or 0) * 100)
                for i in range(len(dq_columns))
            )
            dq_score = round(score_total / len(dq_columns), 1)
        except Exception:
            dq_score = 0.0

        publish_rate = round(total_published / total, 4) if total else 0.0
        processing_rate = round(total_processed / total, 4) if total else 0.0

        data = KPIResponse(
            total_uploaded=total,
            total_created=total_created,
            total_published=total_published,
            total_processed=total_processed,
            active_teams=int(active_row["active_teams"] or 0),
            publish_rate=publish_rate,
            processing_rate=processing_rate,
            total_uploaded_duration_hrs=round(uploaded_secs / 3600, 2),
            total_created_duration_hrs=round(created_secs / 3600, 2),
            total_published_duration_hrs=round(published_secs / 3600, 2),
            active_channels=int(active_row["active_channels"] or 0),
            active_users=int(active_row["active_users"] or 0),
            active_clients=int(active_row["active_clients"] or 0),
            mom_growth_pct=mom_growth_pct,
            avg_clips_per_video=round(total_created / total, 2) if total else 0.0,
            top_channel=top_channel,
            top_language=top_language,
            dq_score=dq_score,
        )
        return wrap(
            data,
            f,
            metrics=[
                "total_uploaded",
                "total_published",
                "total_processed",
                "publish_rate",
                "processing_rate",
                "dq_score",
                "mom_growth_pct",
            ],
            grain="monthly-aggregated",
            caveats=[
                "total_uploaded, total_created, total_published, durations, and active segment counts come from the modified aggregate tables",
                "total_processed and dq_score remain fact-level proxies because the modified bundle does not preserve processed-video and quality counts explicitly",
            ],
            unit="count",
        )

    # Full filter (dimensions + date range) — used for main counts
    all_where, all_params = build_where_clause(f)
    where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""
    # Dimension-only filter (no date) — used for MoM comparison windows
    dim_where, dim_params = build_dim_only_where_clause(f)
    dim_where_sql = ("WHERE " + " AND ".join(dim_where)) if dim_where else ""

    # ── Core counts ────────────────────────────────────────────────────────────
    core_sql = text(f"""
        SELECT
            COUNT(*)                                                      AS total_uploaded,
            SUM(CASE WHEN published THEN 1 ELSE 0 END)                   AS total_published,
            SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)                AS total_processed,
            COALESCE(SUM(uploaded_duration_sec),  0)                     AS uploaded_secs,
            COALESCE(SUM(created_duration_sec),   0)                     AS created_secs,
            COALESCE(SUM(published_duration_sec), 0)                     AS published_secs,
            COUNT(DISTINCT channel_id)                                   AS active_channels,
            COUNT(DISTINCT user_id)                                      AS active_users,
            COUNT(DISTINCT client_id)                                    AS active_clients
        FROM fact_video fv
        {where_sql}
    """)
    row = (await db.execute(core_sql, all_params)).mappings().one()

    total:           int = int(row["total_uploaded"] or 0)
    total_published: int = int(row["total_published"] or 0)
    total_processed: int = int(row["total_processed"] or 0)
    uploaded_secs:   int = int(row["uploaded_secs"] or 0)
    created_secs:    int = int(row["created_secs"] or 0)
    published_secs:  int = int(row["published_secs"] or 0)
    active_channels: int = int(row["active_channels"] or 0)
    active_users:    int = int(row["active_users"] or 0)
    active_clients:  int = int(row["active_clients"] or 0)

    # ── Active teams ───────────────────────────────────────────────────────────
    teams_sql = text(f"""
        SELECT COUNT(DISTINCT du.team_name)
        FROM fact_video fv
        JOIN dim_user du ON du.id = fv.user_id
        {where_sql + (' AND' if where_sql else 'WHERE')} du.team_name IS NOT NULL
            AND LOWER(du.team_name) NOT IN ('unknown', '')
    """)
    try:
        async with db.begin_nested():
            active_teams = int((await db.execute(teams_sql, all_params)).scalar_one() or 0)
    except Exception:
        active_teams = 0

    # ── MoM growth (last 2 months with actual data) ────────────────────────────
    mom_growth_pct: float | None = None
    try:
        mom_where = dim_where + ["fv.uploaded_at IS NOT NULL"]
        mom_where_sql = "WHERE " + " AND ".join(mom_where)
        mom_sql = text(f"""
            SELECT yr, mo, cnt FROM (
                SELECT
                    EXTRACT(YEAR  FROM to_timestamp(uploaded_at))::int AS yr,
                    EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo,
                    COUNT(*) AS cnt,
                    ROW_NUMBER() OVER (ORDER BY
                        EXTRACT(YEAR  FROM to_timestamp(uploaded_at)) DESC,
                        EXTRACT(MONTH FROM to_timestamp(uploaded_at)) DESC
                    ) AS rn
                FROM fact_video fv
                {mom_where_sql}
                GROUP BY yr, mo
            ) sub WHERE rn <= 2
        """)
        async with db.begin_nested():
            mom_rows = (await db.execute(mom_sql, dim_params)).mappings().all()
        if len(mom_rows) >= 2:
            curr = int(mom_rows[0]["cnt"] or 0)
            prev = int(mom_rows[1]["cnt"] or 0)
            if prev > 0:
                mom_growth_pct = round((curr - prev) / prev * 100, 1)
    except Exception:
        pass

    # ── Clip count from bridge table (filtered via JOIN to fact_video) ──────────
    clip_sql = text(f"""
        SELECT COALESCE(SUM(fvot.created_count), 0)
        FROM fact_video_output_type fvot
        JOIN fact_video fv ON fv.id = fvot.video_id
        {where_sql}
    """)
    clip_result = await db.execute(clip_sql, all_params)
    total_clips = int(clip_result.scalar_one() or 0)

    # ── Top channel ────────────────────────────────────────────────────────────
    top_ch_sql = text(f"""
        SELECT dc.name FROM fact_video fv
        JOIN dim_channel dc ON dc.id = fv.channel_id
        {where_sql}
        GROUP BY dc.name ORDER BY COUNT(fv.id) DESC LIMIT 1
    """)
    top_channel = (await db.execute(top_ch_sql, all_params)).scalar_one_or_none() or ""

    # ── Top language ───────────────────────────────────────────────────────────
    top_lang_sql = text(f"""
        SELECT dl.display_name FROM fact_video fv
        JOIN dim_language dl ON dl.id = fv.language_id
        {where_sql}
        GROUP BY dl.display_name ORDER BY COUNT(fv.id) DESC LIMIT 1
    """)
    top_language = (await db.execute(top_lang_sql, all_params)).scalar_one_or_none() or ""

    # ── DQ score ─────────────────────────────────────────────────────────────
    # Uses the same 12-column formula as /quality/summary for consistency.
    try:
        dq_columns = [
            "video_id", "headline", "source_url", "channel_id", "user_id",
            "language_id", "input_type_id", "uploaded_at", "published_platform",
            "published_url", "uploaded_duration_sec", "created_duration_sec",
        ]
        col_exprs = ", ".join(
            f"SUM(CASE WHEN {col} IS NULL OR {col}::text = '' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) AS np_{i}"
            for i, col in enumerate(dq_columns)
        )
        dq_sql = text(f"""
            SELECT {col_exprs}
            FROM fact_video fv
            {where_sql}
        """)
        async with db.begin_nested():
            dq_row = (await db.execute(dq_sql, all_params)).mappings().one()
        score_total = sum(
            max(0.0, 100.0 - float(dq_row[f"np_{i}"] or 0) * 100)
            for i in range(len(dq_columns))
        )
        dq_score = round(score_total / len(dq_columns), 1)
    except Exception:
        dq_score = 0.0

    processing_rate = round(total_processed / total, 4) if total else 0.0
    publish_rate    = round(total_published / total, 4) if total else 0.0

    # ── Comparison period (optional) ───────────────────────────────────────────
    cmp_uploaded: int | None        = None
    cmp_published: int | None       = None
    cmp_processed: int | None       = None
    cmp_uploaded_hrs: float | None  = None
    cmp_published_hrs: float | None = None
    d_uploaded_pct: float | None    = None
    d_published_pct: float | None   = None
    d_processed_pct: float | None   = None
    d_duration_pct: float | None    = None

    def _pct_delta(curr: int | float, prev: int | float) -> float | None:
        return round((curr - prev) / prev * 100, 1) if prev else None

    if f.compare_mode and getattr(f, "compare_date_from", None):
        cmp_where, cmp_params = build_compare_where_clause(f)
        cmp_where_sql = ("WHERE " + " AND ".join(cmp_where)) if cmp_where else ""

        cmp_sql = text(f"""
            SELECT
                COUNT(*)                                                   AS total_uploaded,
                SUM(CASE WHEN published   THEN 1 ELSE 0 END)               AS total_published,
                SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)              AS total_processed,
                COALESCE(SUM(uploaded_duration_sec),  0)                   AS uploaded_secs,
                COALESCE(SUM(published_duration_sec), 0)                   AS published_secs
            FROM fact_video fv
            {cmp_where_sql}
        """)
        cmp_row = (await db.execute(cmp_sql, cmp_params)).mappings().one()
        cmp_uploaded      = int(cmp_row["total_uploaded"]  or 0)
        cmp_published     = int(cmp_row["total_published"] or 0)
        cmp_processed     = int(cmp_row["total_processed"] or 0)
        cmp_uploaded_hrs  = round(int(cmp_row["uploaded_secs"]  or 0) / 3600, 2)
        cmp_published_hrs = round(int(cmp_row["published_secs"] or 0) / 3600, 2)

        d_uploaded_pct  = _pct_delta(total,           cmp_uploaded)
        d_published_pct = _pct_delta(total_published,  cmp_published)
        d_processed_pct = _pct_delta(total_processed,  cmp_processed)
        d_duration_pct  = _pct_delta(uploaded_secs / 3600, cmp_uploaded_hrs)

    data = KPIResponse(
        total_uploaded=total,
        total_created=total_clips if total_clips else total,
        total_published=total_published,
        total_processed=total_processed,
        active_teams=active_teams,
        publish_rate=publish_rate,
        processing_rate=processing_rate,
        total_uploaded_duration_hrs=round(uploaded_secs / 3600, 2),
        total_created_duration_hrs=round(created_secs / 3600, 2),
        total_published_duration_hrs=round(published_secs / 3600, 2),
        active_channels=active_channels,
        active_users=active_users,
        active_clients=active_clients,
        mom_growth_pct=mom_growth_pct,
        avg_clips_per_video=round(total_clips / total, 2) if total and total_clips else 1.0,
        top_channel=top_channel,
        top_language=top_language,
        dq_score=dq_score,
        # Comparison
        compare_mode=f.compare_mode,
        compare_period_label=getattr(f, "compare_period_label", None),
        comparison_total_uploaded=cmp_uploaded,
        comparison_total_published=cmp_published,
        comparison_total_processed=cmp_processed,
        comparison_uploaded_duration_hrs=cmp_uploaded_hrs,
        comparison_published_duration_hrs=cmp_published_hrs,
        delta_uploaded_pct=d_uploaded_pct,
        delta_published_pct=d_published_pct,
        delta_processed_pct=d_processed_pct,
        delta_duration_pct=d_duration_pct,
    )
    caveats = ["MoM growth requires at least 2 months of data with the current dimension filters"]
    if f.compare_mode:
        caveats.append("Comparison deltas are null when the comparison period has zero records")
    return wrap(
        data, f,
        metrics=["total_uploaded", "total_published", "total_processed", "publish_rate",
                 "processing_rate", "dq_score", "mom_growth_pct"],
        grain="video-level",
        caveats=caveats,
        unit="count",
    )
