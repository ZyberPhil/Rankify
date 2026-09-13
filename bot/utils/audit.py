from __future__ import annotations

from collections.abc import Sequence

import discord
from discord.ext import commands


async def send_staff_audit_log(
    bot: commands.Bot,
    title: str,
    description: str,
    fields: Sequence[tuple[str, str, bool]] = (),
) -> None:
    channel_id = getattr(bot.settings, "staff_audit_log_channel_id", None)
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(title=title, description=description, color=0xF59E0B)
    bot_user = getattr(bot, "user", None)
    avatar_url = getattr(getattr(bot_user, "display_avatar", None), "url", None)
    if avatar_url is not None:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="ValorantDeren", icon_url=avatar_url)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    await channel.send(embed=embed)


async def send_referral_audit_log(
    bot: commands.Bot,
    title: str,
    description: str,
    fields: Sequence[tuple[str, str, bool]] = (),
) -> None:
    channel_id = getattr(bot.settings, "referral_log_channel_id", None)
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(title=title, description=description, color=0x10B981)
    bot_user = getattr(bot, "user", None)
    avatar_url = getattr(getattr(bot_user, "display_avatar", None), "url", None)
    if avatar_url is not None:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="ValorantDeren", icon_url=avatar_url)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    await channel.send(embed=embed)


async def send_booster_milestone_announcement(
    bot: commands.Bot,
    user_name: str,
    completed_count: int,
    reached_thresholds: Sequence[int],
) -> None:
    channel_id = getattr(bot.settings, "booster_completion_channel_id", None)
    if channel_id is None or not reached_thresholds:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(
        title="🏆 Booster milestone reached",
        description=f"{user_name} now has {completed_count} completed orders.",
        color=0x7C3AED,
    )
    bot_user = getattr(bot, "user", None)
    avatar_url = getattr(getattr(bot_user, "display_avatar", None), "url", None)
    if avatar_url is not None:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="ValorantDeren", icon_url=avatar_url)
    embed.add_field(
        name="Reached thresholds",
        value=", ".join(f"{threshold} orders" for threshold in reached_thresholds),
        inline=False,
    )
    await channel.send(embed=embed)
