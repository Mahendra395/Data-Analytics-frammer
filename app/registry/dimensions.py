"""Canonical dimension registry for every filterable / groupable dimension
in the Frammer analytics API.

This consolidates the previously scattered maps:
  - ``_DIMS`` in multi_dimensional.py
  - ``_SEGMENT_MAP`` in lag.py
  - ``VALID_SEGMENTS`` in funnel.py
  - per-endpoint inline JOIN strings in channels.py, users.py, growth.py, etc.

Route handlers should import ``DIMENSION_REGISTRY`` and call
``DimDef.join_sql(alias)`` / ``DimDef.name_sql(alias)`` to build queries,
rather than maintaining their own copies.

Dimension inventory
--------------------
  client       — DimClient (slug lookup)
  channel      — DimChannel via FK channel_id
  user         — DimUser via FK user_id (individual user)
  team         — DimUser.team_name via FK user_id (team group-by)
  language     — DimLanguage via FK language_id
  input_type   — DimInputType via FK input_type_id
  output_type  — DimOutputType via bridge fact_video_output_type
  platform     — fact_video.published_platform (direct column, no FK)
  billable_flag — fact_video.billable_flag (boolean column, no FK)
  published_flag — fact_video.published (boolean column, no FK)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DimDef:
    """Complete definition of a single analytics dimension.

    Attributes
    ----------
    name:
        Canonical machine-readable key.
    label:
        Human-readable display name.
    join_template:
        SQL JOIN fragment.  Use ``{alias}`` as the table alias placeholder
        (the alias is appended by the ``__init__`` of the dim, however callers
        should use ``join_sql(alias)``).  Empty string for ``direct`` columns.
    name_col_template:
        SQL expression that resolves to the dimension label value.
        Use ``{alias}`` as the table alias placeholder.
    filter_col:
        Column/expression on ``fact_video`` (or bridge) used for WHERE filtering.
        ``None`` for dimensions that cannot be directly filtered via a single column.
    filter_lookup_sql:
        Correlated sub-select that resolves a filter value to an FK id.
        ``None`` for direct-column dimensions.
    filter_param:
        Name of the attribute on ``FilterParams`` that carries the filter value.
    db_table:
        Primary dimension table name (for reference / documentation).
    supports_bridge:
        True when this dimension lives on the fact_video_output_type bridge table
        rather than on fact_video itself.
    is_direct:
        True when the dimension is a column directly on fact_video (no JOIN needed).
    is_flag:
        True when the dimension is a boolean flag (billable_flag, published_flag).
    """

    name: str
    label: str
    join_template: str
    name_col_template: str
    filter_col: str | None
    filter_lookup_sql: str | None
    filter_param: str | None
    db_table: str
    supports_bridge: bool = False
    is_direct: bool = False
    is_flag: bool = False

    def join_sql(self, alias: str = "d") -> str:
        """Render the JOIN fragment with the given table alias."""
        return self.join_template.format(alias=alias)

    def name_sql(self, alias: str = "d") -> str:
        """Render the name-column expression with the given table alias."""
        return self.name_col_template.format(alias=alias)


# ---------------------------------------------------------------------------
# Dimension Registry
# ---------------------------------------------------------------------------

DIMENSION_REGISTRY: dict[str, DimDef] = {

    "client": DimDef(
        name="client",
        label="Client",
        join_template="JOIN dim_client {alias} ON {alias}.id = fv.client_id",
        name_col_template="{alias}.name",
        filter_col="fv.client_id",
        filter_lookup_sql="(SELECT id FROM dim_client WHERE slug = :client)",
        filter_param="client",
        db_table="dim_client",
    ),

    "channel": DimDef(
        name="channel",
        label="Channel",
        join_template="JOIN dim_channel {alias} ON {alias}.id = fv.channel_id",
        name_col_template="{alias}.name",
        filter_col="fv.channel_id",
        filter_lookup_sql=(
            "(SELECT id FROM dim_channel WHERE obfuscated_code = :channel OR name = :channel)"
        ),
        filter_param="channel",
        db_table="dim_channel",
    ),

    "user": DimDef(
        name="user",
        label="User",
        join_template="JOIN dim_user {alias} ON {alias}.id = fv.user_id",
        name_col_template="{alias}.name",
        filter_col="fv.user_id",
        filter_lookup_sql="(SELECT id FROM dim_user WHERE name = :team_member)",
        filter_param="team_member",
        db_table="dim_user",
    ),

    "team": DimDef(
        name="team",
        label="Team",
        # Same JOIN as user — group-by is on team_name column, not a separate table
        join_template="JOIN dim_user {alias} ON {alias}.id = fv.user_id",
        name_col_template="{alias}.team_name",
        filter_col="fv.user_id",
        # Filter by team name: select all users belonging to that team
        filter_lookup_sql="(SELECT id FROM dim_user WHERE team_name = :team_member)",
        filter_param="team_member",
        db_table="dim_user",
    ),

    "language": DimDef(
        name="language",
        label="Language",
        join_template="JOIN dim_language {alias} ON {alias}.id = fv.language_id",
        name_col_template="{alias}.display_name",
        filter_col="fv.language_id",
        filter_lookup_sql=(
            "(SELECT id FROM dim_language WHERE iso_code = :language OR display_name = :language)"
        ),
        filter_param="language",
        db_table="dim_language",
    ),

    "input_type": DimDef(
        name="input_type",
        label="Input Type",
        join_template="JOIN dim_input_type {alias} ON {alias}.id = fv.input_type_id",
        name_col_template="{alias}.name",
        filter_col="fv.input_type_id",
        filter_lookup_sql="(SELECT id FROM dim_input_type WHERE name = :input_type)",
        filter_param="input_type",
        db_table="dim_input_type",
    ),

    "output_type": DimDef(
        name="output_type",
        label="Output Type",
        # Bridge join: fact_video_output_type first, then to dim_output_type
        join_template=(
            "JOIN fact_video_output_type fvot ON fvot.video_id = fv.id "
            "JOIN dim_output_type {alias} ON {alias}.id = fvot.output_type_id"
        ),
        name_col_template="{alias}.name",
        filter_col=None,  # output_type filter requires a bridge sub-select
        filter_lookup_sql=(
            "EXISTS ("
            "  SELECT 1 FROM fact_video_output_type fvot2 "
            "  JOIN dim_output_type dot2 ON dot2.id = fvot2.output_type_id "
            "  WHERE fvot2.video_id = fv.id AND dot2.name = :output_type"
            ")"
        ),
        filter_param="output_type",
        db_table="dim_output_type",
        supports_bridge=True,
    ),

    "platform": DimDef(
        name="platform",
        label="Platform",
        # No JOIN needed — column is directly on fact_video
        join_template="",
        name_col_template="fv.published_platform",
        filter_col="fv.published_platform",
        filter_lookup_sql=None,  # direct equality: fv.published_platform = :published_platform
        filter_param="published_platform",
        db_table="fact_video",
        is_direct=True,
    ),

    "billable_flag": DimDef(
        name="billable_flag",
        label="Billable",
        join_template="",
        name_col_template="fv.billable_flag::text",
        filter_col="fv.billable_flag",
        filter_lookup_sql=None,  # direct equality: fv.billable_flag = :billable_flag
        filter_param="billable_flag",
        db_table="fact_video",
        is_direct=True,
        is_flag=True,
    ),

    "published_flag": DimDef(
        name="published_flag",
        label="Published",
        join_template="",
        name_col_template="fv.published::text",
        filter_col="fv.published",
        filter_lookup_sql=None,  # direct equality: fv.published = :published_flag
        filter_param="published_flag",
        db_table="fact_video",
        is_direct=True,
        is_flag=True,
    ),
}
