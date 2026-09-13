from __future__ import annotations

import discord


def has_any_role(member: discord.Member, allowed_role_ids: set[int]) -> bool:
    if not allowed_role_ids:
        return True
    return any(role.id in allowed_role_ids for role in member.roles)


def is_staff_member(member: discord.Member, staff_role_ids: set[int], admin_role_ids: set[int]) -> bool:
    if member.guild_permissions.administrator:
        return True
    allowed = staff_role_ids | admin_role_ids
    return has_any_role(member, allowed)
