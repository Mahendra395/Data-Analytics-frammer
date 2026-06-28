"""Frammer analytics ingestion."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import uuid
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import AsyncSessionLocal, Base, engine
from app.models.aggregates import (
    AggChannelPublishing,
    AggChannelPublishingDuration,
    AggChannelStat,
    AggChannelUserStat,
    AggInputTypeStat,
    AggLanguageStat,
    AggMonthlyStat,
    AggOutputTypeStat,
    AggUserStat,
)
from app.models.dimensions import (
    DimChannel,
    DimClient,
    DimInputType,
    DimLanguage,
    DimOutputType,
    DimUser,
)
from app.models.facts import FactVideo, FactVideoOutputType

LANGUAGE_MAP: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "mix": "Mixed",
    "es": "Spanish",
    "ar": "Arabic",
    "mr": "Marathi",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "bn": "Bengali",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "ur": "Urdu",
    "id": "Indonesian",
    "ru": "Russian",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_DIR = PROJECT_ROOT / "Modified data"
INT64_WRAP_BASE = 1 << 63
SLA_BREACH_SECS = 7 * 86400

PUBLISH_PLATFORM_COLUMNS: dict[str, str] = {
    "Facebook": "facebook",
    "Instagram": "instagram",
    "Linkedin": "linkedin",
    "Reels": "reels",
    "Shorts": "shorts",
    "X": "x",
    "Youtube": "youtube",
    "Threads": "threads",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Frammer analytics CSVs into Postgres.")
    parser.add_argument(
        "--csv-dir",
        default=str(DEFAULT_CSV_DIR),
        help="Path to the CSV bundle directory. Defaults to ../Modified data.",
    )
    return parser.parse_args()


def clean_str(val: object) -> Optional[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s if s else None


def parse_hhmmss(value: object) -> int:
    if value is None or pd.isna(value):
        return 0
    parts = str(value).strip().split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(float(parts[2]))
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), int(float(parts[1]))
            return minutes * 60 + seconds
    except (TypeError, ValueError):
        return 0
    return 0


def parse_count(value: object) -> int:
    if value is None or pd.isna(value):
        return 0
    count = int(value)
    if count < 0:
        return count + INT64_WRAP_BASE
    return count


def stable_int(value: object) -> int:
    digest = hashlib.sha256(str(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def synth_month_epoch(raw_month: str, ordinal: int, seed_value: object) -> int:
    normalized = " ".join(str(raw_month).replace(",", " ").split())
    dt = datetime.strptime(normalized, "%b %Y")
    days = monthrange(dt.year, dt.month)[1]
    seed = stable_int(f"{seed_value}:{ordinal}")
    day = 1 + (seed % days)
    hour = (seed // 37) % 24
    minute = (seed // 997) % 60
    second = (seed // 65537) % 60
    return int(datetime(dt.year, dt.month, day, hour, minute, second, tzinfo=timezone.utc).timestamp())


def synth_processing_lag_sec(
    uploaded_duration_sec: int | None,
    created_duration_sec: int | None,
    channel_id: int | None,
    user_id: int | None,
    missing_team_flag: bool,
    seed_value: object,
) -> int:
    seed = stable_int(f"proc:{seed_value}")
    percentile = seed % 1000
    uploaded_hours = (uploaded_duration_sec or 0) / 3600.0
    created_hours = (created_duration_sec or 0) / 3600.0

    if percentile < 55:
        base_hours = 192.0 + ((seed // 1000) % 168) / 12.0  # 8-22 day tail
    elif percentile < 240:
        base_hours = 28.0 + ((seed // 1000) % 144) / 6.0   # ~1-4 days
    else:
        base_hours = 2.0 + ((seed // 1000) % 160) / 10.0   # ~2-18 hours

    duration_hours = min(18.0, uploaded_hours * 1.5 + created_hours * 2.25)
    ops_penalty = ((channel_id or 0) % 5) * 1.25 + ((user_id or 0) % 7) * 0.5
    if missing_team_flag:
        ops_penalty += 6.0

    lag_hours = max(1.0, base_hours + duration_hours + ops_penalty)
    return int(round(lag_hours * 3600))


def synth_publishing_lag_sec(
    published_duration_sec: int | None,
    channel_id: int | None,
    platform: str,
    seed_value: object,
) -> int:
    seed = stable_int(f"pub:{seed_value}:{platform}")
    percentile = seed % 1000
    published_hours = (published_duration_sec or 0) / 3600.0
    platform_bias = {
        "youtube": 10.0,
        "linkedin": 6.0,
        "facebook": 4.0,
        "instagram": 3.0,
        "threads": 2.0,
        "x": 2.0,
        "reels": 1.5,
        "shorts": 1.5,
    }.get(platform, 2.0)

    if percentile < 40:
        base_hours = 180.0 + ((seed // 1000) % 144) / 8.0  # 7.5-25 day tail
    elif percentile < 220:
        base_hours = 18.0 + ((seed // 1000) % 144) / 6.0   # ~18-42 hours
    else:
        base_hours = 1.0 + ((seed // 1000) % 120) / 10.0   # ~1-13 hours

    lag_hours = max(0.5, base_hours + min(10.0, published_hours * 3.0) + platform_bias + ((channel_id or 0) % 4))
    return int(round(lag_hours * 3600))


def synth_published_url(platform: str, video_id: str | None, fact_id: uuid.UUID) -> str:
    slug = (video_id or str(fact_id).replace("-", ""))[-12:]
    return f"https://published.example/{platform}/{slug}"


def month_parts(raw_month: str) -> tuple[str, int, int]:
    normalized = " ".join(str(raw_month).replace(",", " ").split())
    dt = datetime.strptime(normalized, "%b %Y")
    return f"{dt.strftime('%b')} {str(dt.year)[2:]}", dt.year, dt.month


def distribute_integer(total: int, buckets: list[int], target_total: int) -> list[int]:
    if target_total <= 0 or total <= 0 or not buckets:
        return [0 for _ in buckets]
    if sum(buckets) <= 0:
        base = target_total // len(buckets)
        remainder = target_total % len(buckets)
        return [base + (1 if i < remainder else 0) for i in range(len(buckets))]
    exact = [target_total * weight / total for weight in buckets]
    floors = [int(v) for v in exact]
    remainder = target_total - sum(floors)
    order = sorted(range(len(exact)), key=lambda idx: exact[idx] - floors[idx], reverse=True)
    for idx in order[:remainder]:
        floors[idx] += 1
    return floors


def spread_counts(total_count: int, row_count: int) -> list[int]:
    if row_count <= 0:
        return []
    base = total_count // row_count
    remainder = total_count % row_count
    return [base + (1 if idx < remainder else 0) for idx in range(row_count)]


def round_robin_ids(weighted_items: list[tuple[int, int]]) -> list[int]:
    """Expand weighted ids into a stable round-robin sequence.

    This keeps dominant languages dominant while avoiding a single giant
    contiguous block of one language in fact_video.
    """
    pending = [[item_id, count] for item_id, count in weighted_items if count > 0]
    ordered: list[int] = []
    while pending:
        next_pending: list[list[int]] = []
        for item_id, count in pending:
            ordered.append(item_id)
            if count > 1:
                next_pending.append([item_id, count - 1])
        pending = next_pending
    return ordered


async def upsert_dim(
    session: AsyncSession,
    model: type,
    lookup_col: str,
    lookup_val: str,
    extra: dict | None = None,
) -> int:
    found = await session.execute(select(model).where(getattr(model, lookup_col) == lookup_val))
    obj = found.scalars().first()
    if obj:
        if extra:
            for key, value in extra.items():
                if getattr(obj, key, None) in (None, "") and value not in (None, ""):
                    setattr(obj, key, value)
        return obj.id
    obj = model(**{lookup_col: lookup_val, **(extra or {})})
    session.add(obj)
    await session.flush()
    return obj.id


async def seed_dimensions(session: AsyncSession, csv_dir: Path) -> None:
    print("Seeding dimensions...")
    client_id = await upsert_dim(session, DimClient, "slug", "client-1", extra={"name": "CLIENT 1"})

    language_csv = csv_dir / "combined_data(2025-3-1-2026-2-28) by language.csv"
    if language_csv.exists():
        lang_df = pd.read_csv(language_csv)
        for raw in lang_df.iloc[:, 0].dropna().tolist():
            iso = str(raw).strip().lower()
            await upsert_dim(
                session,
                DimLanguage,
                "iso_code",
                iso,
                extra={"display_name": LANGUAGE_MAP.get(iso, iso.upper())},
            )
    for iso, display in LANGUAGE_MAP.items():
        await upsert_dim(session, DimLanguage, "iso_code", iso, extra={"display_name": display})

    input_csv = csv_dir / "combined_data(2025-3-1-2026-2-28) by input type.csv"
    if input_csv.exists():
        input_df = pd.read_csv(input_csv)
        for raw in input_df.iloc[:, 0].dropna().tolist():
            await upsert_dim(session, DimInputType, "name", str(raw).strip().lower())

    output_csv = csv_dir / "combined_data(2025-3-1-2026-2-28) by output type.csv"
    if output_csv.exists():
        output_df = pd.read_csv(output_csv)
        for raw in output_df.iloc[:, 0].dropna().tolist():
            await upsert_dim(session, DimOutputType, "name", str(raw).strip())

    channel_csv = csv_dir / "CLIENT 1 combined_data(2025-3-1-2026-2-28).csv"
    if channel_csv.exists():
        channel_df = pd.read_csv(channel_csv)
        for raw in channel_df.iloc[:, 0].dropna().tolist():
            code = str(raw).strip()
            await upsert_dim(
                session,
                DimChannel,
                "obfuscated_code",
                code,
                extra={"name": code, "client_id": client_id},
            )

    user_csv = csv_dir / "combined_data(2025-3-1-2026-2-28) by user.csv"
    if user_csv.exists():
        user_df = pd.read_csv(user_csv)
        for raw in user_df.iloc[:, 0].dropna().tolist():
            await upsert_dim(session, DimUser, "name", str(raw).strip(), extra={"client_id": client_id})

    await session.commit()
    print("  Dimensions ready.")


async def reset_analytics_tables(session: AsyncSession) -> None:
    print("Resetting analytics tables...")
    for model in (
        AggChannelPublishingDuration,
        AggChannelPublishing,
        AggOutputTypeStat,
        AggLanguageStat,
        AggInputTypeStat,
        AggChannelUserStat,
        AggUserStat,
        AggChannelStat,
        AggMonthlyStat,
        FactVideoOutputType,
        FactVideo,
    ):
        await session.execute(delete(model))
    await session.commit()
    print("  Existing analytics data cleared.")


async def load_fact_video(session: AsyncSession, csv_dir: Path) -> None:
    csv_path = csv_dir / "video_list_data_obfuscated.csv"
    if not csv_path.exists():
        print("  video_list_data_obfuscated.csv missing, skipping fact_video load.")
        return

    print("Loading fact_video...")
    df = pd.read_csv(csv_path, low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    client_id = (await session.execute(select(DimClient.id).where(DimClient.slug == "client-1"))).scalar_one()
    default_lang_id = (
        await session.execute(select(DimLanguage.id).where(DimLanguage.iso_code == "en"))
    ).scalar_one()

    user_rows = await session.execute(select(DimUser.name, DimUser.id))
    user_map = {name: dim_id for name, dim_id in user_rows.all()}
    user_obj_rows = await session.execute(select(DimUser))
    user_obj_map = {user.name: user for user in user_obj_rows.scalars().all()}
    input_rows = await session.execute(select(DimInputType.name, DimInputType.id))
    input_map = {name.lower(): dim_id for name, dim_id in input_rows.all()}

    batch: list[dict] = []
    for _, row in df.iterrows():
        user_name = clean_str(row.get("Uploaded By"))
        team_name = clean_str(row.get("Team Name"))
        user_id = user_map.get(user_name) if user_name else None
        if user_name and user_id is None:
            user = DimUser(name=user_name, client_id=client_id, team_name=team_name)
            session.add(user)
            await session.flush()
            user_map[user_name] = user.id
            user_obj_map[user_name] = user
            user_id = user.id
        elif user_name and team_name and user_name in user_obj_map:
            user_obj = user_obj_map[user_name]
            if not user_obj.team_name or str(user_obj.team_name).strip().lower() == "unknown":
                user_obj.team_name = team_name

        input_type_raw = clean_str(row.get("Type"))
        published_str = (clean_str(row.get("Published")) or "").lower()
        batch.append(
            {
                "id": uuid.uuid4(),
                "video_id": clean_str(row.get("Video ID")),
                "headline": clean_str(row.get("Headline")),
                "source_url": clean_str(row.get("Source")),
                "client_id": client_id,
                "channel_id": None,
                "user_id": user_id,
                "language_id": default_lang_id,
                "input_type_id": input_map.get(input_type_raw.lower()) if input_type_raw else None,
                "uploaded_at": None,
                "processed_at": None,
                "published_at": None,
                "published": published_str in {"yes", "true", "1"},
                "published_platform": clean_str(row.get("Published Platform")),
                "published_url": clean_str(row.get("Published URL")),
                "billable_flag": False,
                "uploaded_duration_sec": 0,
                "created_duration_sec": 0,
                "published_duration_sec": 0,
                "is_processed": False,
                "processing_lag_sec": None,
                "publishing_lag_sec": None,
                "total_cycle_lag_sec": None,
                "sla_breach_flag": None,
                "backlog_age_bucket": None,
                "missing_team_flag": team_name in (None, "", "unknown", "Unknown"),
                "missing_platform_flag": clean_str(row.get("Published Platform")) is None,
                "invalid_url_flag": False,
                "duplicate_video_id_flag": False,
            }
        )
        if len(batch) >= 1000:
            await session.execute(FactVideo.__table__.insert(), batch)
            await session.flush()
            batch = []

    if batch:
        await session.execute(FactVideo.__table__.insert(), batch)
    await session.commit()
    print(f"  Loaded {len(df)} fact_video rows.")


async def enrich_channel_durations(session: AsyncSession, csv_dir: Path) -> None:
    csv_path = csv_dir / "combined_data(2025-3-1-2026-2-28) by channel and user.csv"
    if not csv_path.exists():
        return

    print("Assigning channel and duration values...")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]

    channel_rows = await session.execute(select(DimChannel.obfuscated_code, DimChannel.id))
    channel_map = {str(code).strip(): dim_id for code, dim_id in channel_rows.all() if code}
    user_rows = await session.execute(select(DimUser.name, DimUser.id))
    user_map = {name: dim_id for name, dim_id in user_rows.all()}
    fact_rows = await session.execute(select(FactVideo.id, FactVideo.user_id).order_by(FactVideo.id))

    rows_by_user: dict[int, list[uuid.UUID]] = {}
    for fact_id, user_id in fact_rows.all():
        if user_id is not None:
            rows_by_user.setdefault(user_id, []).append(fact_id)

    updates: list[dict] = []
    for user_name, user_df in df.groupby(df.columns[1]):
        clean_user = clean_str(user_name)
        user_id = user_map.get(clean_user) if clean_user else None
        if user_id is None:
            continue
        fact_ids = rows_by_user.get(user_id, [])
        if not fact_ids:
            continue

        user_df = user_df.copy()
        user_df["uploaded_count_i"] = user_df[df.columns[2]].fillna(0).astype(int)
        positive_rows = user_df[user_df["uploaded_count_i"] > 0]
        if positive_rows.empty:
            continue

        allocations = distribute_integer(
            int(positive_rows["uploaded_count_i"].sum()),
            positive_rows["uploaded_count_i"].tolist(),
            len(fact_ids),
        )

        cursor = 0
        for alloc, (_, row) in zip(allocations, positive_rows.iterrows()):
            channel_id = channel_map.get(clean_str(row[df.columns[0]]) or "")
            if channel_id is None or alloc <= 0:
                continue
            selected_ids = fact_ids[cursor:cursor + alloc]
            cursor += alloc
            if not selected_ids:
                continue

            up_parts = spread_counts(parse_hhmmss(row[df.columns[5]]), len(selected_ids))
            cr_parts = spread_counts(parse_hhmmss(row[df.columns[6]]), len(selected_ids))
            pub_parts = spread_counts(parse_hhmmss(row[df.columns[7]]), len(selected_ids))
            for idx, fact_id in enumerate(selected_ids):
                updates.append(
                    {
                        "fact_id": fact_id,
                        "channel_id": channel_id,
                        "uploaded_duration_sec": up_parts[idx],
                        "created_duration_sec": cr_parts[idx],
                        "published_duration_sec": pub_parts[idx],
                    }
                )

    if updates:
        await session.execute(
            text(
                """
                UPDATE fact_video
                SET channel_id = :channel_id,
                    uploaded_duration_sec = :uploaded_duration_sec,
                    created_duration_sec = :created_duration_sec,
                    published_duration_sec = :published_duration_sec
                WHERE id = :fact_id
                """
            ),
            updates,
        )
        await session.commit()
    print(f"  Updated {len(updates)} fact rows.")


async def enrich_monthly_timestamps(session: AsyncSession, csv_dir: Path) -> None:
    csv_path = csv_dir / "monthly-chart.csv"
    if not csv_path.exists():
        return

    print("Assigning monthly timestamps...")
    monthly_df = pd.read_csv(csv_path)
    monthly_df.columns = [c.strip() for c in monthly_df.columns]
    monthly_df["uploaded_count_i"] = monthly_df[monthly_df.columns[1]].fillna(0).astype(int)

    fact_ids = list((await session.execute(select(FactVideo.id).order_by(FactVideo.id))).scalars().all())
    allocations = distribute_integer(
        int(monthly_df["uploaded_count_i"].sum()),
        monthly_df["uploaded_count_i"].tolist(),
        len(fact_ids),
    )

    updates: list[dict] = []
    cursor = 0
    for alloc, (_, row) in zip(allocations, monthly_df.iterrows()):
        if alloc <= 0:
            continue
        month_label = row[monthly_df.columns[0]]
        for ordinal, fact_id in enumerate(fact_ids[cursor:cursor + alloc]):
            updates.append(
                {
                    "fact_id": fact_id,
                    "uploaded_at": synth_month_epoch(str(month_label), ordinal, fact_id),
                }
            )
        cursor += alloc

    if updates:
        await session.execute(
            text("UPDATE fact_video SET uploaded_at = :uploaded_at WHERE id = :fact_id"),
            updates,
        )
        await session.commit()
    print(f"  Assigned month buckets to {len(updates)} fact rows.")


async def synthesize_pipeline_timestamps(session: AsyncSession, csv_dir: Path) -> None:
    publishing_csv = csv_dir / "channel-wise-publishing.csv"
    if not publishing_csv.exists():
        return

    print("Synthesizing processing and publishing timestamps...")

    channel_rows = await session.execute(select(DimChannel.obfuscated_code, DimChannel.id))
    channel_map = {str(code).strip(): dim_id for code, dim_id in channel_rows.all() if code}

    publishing_df = pd.read_csv(publishing_csv)
    publishing_df.columns = [c.strip() for c in publishing_df.columns]

    publish_plan: dict[int, list[str]] = {}
    for _, row in publishing_df.iterrows():
        channel_key = clean_str(row.get("Channels")) or ""
        channel_id = channel_map.get(channel_key)
        if channel_id is None:
            continue

        slots: list[tuple[int, str]] = []
        for col, platform in PUBLISH_PLATFORM_COLUMNS.items():
            count = parse_count(row.get(col, 0))
            for idx in range(count):
                slots.append((stable_int(f"{channel_key}:{platform}:{idx}"), platform))
        slots.sort(key=lambda item: item[0])
        publish_plan[channel_id] = [platform for _, platform in slots]

    fact_rows = (
        await session.execute(
            select(
                FactVideo.id,
                FactVideo.video_id,
                FactVideo.channel_id,
                FactVideo.user_id,
                FactVideo.uploaded_at,
                FactVideo.created_duration_sec,
                FactVideo.published_duration_sec,
                FactVideo.uploaded_duration_sec,
                FactVideo.missing_team_flag,
            ).order_by(FactVideo.uploaded_at, FactVideo.id)
        )
    ).mappings().all()

    updates_by_id: dict[uuid.UUID, dict] = {}
    publish_candidates: dict[int, list[dict]] = {}

    for row in fact_rows:
        row_id = row["id"]
        video_id = row["video_id"]
        uploaded_at = row["uploaded_at"]
        created_duration_sec = row["created_duration_sec"] or 0
        channel_id = row["channel_id"]
        user_id = row["user_id"]
        missing_team_flag = bool(row["missing_team_flag"])

        update = {
            "fact_id": row_id,
            "published": False,
            "published_platform": None,
            "published_url": None,
            "processed_at": None,
            "published_at": None,
            "processing_lag_sec": None,
            "publishing_lag_sec": None,
            "total_cycle_lag_sec": None,
        }

        if uploaded_at is not None and created_duration_sec > 0:
            proc_lag_sec = synth_processing_lag_sec(
                row["uploaded_duration_sec"],
                created_duration_sec,
                channel_id,
                user_id,
                missing_team_flag,
                video_id or row_id,
            )
            processed_at = int(uploaded_at) + proc_lag_sec
            update["processed_at"] = processed_at
            update["processing_lag_sec"] = proc_lag_sec

            if channel_id is not None:
                publish_candidates.setdefault(channel_id, []).append(
                    {
                        "fact_id": row_id,
                        "video_id": video_id,
                        "processed_at": processed_at,
                        "published_duration_sec": row["published_duration_sec"],
                        "rank_key": (
                            0 if (row["published_duration_sec"] or 0) > 0 else 1,
                            stable_int(f"publish-rank:{video_id or row_id}"),
                        ),
                    }
                )

        updates_by_id[row_id] = update

    for channel_id, platforms in publish_plan.items():
        if not platforms:
            continue

        candidates = publish_candidates.get(channel_id, [])
        if not candidates:
            continue

        candidates.sort(key=lambda item: item["rank_key"])
        selected = candidates[:len(platforms)]

        for candidate, platform in zip(selected, platforms):
            row_update = updates_by_id[candidate["fact_id"]]
            pub_lag_sec = synth_publishing_lag_sec(
                candidate["published_duration_sec"],
                channel_id,
                platform,
                candidate["video_id"] or candidate["fact_id"],
            )
            published_at = int(candidate["processed_at"]) + pub_lag_sec
            processing_lag_sec = int(row_update["processing_lag_sec"] or 0)

            row_update["published"] = True
            row_update["published_platform"] = platform
            row_update["published_url"] = synth_published_url(platform, candidate["video_id"], candidate["fact_id"])
            row_update["published_at"] = published_at
            row_update["publishing_lag_sec"] = pub_lag_sec
            row_update["total_cycle_lag_sec"] = processing_lag_sec + pub_lag_sec

    updates = list(updates_by_id.values())
    if updates:
        await session.execute(
            text(
                """
                UPDATE fact_video
                SET published = :published,
                    published_platform = :published_platform,
                    published_url = :published_url,
                    processed_at = :processed_at,
                    published_at = :published_at,
                    processing_lag_sec = :processing_lag_sec,
                    publishing_lag_sec = :publishing_lag_sec,
                    total_cycle_lag_sec = :total_cycle_lag_sec
                WHERE id = :fact_id
                """
            ),
            updates,
        )
        await session.commit()

    print(f"  Synthesized pipeline timestamps for {len(updates)} fact rows.")


async def enrich_language_distribution(session: AsyncSession, csv_dir: Path) -> None:
    csv_path = csv_dir / "combined_data(2025-3-1-2026-2-28) by language.csv"
    if not csv_path.exists():
        return

    print("Assigning language distribution...")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    df["uploaded_count_i"] = df[df.columns[1]].map(parse_count)
    positive_rows = df[df["uploaded_count_i"] > 0].copy()
    if positive_rows.empty:
        print("  No positive language rows found.")
        return

    language_rows = await session.execute(select(DimLanguage.iso_code, DimLanguage.id))
    language_map = {str(code).strip().lower(): dim_id for code, dim_id in language_rows.all() if code}
    default_lang_id = (
        await session.execute(select(DimLanguage.id).where(DimLanguage.iso_code == "en"))
    ).scalar_one()
    fact_ids = list((await session.execute(select(FactVideo.id).order_by(FactVideo.id))).scalars().all())
    if not fact_ids:
        print("  No fact_video rows available for language assignment.")
        return

    allocations = distribute_integer(
        int(positive_rows["uploaded_count_i"].sum()),
        positive_rows["uploaded_count_i"].tolist(),
        len(fact_ids),
    )

    weighted_ids: list[tuple[int, int]] = []
    for alloc, (_, row) in zip(allocations, positive_rows.iterrows()):
        iso_code = (clean_str(row[df.columns[0]]) or "").lower()
        language_id = language_map.get(iso_code)
        if language_id is None or alloc <= 0:
            continue
        weighted_ids.append((language_id, alloc))

    assignment_ids = round_robin_ids(weighted_ids)
    if len(assignment_ids) < len(fact_ids):
        assignment_ids.extend([default_lang_id] * (len(fact_ids) - len(assignment_ids)))

    updates = [
        {"fact_id": fact_id, "language_id": assignment_ids[idx]}
        for idx, fact_id in enumerate(fact_ids)
    ]
    await session.execute(
        text("UPDATE fact_video SET language_id = :language_id WHERE id = :fact_id"),
        updates,
    )
    await session.commit()
    print(f"  Assigned languages to {len(updates)} fact rows.")


async def load_output_types_bridge(session: AsyncSession, csv_dir: Path) -> None:
    csv_path = csv_dir / "combined_data(2025-3-1-2026-2-28) by output type.csv"
    if not csv_path.exists():
        return

    print("Loading output type bridge...")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]

    output_rows = await session.execute(select(DimOutputType.name, DimOutputType.id))
    output_map = {name: dim_id for name, dim_id in output_rows.all()}
    fact_ids = list((await session.execute(select(FactVideo.id).order_by(FactVideo.id))).scalars().all())
    if not fact_ids:
        return

    df["created_count_i"] = df[df.columns[2]].map(parse_count)
    allocations = distribute_integer(
        int(df["created_count_i"].sum()),
        df["created_count_i"].tolist(),
        len(fact_ids),
    )

    inserts: list[dict] = []
    cursor = 0
    for alloc, (_, row) in zip(allocations, df.iterrows()):
        output_type_id = output_map.get(clean_str(row[df.columns[0]]) or "")
        if output_type_id is None or alloc <= 0:
            continue
        selected_ids = fact_ids[cursor:cursor + alloc]
        cursor += alloc
        created_counts = spread_counts(parse_count(row[df.columns[2]]), len(selected_ids))
        published_counts = spread_counts(parse_count(row[df.columns[3]]), len(selected_ids))
        for idx, fact_id in enumerate(selected_ids):
            inserts.append(
                {
                    "video_id": fact_id,
                    "output_type_id": output_type_id,
                    "created_count": created_counts[idx],
                    "published_count": min(created_counts[idx], published_counts[idx]),
                }
            )

    if inserts:
        await session.execute(FactVideoOutputType.__table__.insert(), inserts)
        await session.commit()
    print(f"  Inserted {len(inserts)} bridge rows.")


async def materialize_semantic_fields(session: AsyncSession) -> None:
    print("Refreshing derived flags...")
    await session.execute(
        text(
            f"""
            UPDATE fact_video
            SET is_processed = (processed_at IS NOT NULL OR COALESCE(created_duration_sec, 0) > 0),
                published = (published_at IS NOT NULL OR published = TRUE),
                processing_lag_sec = CASE
                    WHEN processing_lag_sec IS NOT NULL THEN processing_lag_sec
                    WHEN processed_at IS NOT NULL AND uploaded_at IS NOT NULL THEN processed_at - uploaded_at
                    ELSE NULL
                END,
                publishing_lag_sec = CASE
                    WHEN publishing_lag_sec IS NOT NULL THEN publishing_lag_sec
                    WHEN published_at IS NOT NULL AND processed_at IS NOT NULL THEN published_at - processed_at
                    ELSE NULL
                END,
                total_cycle_lag_sec = CASE
                    WHEN total_cycle_lag_sec IS NOT NULL THEN total_cycle_lag_sec
                    WHEN published_at IS NOT NULL AND uploaded_at IS NOT NULL THEN published_at - uploaded_at
                    ELSE NULL
                END,
                sla_breach_flag = CASE
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) IS NULL THEN NULL
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) > {SLA_BREACH_SECS} THEN TRUE
                    ELSE FALSE
                END,
                backlog_age_bucket = CASE
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) IS NULL THEN NULL
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) < 86400 THEN '< 1 day'
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) < 3 * 86400 THEN '1-3 days'
                    WHEN COALESCE(total_cycle_lag_sec, processing_lag_sec) < 7 * 86400 THEN '3-7 days'
                    ELSE '> 7 days'
                END,
                invalid_url_flag = CASE
                    WHEN published_url IS NULL THEN FALSE
                    WHEN published_url LIKE 'http%' THEN FALSE
                    ELSE TRUE
                END
            """
        )
    )
    await session.execute(
        text(
            """
            UPDATE fact_video
            SET duplicate_video_id_flag = FALSE
            """
        )
    )
    await session.execute(
        text(
            """
            UPDATE fact_video
            SET missing_team_flag = (
                    user_id IS NULL OR user_id IN (
                        SELECT id FROM dim_user
                        WHERE team_name IS NULL OR team_name = '' OR LOWER(team_name) = 'unknown'
                    )
                ),
                missing_platform_flag = (published_platform IS NULL OR published_platform = '')
            """
        )
    )
    await session.commit()
    print("  Derived flags refreshed.")


async def load_aggregate_tables(session: AsyncSession, csv_dir: Path) -> None:
    print("Loading aggregate tables...")

    monthly_df = pd.read_csv(csv_dir / "monthly-chart.csv")
    duration_df = pd.read_csv(csv_dir / "month-wise-duration.csv")
    monthly_df.columns = [c.strip() for c in monthly_df.columns]
    duration_df.columns = [c.strip() for c in duration_df.columns]
    monthly_merged = monthly_df.merge(duration_df, on="Month", how="left")
    monthly_rows = []
    for _, row in monthly_merged.iterrows():
        label, year, month = month_parts(str(row["Month"]))
        monthly_rows.append(
            {
                "month_label": label,
                "year": year,
                "month": month,
                "uploaded_count": parse_count(row["Total Uploaded"]),
                "created_count": parse_count(row["Total Created"]),
                "published_count": parse_count(row["Total Published"]),
                "uploaded_duration_sec": parse_hhmmss(row["Total Uploaded Duration"]),
                "created_duration_sec": parse_hhmmss(row["Total Created Duration"]),
                "published_duration_sec": parse_hhmmss(row["Total Published Duration"]),
            }
        )
    await session.execute(AggMonthlyStat.__table__.insert(), monthly_rows)

    channel_rows = await session.execute(select(DimChannel.obfuscated_code, DimChannel.id))
    channel_map = {str(code).strip(): dim_id for code, dim_id in channel_rows.all() if code}
    user_rows = await session.execute(select(DimUser.name, DimUser.id))
    user_map = {name: dim_id for name, dim_id in user_rows.all()}
    input_rows = await session.execute(select(DimInputType.name, DimInputType.id))
    input_map = {name.lower(): dim_id for name, dim_id in input_rows.all()}
    language_rows = await session.execute(select(DimLanguage.iso_code, DimLanguage.id))
    language_map = {code.lower(): dim_id for code, dim_id in language_rows.all()}
    output_rows = await session.execute(select(DimOutputType.name, DimOutputType.id))
    output_map = {name: dim_id for name, dim_id in output_rows.all()}

    def read_stats(name: str) -> pd.DataFrame:
        df = pd.read_csv(csv_dir / name)
        df.columns = [c.strip() for c in df.columns]
        return df

    def add_counts(store: dict, key: object, payload: dict) -> None:
        normalized = {}
        for field, value in payload.items():
            if value is None or isinstance(value, str | bool):
                normalized[field] = value
            else:
                normalized[field] = int(value)
        if key not in store:
            store[key] = normalized
            return
        for field, value in normalized.items():
            if field in {"channel_id", "user_id", "input_type_id", "language_id", "output_type_id"}:
                continue
            store[key][field] = int(store[key][field]) + int(value)

    for model, filename, key_lookup in (
        (AggChannelStat, "CLIENT 1 combined_data(2025-3-1-2026-2-28).csv", channel_map),
        (AggUserStat, "combined_data(2025-3-1-2026-2-28) by user.csv", user_map),
    ):
        df = read_stats(filename)
        inserts_by_key: dict[object, dict] = {}
        for _, row in df.iterrows():
            key = clean_str(row[df.columns[0]]) or ""
            dim_id = key_lookup.get(key)
            if dim_id is None:
                continue
            field = "channel_id" if model is AggChannelStat else "user_id"
            payload = (
                {
                    field: dim_id,
                    "uploaded_count": parse_count(row[df.columns[1]]),
                    "created_count": parse_count(row[df.columns[2]]),
                    "published_count": parse_count(row[df.columns[3]]),
                    "uploaded_duration_sec": parse_hhmmss(row[df.columns[4]]),
                    "created_duration_sec": parse_hhmmss(row[df.columns[5]]),
                    "published_duration_sec": parse_hhmmss(row[df.columns[6]]),
                }
            )
            add_counts(inserts_by_key, dim_id, payload)
        if inserts_by_key:
            await session.execute(model.__table__.insert(), list(inserts_by_key.values()))

    channel_user_df = read_stats("combined_data(2025-3-1-2026-2-28) by channel and user.csv")
    channel_user_inserts: dict[tuple[int, int], dict] = {}
    for _, row in channel_user_df.iterrows():
        channel_id = channel_map.get(clean_str(row[channel_user_df.columns[0]]) or "")
        user_id = user_map.get(clean_str(row[channel_user_df.columns[1]]) or "")
        if channel_id is None or user_id is None:
            continue
        payload = (
            {
                "channel_id": channel_id,
                "user_id": user_id,
                "uploaded_count": parse_count(row[channel_user_df.columns[2]]),
                "created_count": parse_count(row[channel_user_df.columns[3]]),
                "published_count": parse_count(row[channel_user_df.columns[4]]),
                "uploaded_duration_sec": parse_hhmmss(row[channel_user_df.columns[5]]),
                "created_duration_sec": parse_hhmmss(row[channel_user_df.columns[6]]),
                "published_duration_sec": parse_hhmmss(row[channel_user_df.columns[7]]),
            }
        )
        add_counts(channel_user_inserts, (channel_id, user_id), payload)
    if channel_user_inserts:
        await session.execute(AggChannelUserStat.__table__.insert(), list(channel_user_inserts.values()))

    for model, filename, key_map, normalize in (
        (AggInputTypeStat, "combined_data(2025-3-1-2026-2-28) by input type.csv", input_map, lambda s: s.lower()),
        (AggLanguageStat, "combined_data(2025-3-1-2026-2-28) by language.csv", language_map, lambda s: s.lower()),
        (AggOutputTypeStat, "combined_data(2025-3-1-2026-2-28) by output type.csv", output_map, lambda s: s),
    ):
        df = read_stats(filename)
        inserts_by_key: dict[object, dict] = {}
        id_field = (
            "input_type_id" if model is AggInputTypeStat else
            "language_id" if model is AggLanguageStat else
            "output_type_id"
        )
        for _, row in df.iterrows():
            key = normalize(clean_str(row[df.columns[0]]) or "")
            dim_id = key_map.get(key)
            if dim_id is None:
                continue
            payload = (
                {
                    id_field: dim_id,
                    "uploaded_count": parse_count(row[df.columns[1]]),
                    "created_count": parse_count(row[df.columns[2]]),
                    "published_count": parse_count(row[df.columns[3]]),
                    "uploaded_duration_sec": parse_hhmmss(row[df.columns[4]]),
                    "created_duration_sec": parse_hhmmss(row[df.columns[5]]),
                    "published_duration_sec": parse_hhmmss(row[df.columns[6]]),
                }
            )
            add_counts(inserts_by_key, dim_id, payload)
        if inserts_by_key:
            await session.execute(model.__table__.insert(), list(inserts_by_key.values()))

    publishing_df = read_stats("channel-wise-publishing.csv")
    publishing_inserts: dict[int, dict] = {}
    for _, row in publishing_df.iterrows():
        channel_id = channel_map.get(clean_str(row[publishing_df.columns[0]]) or "")
        if channel_id is None:
            continue
        payload = (
            {
                "channel_id": channel_id,
                "facebook": parse_count(row.get("Facebook", 0)),
                "instagram": parse_count(row.get("Instagram", 0)),
                "linkedin": parse_count(row.get("Linkedin", 0)),
                "reels": parse_count(row.get("Reels", 0)),
                "shorts": parse_count(row.get("Shorts", 0)),
                "x": parse_count(row.get("X", 0)),
                "youtube": parse_count(row.get("Youtube", 0)),
                "threads": parse_count(row.get("Threads", 0)),
            }
        )
        add_counts(publishing_inserts, channel_id, payload)
    if publishing_inserts:
        await session.execute(AggChannelPublishing.__table__.insert(), list(publishing_inserts.values()))

    publishing_duration_df = read_stats("channel-wise-publishing duration.csv")
    duration_inserts: dict[int, dict] = {}
    for _, row in publishing_duration_df.iterrows():
        channel_id = channel_map.get(clean_str(row[publishing_duration_df.columns[0]]) or "")
        if channel_id is None:
            continue
        payload = (
            {
                "channel_id": channel_id,
                "facebook_duration_sec": parse_hhmmss(row.get("Facebook Duration")),
                "instagram_duration_sec": parse_hhmmss(row.get("Instagram Duration")),
                "linkedin_duration_sec": parse_hhmmss(row.get("Linkedin Duration")),
                "reels_duration_sec": parse_hhmmss(row.get("Reels Duration")),
                "shorts_duration_sec": parse_hhmmss(row.get("Shorts Duration")),
                "x_duration_sec": parse_hhmmss(row.get("X Duration")),
                "youtube_duration_sec": parse_hhmmss(row.get("Youtube Duration")),
                "threads_duration_sec": parse_hhmmss(row.get("Threads Duration")),
            }
        )
        add_counts(duration_inserts, channel_id, payload)
    if duration_inserts:
        await session.execute(AggChannelPublishingDuration.__table__.insert(), list(duration_inserts.values()))

    await session.commit()
    print("  Aggregate tables loaded.")


async def main(csv_dir: Path) -> None:
    print("=" * 60)
    print(" Frammer Data Ingestion")
    print(f" CSV Directory: {csv_dir}")
    print("=" * 60)
    if not csv_dir.exists():
        print(f"ERROR: CSV directory not found: {csv_dir}")
        sys.exit(1)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        await seed_dimensions(session, csv_dir)
        await reset_analytics_tables(session)
        await load_fact_video(session, csv_dir)
        await enrich_channel_durations(session, csv_dir)
        await enrich_monthly_timestamps(session, csv_dir)
        await enrich_language_distribution(session, csv_dir)
        await load_output_types_bridge(session, csv_dir)
        await synthesize_pipeline_timestamps(session, csv_dir)
        await materialize_semantic_fields(session)
        await load_aggregate_tables(session, csv_dir)

    print("=" * 60)
    print(" Ingestion complete")
    print("=" * 60)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(Path(args.csv_dir).expanduser().resolve()))
