"""Service router — maps service_name to inline SQL execution returning ResponseBlocks."""
from __future__ import annotations

import logging
import time
from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import ChartSpec, ResponseBlock, StatValue
from app.api.deps import FilterParams
from app.registry.filters import build_dim_only_where_clause, build_where_clause

logger = logging.getLogger(__name__)

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

AVAILABLE_SERVICES: frozenset[str] = frozenset({
    "kpis", "growth", "quality_summary", "funnel", "monthly_trend",
    "insights", "scores", "anomalies",
})


def _month_epochs(yr: int, mo: int) -> tuple[int, int]:
    first = int(datetime(yr, mo, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(yr, mo)[1]
    last = int(datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return first, last


def _prev_month(yr: int, mo: int) -> tuple[int, int]:
    if mo == 1:
        return yr - 1, 12
    return yr, mo - 1


def _trend(delta_pct: float | None) -> str | None:
    if delta_pct is None:
        return None
    if delta_pct > 0.5:
        return "up"
    if delta_pct < -0.5:
        return "down"
    return "flat"


class ServiceRouter:
    """Execute pre-built analytics services, returning ResponseBlock arrays."""

    async def execute(
        self,
        service_name: str,
        db: AsyncSession,
        f: FilterParams,
    ) -> list[ResponseBlock]:
        handler = {
            "kpis": self._kpis,
            "growth": self._growth,
            "quality_summary": self._quality_summary,
            "funnel": self._funnel,
            "monthly_trend": self._monthly_trend,
            "insights": self._insights,
            "scores": self._scores,
            "anomalies": self._anomalies,
        }.get(service_name)
        if handler is None:
            return [ResponseBlock(
                block_type="markdown",
                title="Unknown Service",
                content=f"Service `{service_name}` is not available. "
                        f"Available: {', '.join(sorted(AVAILABLE_SERVICES))}",
            )]
        try:
            return await handler(db, f)
        except Exception as exc:
            logger.exception("service_router_failed service=%s", service_name)
            return [ResponseBlock(
                block_type="markdown",
                title="Service Error",
                content=f"Failed to execute `{service_name}`: {type(exc).__name__}",
            )]

    # ── KPIs ───────────────────────────────────────────────────────────────────

    async def _kpis(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        all_where, all_params = build_where_clause(f)
        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""
        dim_where, dim_params = build_dim_only_where_clause(f)

        # Core counts
        core_sql = text(f"""
            SELECT
                COUNT(*)                                                      AS total_uploaded,
                SUM(CASE WHEN published THEN 1 ELSE 0 END)                   AS total_published,
                SUM(CASE WHEN is_processed THEN 1 ELSE 0 END)                AS total_processed,
                COALESCE(SUM(uploaded_duration_sec), 0)                      AS uploaded_secs,
                COALESCE(SUM(published_duration_sec), 0)                     AS published_secs,
                COUNT(DISTINCT channel_id)                                   AS active_channels,
                COUNT(DISTINCT user_id)                                      AS active_users,
                COUNT(DISTINCT client_id)                                    AS active_clients
            FROM fact_video fv
            {where_sql}
        """)
        row = (await db.execute(core_sql, all_params)).mappings().one()

        total = int(row["total_uploaded"] or 0)
        total_published = int(row["total_published"] or 0)
        total_processed = int(row["total_processed"] or 0)
        uploaded_hrs = round(int(row["uploaded_secs"] or 0) / 3600, 2)
        published_hrs = round(int(row["published_secs"] or 0) / 3600, 2)
        publish_rate = round(total_published / total * 100, 1) if total else 0.0
        processing_rate = round(total_processed / total * 100, 1) if total else 0.0

        # MoM growth
        mom_pct: float | None = None
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
                            EXTRACT(MONTH FROM to_timestamp(uploaded_at)) DESC) AS rn
                    FROM fact_video fv {mom_where_sql}
                    GROUP BY yr, mo
                ) sub WHERE rn <= 2
            """)
            async with db.begin_nested():
                mom_rows = (await db.execute(mom_sql, dim_params)).mappings().all()
            if len(mom_rows) >= 2:
                curr_cnt = int(mom_rows[0]["cnt"] or 0)
                prev_cnt = int(mom_rows[1]["cnt"] or 0)
                if prev_cnt:
                    mom_pct = round((curr_cnt - prev_cnt) / prev_cnt * 100, 1)
        except Exception:
            pass

        # Volume KPIs block
        volume_stats = [
            StatValue(label="Total Uploaded", value=total, unit="videos",
                      delta_pct=mom_pct, trend=_trend(mom_pct)),
            StatValue(label="Total Published", value=total_published, unit="videos"),
            StatValue(label="Total Processed", value=total_processed, unit="videos"),
            StatValue(label="Uploaded Duration", value=uploaded_hrs, unit="hrs"),
            StatValue(label="Published Duration", value=published_hrs, unit="hrs"),
        ]

        # Rate KPIs block
        rate_stats = [
            StatValue(label="Publish Rate", value=publish_rate, unit="%"),
            StatValue(label="Processing Rate", value=processing_rate, unit="%"),
            StatValue(label="Active Channels", value=int(row["active_channels"] or 0)),
            StatValue(label="Active Users", value=int(row["active_users"] or 0)),
            StatValue(label="Active Clients", value=int(row["active_clients"] or 0)),
        ]

        # Top channels chart
        top_ch_sql = text(f"""
            SELECT dc.name AS channel, COUNT(fv.id) AS uploads
            FROM fact_video fv
            JOIN dim_channel dc ON dc.id = fv.channel_id
            {where_sql}
            GROUP BY dc.name ORDER BY uploads DESC LIMIT 10
        """)
        ch_rows = (await db.execute(top_ch_sql, all_params)).mappings().all()
        ch_columns = ["channel", "uploads"]
        ch_data = [[r["channel"], int(r["uploads"] or 0)] for r in ch_rows]

        blocks: list[ResponseBlock] = [
            ResponseBlock(
                block_type="kpi_grid",
                title="Volume KPIs",
                stats=volume_stats,
            ),
            ResponseBlock(
                block_type="kpi_grid",
                title="Rates & Activity",
                stats=rate_stats,
            ),
        ]
        if ch_data:
            blocks.append(ResponseBlock(
                block_type="chart",
                title="Top 10 Channels by Uploads",
                columns=ch_columns,
                rows=ch_data,
                chart_spec=ChartSpec(
                    chart_type="bar",
                    x="channel",
                    y="uploads",
                    title="Top 10 Channels by Uploads",
                ),
            ))
        return blocks

    # ── Growth ─────────────────────────────────────────────────────────────────

    async def _growth(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        dim_where, dim_params = build_dim_only_where_clause(f)

        # Determine ref month
        if f.date_to:
            ref_yr, ref_mo = f.date_to.year, f.date_to.month
        else:
            latest_sql = text("""
                SELECT EXTRACT(YEAR FROM to_timestamp(uploaded_at))::int AS yr,
                       EXTRACT(MONTH FROM to_timestamp(uploaded_at))::int AS mo
                FROM fact_video WHERE uploaded_at IS NOT NULL
                ORDER BY uploaded_at DESC LIMIT 1
            """)
            latest = (await db.execute(latest_sql)).mappings().first()
            if latest:
                ref_yr, ref_mo = int(latest["yr"]), int(latest["mo"])
            else:
                today = date.today()
                ref_yr, ref_mo = today.year, today.month

        prev_yr, prev_mo = _prev_month(ref_yr, ref_mo)

        async def _qm(yr: int, mo: int) -> dict[str, Any]:
            ep_from, ep_to = _month_epochs(yr, mo)
            where = dim_where + [
                "fv.uploaded_at >= :ep_from",
                "fv.uploaded_at <= :ep_to",
            ]
            params = {**dim_params, "ep_from": ep_from, "ep_to": ep_to}
            sql = text(f"""
                SELECT COUNT(*) AS uploaded,
                       SUM(CASE WHEN published THEN 1 ELSE 0 END) AS published,
                       COALESCE(SUM(uploaded_duration_sec), 0)/3600.0 AS uploaded_hrs
                FROM fact_video fv
                WHERE {' AND '.join(where)}
            """)
            row = (await db.execute(sql, params)).mappings().one()
            return {
                "label": f"{_MONTH_LABELS[mo]} {str(yr)[2:]}",
                "uploaded": int(row["uploaded"] or 0),
                "published": int(row["published"] or 0),
                "uploaded_hrs": round(float(row["uploaded_hrs"] or 0), 2),
            }

        curr = await _qm(ref_yr, ref_mo)
        prev = await _qm(prev_yr, prev_mo)

        def _pct(c: int, p: int) -> float | None:
            return round((c - p) / p * 100, 1) if p else None

        mom_up = _pct(curr["uploaded"], prev["uploaded"])
        mom_pub = _pct(curr["published"], prev["published"])

        stats = [
            StatValue(label=f"Uploads ({curr['label']})", value=curr["uploaded"],
                      unit="videos", delta_pct=mom_up, trend=_trend(mom_up)),
            StatValue(label=f"Published ({curr['label']})", value=curr["published"],
                      unit="videos", delta_pct=mom_pub, trend=_trend(mom_pub)),
            StatValue(label=f"Duration ({curr['label']})", value=curr["uploaded_hrs"], unit="hrs"),
        ]

        chart_columns = ["period", "uploaded", "published"]
        chart_rows = [
            [prev["label"], prev["uploaded"], prev["published"]],
            [curr["label"], curr["uploaded"], curr["published"]],
        ]

        return [
            ResponseBlock(block_type="kpi_grid", title="MoM Growth", stats=stats),
            ResponseBlock(
                block_type="chart",
                title=f"{prev['label']} vs {curr['label']}",
                columns=chart_columns,
                rows=chart_rows,
                chart_spec=ChartSpec(
                    chart_type="bar",
                    x="period",
                    y="uploaded",
                    series=["published"],
                    title=f"Month-over-Month: {prev['label']} vs {curr['label']}",
                ),
            ),
        ]

    # ── Quality Summary ────────────────────────────────────────────────────────

    async def _quality_summary(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        all_where, all_params = build_where_clause(f)
        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""

        # Total rows
        total_r = await db.execute(text(f"SELECT COUNT(*) FROM fact_video fv {where_sql}"), all_params)
        total = int(total_r.scalar_one() or 0)
        if total == 0:
            return [ResponseBlock(
                block_type="markdown", title="Data Quality",
                content="No data matched the current filters.",
            )]

        dq_columns = [
            "video_id", "headline", "source_url", "channel_id", "user_id",
            "language_id", "input_type_id", "uploaded_at", "published_platform",
            "published_url", "uploaded_duration_sec", "created_duration_sec",
        ]
        col_exprs = ", ".join(
            f"SUM(CASE WHEN {col} IS NULL OR {col}::text = '' THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*),0) AS np_{i}"
            for i, col in enumerate(dq_columns)
        )
        dq_sql = text(f"SELECT {col_exprs} FROM fact_video fv {where_sql}")
        dq_row = (await db.execute(dq_sql, all_params)).mappings().one()

        field_scores: list[list[Any]] = []
        score_total = 0.0
        for i, col in enumerate(dq_columns):
            null_pct = round(float(dq_row[f"np_{i}"] or 0) * 100, 1)
            col_score = round(max(0, 100 - null_pct), 1)
            score_total += col_score
            field_scores.append([col, null_pct, col_score])

        overall = round(score_total / len(dq_columns), 1) if dq_columns else 100.0
        stat_block = ResponseBlock(
            block_type="stat",
            title="Overall Data Quality",
            stats=[StatValue(label="DQ Score", value=overall, unit="/100")],
        )
        table_block = ResponseBlock(
            block_type="table",
            title="Per-Field Quality",
            columns=["field", "null_%", "score"],
            rows=field_scores,
        )
        return [stat_block, table_block]

    # ── Funnel ─────────────────────────────────────────────────────────────────

    async def _funnel(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        all_where, all_params = build_where_clause(f)
        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""

        sql = text(f"""
            SELECT
                COUNT(*)                                       AS uploaded,
                SUM(CASE WHEN is_processed THEN 1 ELSE 0 END) AS processed,
                SUM(CASE WHEN published THEN 1 ELSE 0 END)    AS published
            FROM fact_video fv
            {where_sql}
        """)
        row = (await db.execute(sql, all_params)).mappings().one()
        uploaded = int(row["uploaded"] or 0)
        processed = int(row["processed"] or 0)
        published = int(row["published"] or 0)

        def _pct(a: int, b: int) -> float | None:
            return round(a / b * 100, 1) if b else None

        stats = [
            StatValue(label="Uploaded", value=uploaded, unit="videos"),
            StatValue(label="Processed", value=processed, unit="videos"),
            StatValue(label="Published", value=published, unit="videos"),
            StatValue(label="Publish Rate", value=_pct(published, uploaded) or 0, unit="%"),
            StatValue(label="Publish Gap", value=max(0, processed - published), unit="videos"),
        ]

        chart_rows = [
            ["Uploaded", uploaded],
            ["Processed", processed],
            ["Published", published],
        ]

        return [
            ResponseBlock(block_type="kpi_grid", title="Funnel Overview", stats=stats),
            ResponseBlock(
                block_type="chart",
                title="Conversion Funnel",
                columns=["stage", "count"],
                rows=chart_rows,
                chart_spec=ChartSpec(
                    chart_type="bar", x="stage", y="count",
                    title="Upload → Process → Publish Funnel",
                ),
            ),
        ]

    # ── Monthly Trend ──────────────────────────────────────────────────────────

    async def _monthly_trend(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        where, params = build_where_clause(f)
        where = ["fv.uploaded_at IS NOT NULL"] + where
        where_sql = "WHERE " + " AND ".join(where)

        sql = text(f"""
            SELECT
                EXTRACT(YEAR  FROM to_timestamp(fv.uploaded_at))::int  AS year,
                EXTRACT(MONTH FROM to_timestamp(fv.uploaded_at))::int  AS month,
                COUNT(fv.id)                                            AS total_uploaded,
                SUM(CASE WHEN fv.published THEN 1 ELSE 0 END)          AS total_published
            FROM fact_video fv
            {where_sql}
            GROUP BY year, month
            ORDER BY year, month
        """)
        rows = (await db.execute(sql, params)).mappings().all()

        chart_columns = ["month", "total_uploaded", "total_published"]
        chart_rows = [
            [f"{_MONTH_LABELS[int(r['month'])]} {str(int(r['year']))[2:]}",
             int(r["total_uploaded"] or 0),
             int(r["total_published"] or 0)]
            for r in rows
        ]

        return [ResponseBlock(
            block_type="chart",
            title="Monthly Upload & Publish Trend",
            columns=chart_columns,
            rows=chart_rows,
            chart_spec=ChartSpec(
                chart_type="line", x="month", y="total_uploaded",
                series=["total_published"],
                title="Monthly Trend: Uploads vs Published",
            ),
        )]

    # ── Insights ───────────────────────────────────────────────────────────────

    async def _insights(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        from app.services.insight_engine import collect_insight_context
        from app.services.insight_llm import generate_insights

        ctx = await collect_insight_context(db, f)
        result = await generate_insights(ctx)

        blocks: list[ResponseBlock] = [
            ResponseBlock(
                block_type="markdown",
                title="Executive Summary",
                content=result.executive_summary,
            ),
        ]
        if result.top_risks:
            risk_rows = [[r.title, r.severity, r.detail] for r in result.top_risks]
            blocks.append(ResponseBlock(
                block_type="table",
                title="Top Risks",
                columns=["risk", "severity", "detail"],
                rows=risk_rows,
            ))
        if result.top_opportunities:
            opp_rows = [[o.title, o.potential_impact, o.detail] for o in result.top_opportunities]
            blocks.append(ResponseBlock(
                block_type="table",
                title="Top Opportunities",
                columns=["opportunity", "impact", "detail"],
                rows=opp_rows,
            ))
        if result.likely_drivers:
            drv_rows = [[d.metric, d.segment, str(d.contribution_pct)] for d in result.likely_drivers]
            blocks.append(ResponseBlock(
                block_type="table",
                title="Likely Drivers",
                columns=["metric", "segment", "contribution_%"],
                rows=drv_rows,
            ))
        return blocks

    # ── Scores ─────────────────────────────────────────────────────────────────

    async def _scores(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        from app.services.scoring import compute_scores

        blocks: list[ResponseBlock] = []
        for dim in ("channel", "user", "language"):
            result = await compute_scores(db, dim, f)
            rows = [
                [s.segment, s.health_score, s.grade, s.risk_level]
                for s in result.segments[:10]
            ]
            blocks.append(ResponseBlock(
                block_type="table",
                title=f"Health Scores: {dim.title()}",
                columns=["segment", "health_score", "grade", "risk_level"],
                rows=rows,
            ))
        return blocks

    # ── Anomalies ──────────────────────────────────────────────────────────────

    async def _anomalies(self, db: AsyncSession, f: FilterParams) -> list[ResponseBlock]:
        from app.services.anomaly import detect_anomalies

        anomalies = await detect_anomalies(db, f)
        if not anomalies:
            return [ResponseBlock(
                block_type="markdown",
                title="Anomaly Detection",
                content="No significant anomalies detected in the current data.",
            )]

        rows = [
            [a.segment, a.dimension, a.metric, str(a.current_value),
             str(a.previous_value), f"{a.change_pct:+.1f}%", a.severity]
            for a in anomalies[:15]
        ]
        return [ResponseBlock(
            block_type="table",
            title=f"Detected Anomalies ({len(anomalies)} total)",
            columns=["segment", "dimension", "metric", "current", "previous", "change", "severity"],
            rows=rows,
        )]
