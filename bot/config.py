from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_role_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    role_ids: set[int] = set()
    for value in raw.split(","):
        stripped = value.strip()
        if not stripped:
            continue
        role_ids.add(int(stripped))
    return role_ids


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return int(value)


def _parse_thresholds(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    thresholds: set[int] = set()
    for value in raw.split(","):
        stripped = value.strip()
        if not stripped:
            continue
        threshold = int(stripped)
        if threshold > 0:
            thresholds.add(threshold)
    return tuple(sorted(thresholds))


def _parse_channel_ids(raw: str | None) -> set[int]:
    return _parse_role_ids(raw)


def _deserialize_int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {_parse_int(value) for value in raw.split(",") if value.strip()}


def _parse_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    return int(raw)


def add_role_id(current: set[int], role_id: int) -> set[int]:
    return set(current) | {role_id}


def remove_role_id(current: set[int], role_id: int) -> set[int]:
    return {existing_id for existing_id in current if existing_id != role_id}


@dataclass(slots=True, frozen=True)
class Settings:
    discord_token: str
    guild_id: int | None
    database_path: Path
    staff_role_ids: set[int]
    admin_role_ids: set[int]
    referral_allowed_role_ids: set[int]
    team_creation_role_id: int | None
    team_setup_channel_id: int | None
    booster_completion_channel_id: int | None
    booster_completion_thresholds: tuple[int, ...]
    command_only_channel_ids: set[int]
    booster_role_id: int | None
    verification_role_id: int | None
    verify_channel_id: int | None
    booster_leaderboard_channel_id: int | None
    team_leaderboard_channel_id: int | None
    support_ticket_category_id: int | None
    payout_ticket_category_id: int | None
    application_ticket_category_id: int | None
    application_review_channel_id: int | None
    staff_audit_log_channel_id: int | None
    referral_log_channel_id: int | None
    expired_order_action_channel_id: int | None
    welcome_channel_id: int | None = None
    booster_approval_role_id: int | None = None
    booster_verify_channel_id: int | None = None


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    guild_id_raw = os.getenv("GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw else None

    db_path = Path(os.getenv("DATABASE_PATH", "./data/valorant_bot.db")).resolve()

    return Settings(
        discord_token=token,
        guild_id=guild_id,
        database_path=db_path,
        staff_role_ids=_parse_role_ids(os.getenv("STAFF_ROLE_IDS")),
        admin_role_ids=_parse_role_ids(os.getenv("ADMIN_ROLE_IDS")),
        referral_allowed_role_ids=_parse_role_ids(os.getenv("REFERRAL_ALLOWED_ROLE_IDS")),
        team_creation_role_id=_optional_int_env("TEAM_CREATION_ROLE_ID"),
        team_setup_channel_id=_optional_int_env("TEAM_SETUP_CHANNEL_ID"),
        booster_completion_channel_id=_optional_int_env("BOOSTER_COMPLETION_CHANNEL_ID"),
        booster_completion_thresholds=_parse_thresholds(os.getenv("BOOSTER_COMPLETION_THRESHOLDS")),
        command_only_channel_ids=_parse_channel_ids(
            os.getenv("COMMAND_ONLY_CHANNEL_IDS")
            or os.getenv("LOCKED_COMMAND_CHANNEL_IDS")
        ),
        booster_role_id=_optional_int_env("BOOSTER_ROLE_ID")
        or _optional_int_env("BOOSTER_ROLE_IDS"),
        booster_approval_role_id=_optional_int_env("BOOSTER_APPROVAL_ROLE_ID")
        or _optional_int_env("PENDING_BOOSTER_ROLE_ID")
        or _optional_int_env("APPROVAL_ROLE_ID"),
        verification_role_id=_optional_int_env("VERIFICATION_ROLE_ID")
        or _optional_int_env("VERIFY_ROLE_ID")
        or _optional_int_env("PENDING_VERIFICATION_ROLE_ID"),
        verify_channel_id=_optional_int_env("VERIFY_CHANNEL_ID")
        or _optional_int_env("VERIFICATION_CHANNEL_ID"),
        booster_verify_channel_id=_optional_int_env("BOOSTER_VERIFY_CHANNEL_ID")
        or _optional_int_env("BOOSTER_VERIFICATION_CHANNEL_ID"),
        welcome_channel_id=_optional_int_env("WELCOME_CHANNEL_ID")
        or _optional_int_env("WELCOME_CHANNEL"),
        booster_leaderboard_channel_id=_optional_int_env("BOOSTER_LEADERBOARD_CHANNEL_ID"),
        team_leaderboard_channel_id=_optional_int_env("TEAM_LEADERBOARD_CHANNEL_ID"),
        support_ticket_category_id=_optional_int_env("SUPPORT_TICKET_CATEGORY_ID"),
        payout_ticket_category_id=_optional_int_env("PAYOUT_TICKET_CATEGORY_ID"),
        application_ticket_category_id=_optional_int_env("APPLICATION_TICKET_CATEGORY_ID"),
        application_review_channel_id=_optional_int_env("APPLICATION_REVIEW_CHANNEL_ID"),
        staff_audit_log_channel_id=_optional_int_env("STAFF_AUDIT_LOG_CHANNEL_ID"),
        referral_log_channel_id=_optional_int_env("REFERRAL_LOG_CHANNEL_ID"),
        expired_order_action_channel_id=_optional_int_env("EXPIRED_ORDER_ACTION_CHANNEL_ID"),
    )


async def apply_persistent_settings(db, settings: Settings) -> Settings:
    rows = await db.fetchall("SELECT name, value FROM bot_settings")
    if not rows:
        return settings

    overrides: dict[str, object] = {}
    for row in rows:
        name = row["name"]
        value = row["value"]
        if name == "staff_role_ids":
            overrides[name] = _deserialize_int_set(value)
        elif name == "admin_role_ids":
            overrides[name] = _deserialize_int_set(value)
        elif name == "referral_allowed_role_ids":
            overrides[name] = _deserialize_int_set(value)
        elif name == "team_creation_role_id":
            overrides[name] = None if value == "" else int(value)
        elif name == "team_setup_channel_id":
            overrides[name] = None if value == "" else int(value)
        elif name == "booster_completion_thresholds":
            overrides[name] = tuple(int(part) for part in value.split(",") if part.strip())
        elif name in {"command_only_channel_ids"}:
            overrides[name] = _deserialize_int_set(value)
        elif name in {
            "booster_completion_channel_id",
            "booster_role_id",
            "booster_approval_role_id",
            "verification_role_id",
            "verify_channel_id",
            "booster_verify_channel_id",
            "welcome_channel_id",
            "booster_leaderboard_channel_id",
            "team_leaderboard_channel_id",
            "support_ticket_category_id",
            "payout_ticket_category_id",
            "application_ticket_category_id",
            "application_review_channel_id",
            "staff_audit_log_channel_id",
            "referral_log_channel_id",
            "expired_order_action_channel_id",
            "guild_id",
        }:
            overrides[name] = None if value == "" else int(value)
        elif name == "database_path":
            overrides[name] = Path(value)
        elif name == "discord_token":
            overrides[name] = value

    merged = {
        "discord_token": settings.discord_token,
        "guild_id": settings.guild_id,
        "database_path": settings.database_path,
        "staff_role_ids": settings.staff_role_ids,
        "admin_role_ids": settings.admin_role_ids,
        "referral_allowed_role_ids": settings.referral_allowed_role_ids,
        "team_creation_role_id": settings.team_creation_role_id,
        "team_setup_channel_id": settings.team_setup_channel_id,
        "booster_completion_channel_id": settings.booster_completion_channel_id,
        "booster_completion_thresholds": settings.booster_completion_thresholds,
        "command_only_channel_ids": settings.command_only_channel_ids,
        "booster_role_id": settings.booster_role_id,
        "booster_approval_role_id": settings.booster_approval_role_id,
        "verification_role_id": settings.verification_role_id,
        "verify_channel_id": settings.verify_channel_id,
        "booster_verify_channel_id": settings.booster_verify_channel_id,
        "welcome_channel_id": settings.welcome_channel_id,
        "booster_leaderboard_channel_id": settings.booster_leaderboard_channel_id,
        "team_leaderboard_channel_id": settings.team_leaderboard_channel_id,
        "support_ticket_category_id": settings.support_ticket_category_id,
        "payout_ticket_category_id": settings.payout_ticket_category_id,
        "application_ticket_category_id": settings.application_ticket_category_id,
        "application_review_channel_id": settings.application_review_channel_id,
        "staff_audit_log_channel_id": settings.staff_audit_log_channel_id,
        "referral_log_channel_id": settings.referral_log_channel_id,
        "expired_order_action_channel_id": settings.expired_order_action_channel_id,
    }
    for key, value in overrides.items():
        if key in merged:
            merged[key] = value
    return Settings(**merged)


async def persist_settings(db, settings: Settings) -> None:
    values = {
        "discord_token": settings.discord_token,
        "guild_id": "" if settings.guild_id is None else str(settings.guild_id),
        "database_path": str(settings.database_path),
        "staff_role_ids": ",".join(str(role_id) for role_id in sorted(settings.staff_role_ids)),
        "admin_role_ids": ",".join(str(role_id) for role_id in sorted(settings.admin_role_ids)),
        "referral_allowed_role_ids": ",".join(str(role_id) for role_id in sorted(settings.referral_allowed_role_ids)),
        "team_creation_role_id": "" if settings.team_creation_role_id is None else str(settings.team_creation_role_id),
        "team_setup_channel_id": "" if settings.team_setup_channel_id is None else str(settings.team_setup_channel_id),
        "booster_completion_channel_id": "" if settings.booster_completion_channel_id is None else str(settings.booster_completion_channel_id),
        "booster_completion_thresholds": ",".join(str(value) for value in settings.booster_completion_thresholds),
        "command_only_channel_ids": ",".join(str(role_id) for role_id in sorted(settings.command_only_channel_ids)),
        "booster_role_id": "" if settings.booster_role_id is None else str(settings.booster_role_id),
        "booster_approval_role_id": "" if settings.booster_approval_role_id is None else str(settings.booster_approval_role_id),
        "verification_role_id": "" if settings.verification_role_id is None else str(settings.verification_role_id),
        "verify_channel_id": "" if settings.verify_channel_id is None else str(settings.verify_channel_id),
        "booster_verify_channel_id": "" if settings.booster_verify_channel_id is None else str(settings.booster_verify_channel_id),
        "welcome_channel_id": "" if settings.welcome_channel_id is None else str(settings.welcome_channel_id),
        "booster_leaderboard_channel_id": "" if settings.booster_leaderboard_channel_id is None else str(settings.booster_leaderboard_channel_id),
        "team_leaderboard_channel_id": "" if settings.team_leaderboard_channel_id is None else str(settings.team_leaderboard_channel_id),
        "support_ticket_category_id": "" if settings.support_ticket_category_id is None else str(settings.support_ticket_category_id),
        "payout_ticket_category_id": "" if settings.payout_ticket_category_id is None else str(settings.payout_ticket_category_id),
        "application_ticket_category_id": "" if settings.application_ticket_category_id is None else str(settings.application_ticket_category_id),
        "application_review_channel_id": "" if settings.application_review_channel_id is None else str(settings.application_review_channel_id),
        "staff_audit_log_channel_id": "" if settings.staff_audit_log_channel_id is None else str(settings.staff_audit_log_channel_id),
        "referral_log_channel_id": "" if settings.referral_log_channel_id is None else str(settings.referral_log_channel_id),
        "expired_order_action_channel_id": "" if settings.expired_order_action_channel_id is None else str(settings.expired_order_action_channel_id),
    }
    for name, value in values.items():
        await db.execute(
            """
            INSERT INTO bot_settings (name, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (name, value),
        )
