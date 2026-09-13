from __future__ import annotations

import discord

from bot.db import DatabaseManager


def _derive_role_type(
    member: discord.Member,
    staff_role_ids: set[int],
    admin_role_ids: set[int],
    booster_role_id: int | None = None,
) -> str:
    guild_permissions = getattr(member, "guild_permissions", None)
    roles = getattr(member, "roles", [])

    if getattr(guild_permissions, "administrator", False) or any(getattr(role, "id", None) in admin_role_ids for role in roles):
        return "admin"
    if any(getattr(role, "id", None) in staff_role_ids for role in roles):
        return "staff"
    if booster_role_id is not None and any(getattr(role, "id", None) == booster_role_id for role in roles):
        return "booster"
    return "booster"


async def resolve_or_create_user(
    db: DatabaseManager,
    member: discord.Member,
    staff_role_ids: set[int],
    admin_role_ids: set[int],
    booster_role_id: int | None = None,
) -> int:
    role_type = _derive_role_type(member, staff_role_ids, admin_role_ids, booster_role_id)
    existing = await db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
    if existing is not None:
        await db.execute(
            """
            UPDATE users
            SET username = ?, role_type = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(member), role_type, existing["id"]),
        )
        return int(existing["id"])

    return await db.execute(
        """
        INSERT INTO users (discord_id, username, role_type)
        VALUES (?, ?, ?)
        """,
        (member.id, str(member), role_type),
    )
