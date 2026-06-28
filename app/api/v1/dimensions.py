"""GET /api/v1/dimensions — all dimension lists for frontend filter dropdowns."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import FilterParams, get_db
from app.models.dimensions import DimChannel, DimClient, DimInputType, DimLanguage, DimOutputType, DimUser
from app.schemas.responses import ApiResponse, DimensionItem, DimensionsResponse
from app.utils.response import build_metadata

router = APIRouter(prefix="/dimensions", tags=["Dimensions"])


@router.get("", response_model=ApiResponse[DimensionsResponse])
async def get_dimensions(
    f: FilterParams = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[DimensionsResponse]:
    """Returns all dimension values for populating filter dropdowns in the frontend."""

    async def fetch(model, value_col: str, label_col: str):
        q = select(getattr(model, value_col), getattr(model, label_col)).order_by(
            getattr(model, label_col)
        )
        res = await db.execute(q)
        return [DimensionItem(value=str(r[0]), label=str(r[1])) for r in res.all()]

    clients     = await fetch(DimClient,    "slug",     "name")
    channels    = await fetch(DimChannel,   "obfuscated_code", "name")

    users_res   = await db.execute(select(DimUser.name).order_by(DimUser.name))
    users       = [DimensionItem(value=r[0], label=r[0]) for r in users_res.all()]

    teams_res   = await db.execute(
        select(DimUser.team_name)
        .where(DimUser.team_name.isnot(None))
        .distinct()
        .order_by(DimUser.team_name)
    )
    teams = [DimensionItem(value=r[0], label=r[0]) for r in teams_res.all() if r[0]]

    languages   = await fetch(DimLanguage,  "iso_code", "display_name")
    input_types = await fetch(DimInputType, "name",     "name")
    output_types = await fetch(DimOutputType, "name",   "name")

    # Platforms — distinct values from fact_video.published_platform
    plat_res = await db.execute(
        text(
            "SELECT DISTINCT published_platform FROM fact_video "
            "WHERE published_platform IS NOT NULL "
            "ORDER BY published_platform"
        )
    )
    platforms = [DimensionItem(value=r[0], label=r[0]) for r in plat_res.all()]

    billable_flag_options = [
        DimensionItem(value="true",  label="Billable"),
        DimensionItem(value="false", label="Non-billable"),
    ]
    published_flag_options = [
        DimensionItem(value="true",  label="Published"),
        DimensionItem(value="false", label="Not published"),
    ]

    date_range_options = [
        DimensionItem(value="last_7d",    label="Last 7 days"),
        DimensionItem(value="last_30d",   label="Last 30 days"),
        DimensionItem(value="last_90d",   label="Last 90 days"),
        DimensionItem(value="this_month", label="This month"),
        DimensionItem(value="last_month", label="Last month"),
        DimensionItem(value="ytd",        label="Year to date"),
        DimensionItem(value="custom",     label="Custom range"),
    ]

    data = DimensionsResponse(
        clients=clients,
        channels=channels,
        users=users,
        teams=teams,
        languages=languages,
        input_types=input_types,
        output_types=output_types,
        platforms=platforms,
        billable_flag_options=billable_flag_options,
        published_flag_options=published_flag_options,
        date_range_options=date_range_options,
    )
    return ApiResponse(
        data=data,
        meta=build_metadata(
            f,
            metrics=[],
            grain="dimension-catalog",
            caveats=["Returns all available dimension values regardless of date filter"],
        ),
    )
