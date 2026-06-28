"""Helpers that convert FilterParams into SQLAlchemy WHERE conditions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query

from app.api.deps import FilterParams
from app.models.dimensions import DimChannel, DimInputType, DimLanguage, DimUser
from app.models.facts import FactVideo


def apply_fact_video_filters(query: Query, f: FilterParams) -> Query:  # type: ignore[type-arg]
    """Apply all active filters to a SQLAlchemy select() targeting FactVideo."""
    conditions: List = []

    # ── Date range ─────────────────────────────────────────────────────────────
    if f.date_from:
        epoch_from = int(
            datetime(f.date_from.year, f.date_from.month, f.date_from.day,
                     tzinfo=timezone.utc).timestamp()
        )
        conditions.append(FactVideo.uploaded_at >= epoch_from)
    if f.date_to:
        epoch_to = int(
            datetime(f.date_to.year, f.date_to.month, f.date_to.day, 23, 59, 59,
                     tzinfo=timezone.utc).timestamp()
        )
        conditions.append(FactVideo.uploaded_at <= epoch_to)

    # ── Channel ────────────────────────────────────────────────────────────────
    if f.channel:
        # f.channel may be an obfuscated code (A, B, …) or a display name
        conditions.append(
            or_(
                DimChannel.obfuscated_code == f.channel.upper(),
                DimChannel.name == f.channel,
            )
        )

    # ── Language ───────────────────────────────────────────────────────────────
    if f.language:
        conditions.append(
            or_(
                DimLanguage.iso_code == f.language.lower(),
                DimLanguage.display_name == f.language,
            )
        )

    # ── Team member / user ─────────────────────────────────────────────────────
    if f.team_member:
        conditions.append(DimUser.name == f.team_member)

    # ── Input type ─────────────────────────────────────────────────────────────
    if f.input_type:
        conditions.append(DimInputType.name == f.input_type.lower())

    if conditions:
        query = query.where(and_(*conditions))
    return query
