from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands

from bot.config import Settings, apply_persistent_settings, load_settings
from bot.db import DatabaseManager


async def _refresh_leaderboards(bot: commands.Bot) -> None:
    if not bot.is_ready():
        return
    guild = bot.get_guild(bot.settings.guild_id) if bot.settings.guild_id else None
    if guild is None:
        return
    booster_cog = bot.get_cog("BoosterCog")
    if booster_cog is None:
        return
    if bot.settings.booster_leaderboard_channel_id:
        channel = bot.get_channel(bot.settings.booster_leaderboard_channel_id)
        if isinstance(channel, discord.TextChannel):
            embed = await booster_cog.build_booster_leaderboard_embed(page=1, page_size=10)
            messages = [msg async for msg in channel.history(limit=10)]
            if messages:
                await messages[0].edit(embed=embed)
            else:
                await channel.send(embed=embed)
    if bot.settings.team_leaderboard_channel_id:
        channel = bot.get_channel(bot.settings.team_leaderboard_channel_id)
        if isinstance(channel, discord.TextChannel):
            embed = await booster_cog.build_team_leaderboard_embed()
            messages = [msg async for msg in channel.history(limit=10)]
            if messages:
                await messages[0].edit(embed=embed)
            else:
                await channel.send(embed=embed)


class ValorantBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.logger = logging.getLogger("valorant-bot")
        self._welcome_message_recent_ids: dict[int, datetime] = {}
        self._background_loops_started = False
        self.db = DatabaseManager(
            database_path=self.settings.database_path,
            schema_path=Path(__file__).resolve().parent / "db" / "schema.sql",
            migrations_path=Path(__file__).resolve().parent / "db" / "migrations",
        )

    async def setup_hook(self) -> None:
        await self.db.initialize()
        self.settings = await apply_persistent_settings(self.db, self.settings)
        await self._load_all_cogs()
        await self._sync_app_commands()

    async def _load_all_cogs(self) -> None:
        cogs_dir = Path(__file__).resolve().parent / "cogs"
        for cog_path in sorted(cogs_dir.glob("*.py")):
            if cog_path.name.startswith("_") or cog_path.stem == "__init__":
                continue
            extension = f"bot.cogs.{cog_path.stem}"
            await self.load_extension(extension)
            self.logger.info("Loaded cog: %s", extension)

    async def _sync_app_commands(self) -> None:
        if self.settings.guild_id is not None:
            guild_obj = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            self.logger.info("Synced %s guild slash command(s).", len(synced))
            return
        synced = await self.tree.sync()
        self.logger.info("Synced %s global slash command(s).", len(synced))

    async def on_ready(self) -> None:
        if self._background_loops_started:
            return
        self._background_loops_started = True
        self.logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "n/a")
        self.loop.create_task(self._leaderboard_loop())
        self.loop.create_task(self._order_deadline_loop())

    async def _get_staff_audit_channel(self) -> discord.TextChannel | None:
        if self.settings.staff_audit_log_channel_id is None:
            return None
        channel = self.get_channel(self.settings.staff_audit_log_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        if self.settings.welcome_channel_id is None:
            return

        now = datetime.now(timezone.utc)
        last_seen = self._welcome_message_recent_ids.get(member.id)
        if last_seen is not None and now - last_seen < timedelta(seconds=30):
            return

        channel = self.get_channel(self.settings.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        self._welcome_message_recent_ids[member.id] = now
        embed = discord.Embed(
            title="👋 Welcome",
            description=f"Welcome {member.mention} to {member.guild.name}!\n\nPlease read the rules and verify yourself in the verification channel.",
            color=0x22C55E,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{len(member.guild.members)}")
        await channel.send(embed=embed)

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        channel = await self._get_staff_audit_channel()
        if channel is None:
            return

        event_name = "Left"
        actor = None
        reason = "No reason provided"
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)

        guild = member.guild
        audit_logs = [
            entry async for entry in guild.audit_logs(limit=20)
            if entry.target and getattr(entry.target, "id", None) == member.id and entry.created_at >= recent_cutoff
        ]

        for entry in audit_logs:
            if entry.action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                actor = entry.user
                reason = entry.reason or "No reason provided"
                event_name = "Kicked" if entry.action == discord.AuditLogAction.kick else "Banned"
                break

        if not actor and not audit_logs:
            event_name = "Left"

        embed = discord.Embed(
            title=f"📜 Member {event_name}",
            description=f"{member.mention} ({member.name}) has {event_name.lower()} the server.",
            color=0xF59E0B if event_name == "Left" else 0xEF4444,
        )
        embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=True)
        embed.add_field(name="Event", value=event_name, inline=True)
        if actor is not None:
            embed.add_field(name="Moderator", value=f"{actor.mention} ({actor.id})", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

    async def _leaderboard_loop(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(60)
            try:
                await _refresh_leaderboards(self)
            except Exception:
                self.logger.exception("Leaderboard refresh failed.")

    async def _order_deadline_loop(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(60)
            try:
                orders_cog = self.get_cog("OrdersCog")
                if orders_cog is not None:
                    await orders_cog._check_claimed_order_deadlines()
            except Exception:
                self.logger.exception("Claimed order deadline check failed.")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id not in self.settings.command_only_channel_ids:
            return
        if message.content.startswith("/"):
            return
        if message.content.startswith("!"):
            return
        await message.delete()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        self.logger.exception("Slash command error: %s", error)
        message = "❌ An error occurred while executing this command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )


async def run_bot() -> None:
    configure_logging()
    settings = load_settings()
    bot = ValorantBot(settings)
    async with bot:
        await bot.start(settings.discord_token)
    # ensure DB connection is closed on shutdown
    try:
        await bot.db.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(run_bot())
