"""FastAPI dependencies: auth, filter params."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_supabase_token
from app.db.session import get_db

# ── Auth ───────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

_DEV_BYPASS = get_settings().APP_ENV.lower() == "development"


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> Dict[str, Any]:
    """Validate the Supabase JWT and return the authenticated user dict.
    In development mode (APP_ENV=development), auth can be bypassed with
    the Authorization header value 'Bearer dev' or no header at all."""
    if _DEV_BYPASS:
        if credentials is None or credentials.credentials in ("dev", ""):
            return {"id": "dev-user", "email": "dev@localhost", "role": "admin"}
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await verify_supabase_token(credentials.credentials)


def _resolve_date_range(date_range: str) -> tuple[Optional[date], Optional[date]]:
    """Convert a named date range slug into (date_from, date_to)."""
    today = date.today()
    ranges = {
        "last_7d":    (today - timedelta(days=7),   today),
        "last_30d":   (today - timedelta(days=30),  today),
        "last_90d":   (today - timedelta(days=90),  today),
        "this_month": (today.replace(day=1),         today),
        "last_month": (
            (today.replace(day=1) - timedelta(days=1)).replace(day=1),
            today.replace(day=1) - timedelta(days=1),
        ),
        "ytd":        (today.replace(month=1, day=1), today),
    }
    return ranges.get(date_range, (None, None))


_VALID_COMPARE_MODES = frozenset({"previous_period", "previous_month", "previous_year"})


def _compare_window(
    mode: Optional[str],
    dfrom: Optional[date],
    dto: Optional[date],
) -> tuple[Optional[date], Optional[date], str]:
    """Compute comparison-period (date_from, date_to, label) from the main window.

    - ``previous_period``  — shift the whole range back by its own duration
    - ``previous_month``   — shift each bound back by one calendar month
    - ``previous_year``    — shift each bound back by one calendar year

    Returns ``(None, None, "")`` when mode is absent or main dates are not set.
    """
    if not mode or mode not in _VALID_COMPARE_MODES or dfrom is None or dto is None:
        return None, None, ""

    def _clamp(yr: int, mo: int, day: int) -> date:
        """Return date(yr, mo, day) clamped to the last day of that month."""
        last = monthrange(yr, mo)[1]
        return date(yr, mo, min(day, last))

    if mode == "previous_period":
        span = (dto - dfrom).days + 1
        c_to   = dfrom - timedelta(days=1)
        c_from = c_to  - timedelta(days=span - 1)
        label  = f"{c_from.strftime('%b %d')} – {c_to.strftime('%b %d, %Y')}"
        return c_from, c_to, label

    if mode == "previous_month":
        def _prev_mo(d: date) -> date:
            yr, mo = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
            return _clamp(yr, mo, d.day)
        return _prev_mo(dfrom), _prev_mo(dto), "Previous Month"

    if mode == "previous_year":
        c_from = _clamp(dfrom.year - 1, dfrom.month, dfrom.day)
        c_to   = _clamp(dto.year   - 1, dto.month,   dto.day)
        return c_from, c_to, "Previous Year"

    return None, None, ""  # unreachable but satisfies linters


class FilterParams:
    """Resolved filter parameters passed to every analytics endpoint."""

    def __init__(
        self,
        date_range:        str            = Query(default="all",  alias="dateRange"),
        date_from_raw:     Optional[date] = Query(default=None, alias="dateFrom",
                                                  description="Explicit start date (YYYY-MM-DD) for custom ranges"),
        date_to_raw:       Optional[date] = Query(default=None, alias="dateTo",
                                                  description="Explicit end date (YYYY-MM-DD) for custom ranges"),
        client:            Optional[str]  = Query(default=None),
        channel:           Optional[str]  = Query(default=None),
        language:          Optional[str]  = Query(default=None),
        team_member:       Optional[str]  = Query(default=None, alias="teamMember"),
        input_type:        Optional[str]  = Query(default=None, alias="inputType"),
        output_type:       Optional[str]  = Query(default=None, alias="outputType"),
        published_flag:    Optional[bool] = Query(default=None, alias="publishedFlag"),
        published_platform: Optional[str] = Query(default=None, alias="publishedPlatform"),
        billable_flag:     Optional[bool] = Query(default=None, alias="billableFlag"),
        compare_mode:      Optional[str]  = Query(
            default=None,
            alias="compareMode",
            description=(
                "Activate comparison period. "
                "One of: previous_period | previous_month | previous_year. "
                "The comparison window is computed from the main date range. "
                "The main date range must be set (not 'all') for compare to work."
            ),
        ),
    ):
        self.date_range        = date_range
        self.client            = None if client            in (None, "all", "") else client
        self.channel           = None if channel           in (None, "all", "") else channel
        self.language          = None if language          in (None, "all", "") else language
        self.team_member       = None if team_member       in (None, "all", "") else team_member
        self.input_type        = None if input_type        in (None, "all", "") else input_type
        self.output_type       = None if output_type       in (None, "all", "") else output_type
        self.published_flag    = published_flag
        self.published_platform = None if published_platform in (None, "all", "") else published_platform
        self.billable_flag     = billable_flag

        self.date_from, self.date_to = _resolve_date_range(date_range)
        if date_range not in (
            "last_7d", "last_30d", "last_90d", "this_month", "last_month", "ytd", "custom"
        ):
            # Unknown slug → no date filter
            self.date_from = None
            self.date_to = None

        # Explicit date_from/date_to override the named range
        if date_from_raw is not None:
            self.date_from = date_from_raw
        if date_to_raw is not None:
            self.date_to = date_to_raw

        # ── Comparison period ─────────────────────────────────────────────────
        # Normalise compare_mode; only accept valid values when a main date range is set.
        self.compare_mode: Optional[str] = (
            compare_mode if compare_mode in _VALID_COMPARE_MODES else None
        )
        (
            self.compare_date_from,
            self.compare_date_to,
            self.compare_period_label,
        ) = _compare_window(self.compare_mode, self.date_from, self.date_to)

    def as_dict(self) -> Dict[str, Any]:
        """Serialize active filter values for inclusion in response metadata."""
        d: Dict[str, Any] = {"date_range": self.date_range}
        if self.date_from:
            d["date_from"] = self.date_from.isoformat()
        if self.date_to:
            d["date_to"] = self.date_to.isoformat()
        for key in ("client", "channel", "language", "team_member", "input_type",
                    "output_type", "published_platform"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        if self.published_flag is not None:
            d["published_flag"] = self.published_flag
        if self.billable_flag is not None:
            d["billable_flag"] = self.billable_flag
        if self.compare_mode:
            d["compare_mode"] = self.compare_mode
            d["compare_period_label"] = self.compare_period_label
        return d

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FilterParams(date_range={self.date_range!r}, client={self.client!r}, "
            f"channel={self.channel!r}, language={self.language!r}, "
            f"compare_mode={self.compare_mode!r})"
        )
