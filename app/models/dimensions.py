"""SQLAlchemy ORM models – Dimension tables."""
from __future__ import annotations

from sqlalchemy import Date, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class DimClient(Base):
    __tablename__ = "dim_client"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    videos: Mapped[list["FactVideo"]] = relationship(back_populates="client")  # noqa: F821


class DimChannel(Base):
    __tablename__ = "dim_channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    obfuscated_code: Mapped[str | None] = mapped_column(String(10), nullable=True)  # A–R
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("name", "client_id", name="uq_channel_client"),)

    videos: Mapped[list["FactVideo"]] = relationship(back_populates="channel")  # noqa: F821


class DimUser(Base):
    __tablename__ = "dim_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("name", "client_id", name="uq_user_client"),)

    videos: Mapped[list["FactVideo"]] = relationship(back_populates="uploader")  # noqa: F821


class DimLanguage(Base):
    __tablename__ = "dim_language"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iso_code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # e.g. 'en'
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)           # e.g. 'English'

    videos: Mapped[list["FactVideo"]] = relationship(back_populates="language")  # noqa: F821


class DimInputType(Base):
    __tablename__ = "dim_input_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # e.g. interview | news bulletin | special reports | speech | debate |
    #      press conference | discussion-show | sports show

    videos: Mapped[list["FactVideo"]] = relationship(back_populates="input_type")  # noqa: F821


class DimOutputType(Base):
    __tablename__ = "dim_output_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Full package | Key moments | Chapters | My Key moments | Summary

    video_links: Mapped[list["FactVideoOutputType"]] = relationship(  # noqa: F821
        back_populates="output_type"
    )


class DimDate(Base):
    """Pre-populated date dimension for fast time-based grouping."""
    __tablename__ = "dim_date"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[Date] = mapped_column(Date, unique=True, nullable=False)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)   # 1–12
    quarter: Mapped[int] = mapped_column(SmallInteger, nullable=False) # 1–4
    week: Mapped[int] = mapped_column(SmallInteger, nullable=False)    # ISO week
    month_label: Mapped[str] = mapped_column(String(10), nullable=False)  # e.g. 'Mar 25'
    is_weekend: Mapped[bool] = mapped_column(nullable=False, default=False)
