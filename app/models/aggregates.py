"""SQLAlchemy ORM models for CSV-backed aggregate tables."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AggMonthlyStat(Base):
    __tablename__ = "agg_monthly_stat"

    month_label: Mapped[str] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggChannelStat(Base):
    __tablename__ = "agg_channel_stat"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("dim_channel.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggUserStat(Base):
    __tablename__ = "agg_user_stat"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("dim_user.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggChannelUserStat(Base):
    __tablename__ = "agg_channel_user_stat"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("dim_channel.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("dim_user.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggInputTypeStat(Base):
    __tablename__ = "agg_input_type_stat"

    input_type_id: Mapped[int] = mapped_column(
        ForeignKey("dim_input_type.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggLanguageStat(Base):
    __tablename__ = "agg_language_stat"

    language_id: Mapped[int] = mapped_column(
        ForeignKey("dim_language.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggOutputTypeStat(Base):
    __tablename__ = "agg_output_type_stat"

    output_type_id: Mapped[int] = mapped_column(
        ForeignKey("dim_output_type.id", ondelete="CASCADE"), primary_key=True
    )
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggChannelPublishing(Base):
    __tablename__ = "agg_channel_publishing"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("dim_channel.id", ondelete="CASCADE"), primary_key=True
    )
    facebook: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    instagram: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linkedin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shorts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    x: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    youtube: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    threads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggChannelPublishingDuration(Base):
    __tablename__ = "agg_channel_publishing_duration"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("dim_channel.id", ondelete="CASCADE"), primary_key=True
    )
    facebook_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    instagram_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linkedin_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reels_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shorts_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    x_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    youtube_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    threads_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
