"""SQLAlchemy ORM models – Fact tables."""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class FactVideo(Base):
    """
    One row = one video processing job.
    Primary source: video_list_data_obfuscated.csv (14,920 rows).
    """
    __tablename__ = "fact_video"

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    video_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Foreign keys ───────────────────────────────────────────────────────────
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_client.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_channel.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    language_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_language.id", ondelete="SET NULL"), nullable=True, index=True
    )
    input_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("dim_input_type.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ── Timestamps (epoch seconds; NULL if unknown) ────────────────────────────
    uploaded_at: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    processed_at: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    published_at: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # published flag
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    published_platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    billable_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Durations in seconds (parsed from hh:mm:ss CSVs) ──────────────────────
    uploaded_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Derived lag fields (populated at ingest time) ──────────────────────────
    processing_lag_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publishing_lag_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cycle_lag_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Semantic status fields (Phase 1) ───────────────────────────────────────
    # is_processed: materialized Boolean — TRUE when created_duration_sec > 0.
    # Replaces the inline CASE expression used across every query.
    is_processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    # sla_breach_flag: TRUE when publishing_lag_sec > SLA threshold (7 days).
    # NULL when publishing_lag_sec is unknown.
    sla_breach_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # backlog_age_bucket: categorical from publishing_lag_sec.
    # Values: '< 1 day', '1-3 days', '3-7 days', '> 7 days', NULL.
    backlog_age_bucket: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Data quality flags ─────────────────────────────────────────────────────
    missing_team_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_platform_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalid_url_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duplicate_video_id_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Relationships ──────────────────────────────────────────────────────────
    client: Mapped["DimClient | None"] = relationship(back_populates="videos")     # noqa: F821
    channel: Mapped["DimChannel | None"] = relationship(back_populates="videos")   # noqa: F821
    uploader: Mapped["DimUser | None"] = relationship(back_populates="videos")     # noqa: F821
    language: Mapped["DimLanguage | None"] = relationship(back_populates="videos") # noqa: F821
    input_type: Mapped["DimInputType | None"] = relationship(back_populates="videos") # noqa: F821

    output_type_links: Mapped[list["FactVideoOutputType"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class FactVideoOutputType(Base):
    """Bridge table: one video can produce multiple output types."""
    __tablename__ = "fact_video_output_type"

    video_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fact_video.id", ondelete="CASCADE"), primary_key=True
    )
    output_type_id: Mapped[int] = mapped_column(
        ForeignKey("dim_output_type.id", ondelete="CASCADE"), primary_key=True
    )
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    video: Mapped["FactVideo"] = relationship(back_populates="output_type_links")
    output_type: Mapped["DimOutputType"] = relationship(back_populates="video_links")  # noqa: F821
