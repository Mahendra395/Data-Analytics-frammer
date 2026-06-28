"""GET /api/v1/registry/* — developer-facing KPI and dimension registry endpoints.

These endpoints expose the canonical metric and dimension definitions from the
registry as JSON, enabling:
  - Frontend contract discovery / documentation generation
  - Automated validation of expected KPI names
  - Debugging unexpected analytics results

These are protected routes (auth required, same as all analytics endpoints).
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from app.api.deps import FilterParams
from app.registry.dimensions import DIMENSION_REGISTRY
from app.registry.metrics import METRIC_REGISTRY
from app.schemas.responses import ApiResponse
from app.utils.response import build_metadata

router = APIRouter(prefix="/registry", tags=["Registry"])


def _metric_to_dict(m) -> Dict[str, Any]:
    return {
        "name": m.name,
        "label": m.label,
        "formula_sql": m.formula_sql,
        "numerator": m.numerator,
        "denominator": m.denominator,
        "source_tables": list(m.source_tables),
        "caveats": m.caveats,
        "valid_dimensions": sorted(m.valid_dimensions) if m.valid_dimensions else "all",
        "valid_time_grains": sorted(m.valid_time_grains),
        "null_handling": m.null_handling,
        "requires_bridge": m.requires_bridge,
        "is_proxy": m.is_proxy,
        "proxy_note": m.proxy_note,
    }


def _dim_to_dict(d) -> Dict[str, Any]:
    return {
        "name": d.name,
        "label": d.label,
        "join_template": d.join_template,
        "name_col_template": d.name_col_template,
        "filter_col": d.filter_col,
        "filter_lookup_sql": d.filter_lookup_sql,
        "filter_param": d.filter_param,
        "db_table": d.db_table,
        "supports_bridge": d.supports_bridge,
        "is_direct": d.is_direct,
        "is_flag": d.is_flag,
    }


@router.get("/metrics", response_model=ApiResponse[List[Dict[str, Any]]])
async def list_metrics(f: FilterParams = Depends()) -> ApiResponse[List[Dict[str, Any]]]:
    """Return all registered KPI metric definitions.

    Includes formula SQL, proxy flags, data quality caveats, and valid
    dimension/time-grain combinations.  Useful for contract validation and
    documentation generation.
    """
    data = [_metric_to_dict(m) for m in METRIC_REGISTRY.values()]
    return ApiResponse(data=data, meta=build_metadata(f, grain="metric-registry",
                       caveats=["Registry definitions are static and not filtered by date"]))


@router.get("/metrics/{name}", response_model=ApiResponse[Dict[str, Any]])
async def get_metric(name: str, f: FilterParams = Depends()) -> ApiResponse[Dict[str, Any]]:
    """Return the definition of a single KPI metric by name."""
    from fastapi import HTTPException
    m = METRIC_REGISTRY.get(name)
    if m is None:
        raise HTTPException(
            status_code=404,
            detail=f"Metric '{name}' not found. Available: {list(METRIC_REGISTRY.keys())}",
        )
    return ApiResponse(data=_metric_to_dict(m),
                       meta=build_metadata(f, grain="metric-registry", metrics=[name]))


@router.get("/dimensions", response_model=ApiResponse[List[Dict[str, Any]]])
async def list_dimensions(f: FilterParams = Depends()) -> ApiResponse[List[Dict[str, Any]]]:
    """Return all registered dimension definitions.

    Includes join templates, filter column mappings, and flags for bridge/direct
    dimensions.  This is distinct from GET /core/dimensions which returns the
    actual dimension values from the database.
    """
    data = [_dim_to_dict(d) for d in DIMENSION_REGISTRY.values()]
    return ApiResponse(data=data, meta=build_metadata(f, grain="dimension-registry",
                       caveats=["Registry definitions are static and not filtered by date"]))


@router.get("/dimensions/{name}", response_model=ApiResponse[Dict[str, Any]])
async def get_dimension(name: str, f: FilterParams = Depends()) -> ApiResponse[Dict[str, Any]]:
    """Return the definition of a single dimension by name."""
    from fastapi import HTTPException
    d = DIMENSION_REGISTRY.get(name)
    if d is None:
        raise HTTPException(
            status_code=404,
            detail=f"Dimension '{name}' not found. Available: {list(DIMENSION_REGISTRY.keys())}",
        )
    return ApiResponse(data=_dim_to_dict(d),
                       meta=build_metadata(f, grain="dimension-registry"))
