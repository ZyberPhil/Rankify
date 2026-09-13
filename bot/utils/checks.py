from __future__ import annotations

import discord
from discord import app_commands


def guild_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member):
            return True
        raise app_commands.CheckFailure("This command is only available in the server.")

    return app_commands.check(predicate)


def command_channel_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.channel, discord.TextChannel):
            raise app_commands.CheckFailure("This command is only available in text channels.")
        settings = getattr(interaction.client, "settings", None)
        if settings is None:
            raise app_commands.CheckFailure("Bot settings missing.")
        if not settings.command_only_channel_ids:
            return True
        if interaction.channel.id in settings.command_only_channel_ids:
            return True
        raise app_commands.CheckFailure("Only commands and bot messages are allowed in this channel.")

    return app_commands.check(predicate)


def verified_booster_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        settings = getattr(interaction.client, "settings", None)
        if settings is None:
            raise app_commands.CheckFailure("Bot settings missing.")

        if member.guild_permissions.administrator:
            return True
        if settings.booster_role_id and any(role.id == settings.booster_role_id for role in member.roles):
            user_row = await interaction.client.db.fetchone(
                "SELECT verified_booster, rules_confirmed_at FROM users WHERE discord_id = ?",
                (member.id,),
            )
            if user_row and int(user_row["verified_booster"]) == 1 and user_row["rules_confirmed_at"]:
                return True
            raise app_commands.CheckFailure(
                "You must confirm the rules before using booster commands."
            )
        raise app_commands.CheckFailure("This area is only for verified boosters.")

    return app_commands.check(predicate)


def staff_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        if member.guild_permissions.administrator:
            return True
        settings = getattr(interaction.client, "settings", None)
        if settings is None:
            raise app_commands.CheckFailure("Bot settings missing.")
        allowed = settings.staff_role_ids | settings.admin_role_ids
        if any(role.id in allowed for role in member.roles):
            return True
        raise app_commands.CheckFailure("You do not have permission for this.")

    return app_commands.check(predicate)


def admin_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        if member.guild_permissions.administrator:
            return True
        settings = getattr(interaction.client, "settings", None)
        if settings is None:
            raise app_commands.CheckFailure("Bot settings missing.")
        if any(role.id in settings.admin_role_ids for role in member.roles):
            return True
        raise app_commands.CheckFailure("This command is only for admins.")

    return app_commands.check(predicate)
