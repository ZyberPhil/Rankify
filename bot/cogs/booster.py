from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import apply_persistent_settings
from bot.services import resolve_or_create_user
from bot.utils.checks import staff_only, verified_booster_only
from bot.utils.formatting import create_lawliet_embed, money_cents_to_eur, THEME_COLORS
from bot.utils.ids import generate_referral_code, normalize_team_prefix


async def safe_booster_response(interaction: discord.Interaction, message: str | None = None, *, ephemeral: bool = True, **kwargs) -> None:
    try:
        response = getattr(interaction, "response", None)
        if response is not None and getattr(response, "is_done", lambda: False)():
            if hasattr(interaction, "followup"):
                await interaction.followup.send(message, ephemeral=ephemeral, **kwargs)
            return
        if message is None:
            await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral, **kwargs)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        pass


async def safe_booster_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
    try:
        response = getattr(interaction, "response", None)
        if response is not None and getattr(response, "is_done", lambda: False)():
            return
        await interaction.response.send_modal(modal)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        pass


class VerifyMemberRoleView(discord.ui.View):
    def __init__(self, cog: "BoosterCog") -> None:
        super().__init__(timeout=60 * 60)
        self.cog = cog

    @discord.ui.button(
        label="Get member role",
        style=discord.ButtonStyle.primary,
        custom_id="member_role:claim",
        emoji="✅",
    )
    async def claim_member_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            await safe_booster_response(interaction, "❌ This action is only available in the server.", ephemeral=True)
            return
        if self.cog.settings.verification_role_id is None:
            await safe_booster_response(interaction, "❌ The member role is not configured yet.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await safe_booster_response(interaction, "❌ This command is only available in the server.", ephemeral=True)
            return
        role = guild.get_role(self.cog.settings.verification_role_id)
        if role is None:
            await safe_booster_response(interaction, "❌ The configured member role could not be found.", ephemeral=True)
            return
        if role in member.roles:
            await safe_booster_response(interaction, f"✅ You already have the {role.mention} role.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Member verification dashboard")
        except discord.Forbidden:
            await safe_booster_response(
                interaction,
                "❌ I could not assign the role. Please make sure the bot role is above the member role and has Manage Roles permission.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await safe_booster_response(
                interaction,
                "❌ The member role could not be assigned right now. Please try again in a moment.",
                ephemeral=True,
            )
            return
        await safe_booster_response(interaction, f"✅ You received the {role.mention} role.", ephemeral=True)


class VerifyRulesView(discord.ui.View):
    def __init__(self, cog: "BoosterCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Accept booster access",
        style=discord.ButtonStyle.success,
        custom_id="booster_access:accept",
        emoji="✅",
    )
    async def confirm_rules(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            await safe_booster_response(interaction, "❌ This action is only available in the server.", ephemeral=True)
            return
        _, message = await self.cog.confirm_booster_rules(member)
        await safe_booster_response(interaction, message, ephemeral=True)


class TeamSetupView(discord.ui.View):
    def __init__(self, cog: "BoosterCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Create team", custom_id="team_setup_create", style=discord.ButtonStyle.success, emoji="✅")
    async def create_team(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_team_setup(interaction, action="create")

    @discord.ui.button(label="Kick member", custom_id="team_setup_kick", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def kick_member(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_team_setup(interaction, action="kick")

    @discord.ui.button(label="Team overview", custom_id="team_setup_overview", style=discord.ButtonStyle.secondary, emoji="👥")
    async def team_overview(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_team_setup(interaction, action="overview")

    @discord.ui.button(label="Create referral", custom_id="team_setup_referral", style=discord.ButtonStyle.secondary, emoji="🔗")
    async def create_referral(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_team_setup(interaction, action="referral")

    @discord.ui.button(label="Leave team", custom_id="team_setup_leave", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave_team(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_team_setup(interaction, action="leave")


class TeamLeaveConfirmationView(discord.ui.View):
    def __init__(self, cog: "BoosterCog", team: dict) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.team = team

    @discord.ui.button(label="Confirm leave", custom_id="team_leave_confirm", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user
        user_row = await self.cog.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (int(getattr(member, "id", 0)),))
        owner_user_id = int(self.team["created_by_user_id"])
        if user_row is None or int(user_row["id"]) != owner_user_id:
            await safe_booster_response(interaction, "❌ This confirmation is only valid for the team owner.", ephemeral=True)
            return
        await self.cog._leave_team(member, self.team, confirmed=True)
        await safe_booster_response(interaction, "✅ You left the team and the team was deleted.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", custom_id="team_leave_cancel", style=discord.ButtonStyle.secondary)
    async def cancel_leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_booster_response(interaction, "✅ Leave cancelled.", ephemeral=True)
        self.stop()


class TeamCreateModal(discord.ui.Modal, title="Create team"):
    team_name = discord.ui.TextInput(label="Team name", max_length=32)
    prefix = discord.ui.TextInput(label="Team prefix (max 3 alphanumeric)", max_length=3)

    def __init__(self, cog: "BoosterCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        team_name = str(self.team_name).strip()
        prefix = normalize_team_prefix(str(self.prefix))
        if not team_name or not prefix:
            await interaction.response.send_message("❌ Team name and prefix are required.", ephemeral=True)
            return
        user_row = await self.cog.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        user_id = user_row["id"] if user_row else await resolve_or_create_user(
            self.cog.bot.db, member, self.cog.settings.staff_role_ids, self.cog.settings.admin_role_ids
        )
        existing_membership = await self.cog.bot.db.fetchone(
            "SELECT team_id FROM team_members WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        if existing_membership is not None:
            await interaction.response.send_message(
                "❌ You are already in a team. You cannot create a second team.",
                ephemeral=True,
            )
            return

        team_id = await self.cog.bot.db.execute(
            "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
            (team_name, prefix, generate_referral_code(8), user_id),
        )
        await self.cog.bot.db.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
            (team_id, user_id),
        )
        team_data = {"id": team_id, "name": team_name, "prefix": prefix, "referral_code": None, "created_by_user_id": user_id}
        await self.cog._sync_member_nickname_with_team(member, team_data)
        await interaction.response.send_message(
            f"✅ Team **{team_name}** created. Prefix: `{prefix}`",
            ephemeral=True,
        )


class TeamInviteModal(discord.ui.Modal, title="Invite team member"):
    member = discord.ui.TextInput(label="Member name or mention", max_length=80)

    def __init__(self, cog: "BoosterCog", team_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        value = str(self.member).strip()
        candidate = None
        if value.startswith("<@") and value.endswith(">"):
            user_id = int(value[2:-1])
            candidate = interaction.guild.get_member(user_id) if interaction.guild else None
        else:
            candidate = interaction.guild.get_member_named(value) if interaction.guild else None
        if candidate is None:
            await interaction.response.send_message("❌ User not found in the server.", ephemeral=True)
            return
        user_row = await self.cog.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (candidate.id,))
        if user_row is None:
            user_id = await resolve_or_create_user(
                self.cog.bot.db, candidate, self.cog.settings.staff_role_ids, self.cog.settings.admin_role_ids
            )
        else:
            user_id = int(user_row["id"])
        await self.cog.bot.db.execute(
            "INSERT OR IGNORE INTO team_members (team_id, user_id) VALUES (?, ?)",
            (self.team_id, user_id),
        )
        await interaction.response.send_message(f"✅ {candidate.mention} was invited to the team.", ephemeral=True)


class TeamKickModal(discord.ui.Modal, title="Kick team member"):
    member = discord.ui.TextInput(label="Member name or mention", max_length=80)

    def __init__(self, cog: "BoosterCog", team_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")

        user_row = await self.cog.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        if user_row is None:
            await interaction.response.send_message("❌ You need a linked account to kick members.", ephemeral=True)
            return

        team_row = await self.cog.bot.db.fetchone(
            "SELECT created_by_user_id FROM teams WHERE id = ?",
            (self.team_id,),
        )
        if team_row is None:
            await interaction.response.send_message("❌ Team not found.", ephemeral=True)
            return
        if int(team_row["created_by_user_id"]) != int(user_row["id"]):
            await interaction.response.send_message("❌ Only the team owner can kick members from the team.", ephemeral=True)
            return

        value = str(self.member).strip()
        candidate = None
        if value.startswith("<@") and value.endswith(">"):
            user_id = int(value[2:-1])
            candidate = interaction.guild.get_member(user_id) if interaction.guild else None
        else:
            candidate = interaction.guild.get_member_named(value) if interaction.guild else None
        if candidate is None:
            await interaction.response.send_message("❌ User not found in the server.", ephemeral=True)
            return
        target_user_row = await self.cog.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (candidate.id,))
        if target_user_row is None:
            await interaction.response.send_message("❌ User has no linked account.", ephemeral=True)
            return
        await self.cog.bot.db.execute(
            "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
            (self.team_id, target_user_row["id"]),
        )
        await interaction.response.send_message(f"✅ {candidate.mention} was removed from the team.", ephemeral=True)


class BoosterCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self.team_setup_view = TeamSetupView(self)
        self.rules_view = VerifyRulesView(self)

    async def _refresh_runtime_settings(self) -> None:
        if not hasattr(self.bot, "db") or self.bot.db is None:
            return
        self.bot.settings = await apply_persistent_settings(self.bot.db, self.bot.settings)
        self.settings = self.bot.settings

    @staticmethod
    def format_team_label(prefix: str | None, team_name: str | None) -> str:
        normalized_prefix = str(prefix or "").strip()
        normalized_name = str(team_name or "").strip()
        if normalized_prefix and normalized_name:
            return f"[{normalized_prefix}] {normalized_name}"
        return normalized_name or normalized_prefix or "Unknown Team"

    @staticmethod
    def format_member_name_with_team_tag(member_name: str | None, prefix: str | None) -> str:
        normalized_name = str(member_name or "").strip()
        normalized_prefix = str(prefix or "").strip()
        if not normalized_name:
            return normalized_prefix or "Unknown User"
        if normalized_prefix:
            cleaned_name = re.sub(r"^\[[^\]]+\]\s*", "", normalized_name)
            return f"[{normalized_prefix}] {cleaned_name.strip()}"
        return normalized_name

    @staticmethod
    def _member_has_role_id(member: discord.Member, role_id: int | None) -> bool:
        if role_id is None:
            return False
        return any(getattr(role, "id", None) == role_id for role in getattr(member, "roles", []))

    async def confirm_booster_rules(self, member: discord.Member) -> tuple[bool, str]:
        approval_role_id = self.settings.booster_approval_role_id
        booster_role_id = self.settings.booster_role_id
        if approval_role_id is None or booster_role_id is None:
            return False, "❌ The approval role and final booster role must be configured first."
        if approval_role_id == booster_role_id:
            return False, "❌ The approval role and final booster role must be different."
        if not self._member_has_role_id(member, approval_role_id):
            return False, "❌ You need the booster approval role before accepting the rules."

        guild = getattr(member, "guild", None)
        if guild is None:
            return False, "❌ This action is only available in the server."
        final_role = guild.get_role(booster_role_id)
        approval_role = guild.get_role(approval_role_id)
        if final_role is None or approval_role is None:
            return False, "❌ A configured booster role could not be found in this server."

        try:
            if not self._member_has_role_id(member, booster_role_id):
                await member.add_roles(final_role, reason="Booster rules accepted")
            if self._member_has_role_id(member, approval_role_id):
                await member.remove_roles(approval_role, reason="Booster rules accepted")
        except discord.Forbidden:
            return False, "❌ I cannot update these roles. Check my Manage Roles permission and role position."
        except discord.HTTPException:
            return False, "❌ The roles could not be updated. Please try again."

        user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        await self.bot.db.execute(
            """
            UPDATE users
            SET verified_booster = 1,
                rules_confirmed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user_id,),
        )
        return True, "✅ Rules confirmed. You received the final booster role."

    async def _resolve_member_team(self, member: discord.Member) -> dict | None:
        user_row = await self.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        if user_row is None:
            return None
        return await self.bot.db.fetchone(
            """
            SELECT t.id, t.name, t.prefix, t.referral_code, t.created_by_user_id
            FROM teams t
            INNER JOIN team_members tm ON tm.team_id = t.id
            WHERE tm.user_id = ?
            LIMIT 1
            """,
            (int(user_row["id"]),),
        )

    async def _find_team_by_referral_code(self, referral_code: str) -> dict | None:
        code = str(referral_code or "").strip().upper()
        if not code:
            return None
        return await self.bot.db.fetchone(
            "SELECT id, name, prefix, referral_code, created_by_user_id FROM teams WHERE UPPER(referral_code) = ? LIMIT 1",
            (code,),
        )

    async def _attach_user_to_team(self, user_id: int, team_id: int) -> None:
        existing = await self.bot.db.fetchone(
            "SELECT team_id FROM team_members WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        if existing is not None:
            if int(existing["team_id"]) == team_id:
                return
            await self.bot.db.execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO team_members (team_id, user_id) VALUES (?, ?)",
            (team_id, user_id),
        )

    async def _leave_team(self, member: discord.Member | object, team: dict | None, *, confirmed: bool = False) -> None:
        if team is None:
            return
        member_id = getattr(member, "id", None)
        if member_id is None:
            return
        user_row = await self.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member_id,))
        if user_row is None:
            return
        user_id = int(user_row["id"])
        team_id = int(team["id"])
        created_by_user_id = team["created_by_user_id"]
        is_owner = int(created_by_user_id) == user_id if created_by_user_id is not None else False

        if is_owner and confirmed:
            await self.bot.db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
            return

        await self.bot.db.execute(
            "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user_id),
        )

    async def _sync_member_nickname_with_team(self, member: discord.Member, team: dict | None) -> None:
        if member is None or team is None:
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        prefix = str(team.get("prefix") if isinstance(team, dict) else team["prefix"] if team else "" or "").strip()
        if not prefix:
            return
        current_nickname = getattr(member, "display_name", None) or getattr(member, "name", None) or ""
        desired_nickname = self.format_member_name_with_team_tag(current_nickname, prefix)
        if getattr(member, "nick", None) == desired_nickname:
            return
        try:
            await member.edit(nick=desired_nickname)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _staff_manage_team(self, team_id: int, *, new_name: str | None = None, new_prefix: str | None = None, member_to_remove_id: int | None = None) -> None:
        updates: list[str] = []
        params: list[object] = []

        if new_name is not None:
            cleaned_name = new_name.strip()
            if cleaned_name:
                updates.append("name = ?")
                params.append(cleaned_name)

        if new_prefix is not None:
            cleaned_prefix = normalize_team_prefix(new_prefix)
            if cleaned_prefix:
                updates.append("prefix = ?")
                params.append(cleaned_prefix)

        if updates:
            params.append(team_id)
            await self.bot.db.execute(
                f"UPDATE teams SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                tuple(params),
            )

        if member_to_remove_id is not None:
            await self.bot.db.execute(
                "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, member_to_remove_id),
            )

    async def _list_all_teams_for_staff(self) -> list[sqlite3.Row]:
        return await self.bot.db.fetchall(
            """
            SELECT t.id, t.name, t.prefix, COUNT(tm.user_id) AS member_count,
                   COALESCE(SUM(u.total_earned_cents), 0) AS team_earnings_cents
            FROM teams t
            LEFT JOIN team_members tm ON tm.team_id = t.id
            LEFT JOIN users u ON u.id = tm.user_id
            GROUP BY t.id, t.name, t.prefix
            ORDER BY t.name ASC
            """
        )

    async def _build_team_overview_embed(self, team: dict) -> discord.Embed:
        team_id = int(team["id"])
        members = await self.bot.db.fetchall(
            """
            SELECT u.id, u.discord_id, u.username, u.role_type
            FROM team_members tm
            INNER JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ?
            ORDER BY u.username ASC
            """,
            (team_id,),
        )

        member_lines = []
        created_by_user_id = team["created_by_user_id"]
        owner_user_id = int(created_by_user_id) if created_by_user_id is not None else None
        team_prefix = team["prefix"] if isinstance(team, dict) else None
        for member_row in members:
            discord_id = int(member_row["discord_id"])
            username = str(member_row["username"]) or f"User {discord_id}"
            user_id = int(member_row["id"])
            display_name = self.format_member_name_with_team_tag(username, team_prefix)
            owner_marker = " 👑" if owner_user_id is not None and user_id == owner_user_id else ""
            member_lines.append(f"• {display_name}{owner_marker}")
        if not member_lines:
            member_lines = ["• No members yet."]

        label = self.format_team_label(team["prefix"], team["name"])
        referral_code = str(team["referral_code"]) if team["referral_code"] else "Not generated yet"
        embed = create_lawliet_embed(
            self.bot,
            title="👥 Team Overview",
            description=f"{label}\nReferral: `{referral_code}`",
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="Members", value="\n".join(member_lines[:12]), inline=False)
        embed.add_field(name="Prefix", value=str(team["prefix"] or "-"), inline=True)
        embed.add_field(name="Member count", value=str(len(members)), inline=True)
        return embed

    async def cog_load(self) -> None:
        self.bot.add_view(self.team_setup_view)
        self.bot.add_view(self.rules_view)
        await self._refresh_runtime_settings()
        await self._ensure_team_setup_message()
        await self._ensure_public_dashboards()

    async def _collect_recent_channel_messages(self, channel: discord.TextChannel) -> list:
        history = getattr(channel, "history", None)
        if history is None:
            return []
        try:
            result = history(limit=20)
        except TypeError:
            result = history
        if hasattr(result, "__await__"):
            result = await result
        if hasattr(result, "__aiter__"):
            return [msg async for msg in result]
        if isinstance(result, list):
            return result
        try:
            return list(result)
        except TypeError:
            return []

    async def _ensure_team_setup_message(self) -> None:
        await self._refresh_runtime_settings()
        if not self.settings.team_setup_channel_id:
            return
        channel = self.bot.get_channel(self.settings.team_setup_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        messages = await self._collect_recent_channel_messages(channel)
        for msg in messages:
            if msg.author.id == self.bot.user.id and msg.embeds and msg.embeds[0].title == "🎭 Team Control Panel":
                return
        embed = create_lawliet_embed(
            self.bot,
            title="🎭 Team Control Panel",
            description="Manage your team, members, and referral code.",
            color=THEME_COLORS["primary"],
        )
        await channel.send(embed=embed, view=self.team_setup_view)

    async def _ensure_public_dashboards(self) -> None:
        await self._refresh_runtime_settings()

        if self.settings.verification_role_id is not None and self.settings.verify_channel_id is not None:
            channel = self.bot.get_channel(self.settings.verify_channel_id)
            if isinstance(channel, discord.TextChannel):
                messages = await self._collect_recent_channel_messages(channel)
                has_dashboard = any(
                    msg.author.id == self.bot.user.id and msg.embeds and msg.embeds[0].title == "✅ Verification Dashboard"
                    for msg in messages
                )
                if not has_dashboard:
                    await self._post_public_verify_dashboard()

        if self.settings.booster_verify_channel_id is not None:
            channel = self.bot.get_channel(self.settings.booster_verify_channel_id)
            if isinstance(channel, discord.TextChannel):
                messages = await self._collect_recent_channel_messages(channel)
                has_dashboard = any(
                    msg.author.id == self.bot.user.id and msg.embeds and msg.embeds[0].title == "✅ Booster Access Verification"
                    for msg in messages
                )
                if not has_dashboard:
                    await self._post_public_rules_dashboard()

    async def _resolve_user_stats(self, member: discord.Member) -> dict[str, int | str | None]:
        user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        row = await self.bot.db.fetchone(
            """
            SELECT booster_level, balance_cents, completed_orders_count, total_earned_cents,
                   COALESCE((SELECT SUM(total_bonus_cents) FROM referrals WHERE referrer_user_id = users.id), 0) AS referral_earned_cents,
                   boosted_since, verified_booster, rules_confirmed_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        )
        if row is None:
            raise RuntimeError("User not found.")
        return {
            "user_id": user_id,
            "booster_level": int(row["booster_level"]),
            "balance_cents": int(row["balance_cents"]),
            "completed_orders_count": int(row["completed_orders_count"]),
            "total_earned_cents": int(row["total_earned_cents"]),
            "referral_earned_cents": int(row["referral_earned_cents"]),
            "boosted_since": row["boosted_since"],
            "verified_booster": int(row["verified_booster"]),
            "rules_confirmed_at": row["rules_confirmed_at"],
        }

    async def handle_team_setup(self, interaction: discord.Interaction, action: str) -> None:
        await self._refresh_runtime_settings()
        member = interaction.user if getattr(interaction.user, "id", None) is not None else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")

        if action == "create":
            team_creation_role_id = getattr(self.settings, "team_creation_role_id", None)
            if team_creation_role_id and not any(getattr(role, "id", None) == team_creation_role_id for role in getattr(member, "roles", [])):
                await safe_booster_response(
                    interaction,
                    "❌ You must first reach the Team Leader role before you can create your own team.",
                    ephemeral=True,
                )
                return
            await safe_booster_modal(interaction, TeamCreateModal(self))
            return

        team = await self._resolve_member_team(member)
        if team is None:
            await safe_booster_response(interaction, "❌ You are not in a team yet.", ephemeral=True)
            return

        if action == "overview":
            await self._sync_member_nickname_with_team(member, team)
            embed = await self._build_team_overview_embed(team)
            await safe_booster_response(interaction, embed=embed, ephemeral=True)
            return

        if action == "leave":
            staff_role_ids = getattr(self.settings, "staff_role_ids", set())
            admin_role_ids = getattr(self.settings, "admin_role_ids", set())
            user_id = await resolve_or_create_user(
                self.bot.db, member, staff_role_ids, admin_role_ids
            )
            if int(team["created_by_user_id"]) == user_id:
                team_label = self.format_team_label(team["prefix"], team["name"])
                await safe_booster_response(
                    interaction,
                    f"⚠️ You are the owner of **{team_label}**. Leaving will delete the team. Please confirm below.",
                    ephemeral=True,
                    view=TeamLeaveConfirmationView(self, team),
                )
                return

            await self._leave_team(member, team)
            await safe_booster_response(
                interaction,
                f"✅ You left **{self.format_team_label(team['prefix'], team['name'])}**.",
                ephemeral=True,
            )
            return

        if action == "kick":
            staff_role_ids = getattr(self.settings, "staff_role_ids", set())
            admin_role_ids = getattr(self.settings, "admin_role_ids", set())
            user_id = await resolve_or_create_user(
                self.bot.db, member, staff_role_ids, admin_role_ids
            )
            if int(team["created_by_user_id"]) != user_id:
                await safe_booster_response(
                    interaction,
                    "❌ Only the team owner can kick members from the team.",
                    ephemeral=True,
                )
                return
            await safe_booster_modal(interaction, TeamKickModal(self, int(team["id"])))
        elif action == "referral":
            staff_role_ids = getattr(self.settings, "staff_role_ids", set())
            admin_role_ids = getattr(self.settings, "admin_role_ids", set())
            user_id = await resolve_or_create_user(
                self.bot.db, member, staff_role_ids, admin_role_ids
            )
            if int(team["created_by_user_id"]) != user_id:
                await safe_booster_response(
                    interaction,
                    "❌ Only the team owner can create or manage the team referral code.",
                    ephemeral=True,
                )
                return
            code = str(team["referral_code"]) if team["referral_code"] else generate_referral_code(8)
            if not team["referral_code"]:
                await self.bot.db.execute(
                    "UPDATE teams SET referral_code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (code, int(team["id"])),
                )
            await safe_booster_response(
                interaction,
                f"✅ Team referral code for **{self.format_team_label(team['prefix'], team['name'])}**: `{code}`",
                ephemeral=True,
            )

    @app_commands.command(name="setup_team", description="Ensures the permanent team management panel exists in the configured setup channel.")
    async def setup_team(self, interaction: discord.Interaction) -> None:
        await self._refresh_runtime_settings()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        if self.settings.team_creation_role_id and not any(role.id == self.settings.team_creation_role_id for role in member.roles):
            await interaction.response.send_message(
                "❌ You need the configured team creation role to manage teams.",
                ephemeral=True,
            )
            return
        if not self.settings.team_setup_channel_id:
            await interaction.response.send_message(
                "❌ No team setup channel is configured yet. Use `/set_team_setup_channel` first.",
                ephemeral=True,
            )
            return
        channel = self.bot.get_channel(self.settings.team_setup_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ The configured team setup channel is invalid or inaccessible.",
                ephemeral=True,
            )
            return

        await self._ensure_team_setup_message()
        await interaction.response.send_message(
            f"✅ The team panel is live in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="team_overview", description="Shows your current team and member list.")
    async def team_overview(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        team = await self._resolve_member_team(member)
        if team is None:
            await interaction.response.send_message("❌ You are not in a team yet.", ephemeral=True)
            return
        embed = await self._build_team_overview_embed(team)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="team_create", description="Create a team and assign a prefix.")
    async def team_create(self, interaction: discord.Interaction, team_name: str, prefix: str) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("This command is only available in the server.")
        if self.settings.team_creation_role_id and not any(role.id == self.settings.team_creation_role_id for role in member.roles):
            await interaction.response.send_message(
                "❌ You must first reach the Team Leader role before you can create your own team.",
                ephemeral=True,
            )
            return

        team_prefix = normalize_team_prefix(prefix)
        if not team_prefix:
            await interaction.response.send_message("❌ Prefix must contain at least one letter or number.", ephemeral=True)
            return
        normalized_team_name = team_name.strip()
        if not normalized_team_name:
            await interaction.response.send_message("❌ Team name cannot be empty.", ephemeral=True)
            return

        user_row = await self.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        if user_row is None:
            user_id = await resolve_or_create_user(
                self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
            )
        else:
            user_id = int(user_row["id"])

        existing_membership = await self.bot.db.fetchone(
            "SELECT team_id FROM team_members WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        if existing_membership is not None:
            await interaction.response.send_message(
                "❌ You are already in a team. Leave your current team before creating a new one.",
                ephemeral=True,
            )
            return

        team_id = await self.bot.db.execute(
            "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
            (normalized_team_name, team_prefix, generate_referral_code(8), user_id),
        )
        await self.bot.db.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
            (team_id, user_id),
        )
        team_data = {"id": team_id, "name": normalized_team_name, "prefix": team_prefix, "referral_code": None, "created_by_user_id": user_id}
        await self._sync_member_nickname_with_team(member, team_data)
        await interaction.response.send_message(
            f"✅ Team **{normalized_team_name}** created. Prefix: `{team_prefix}`",
            ephemeral=True,
        )

    @app_commands.command(name="team_invite", description="Staff: assign a member to a team using the team referral code.")
    @app_commands.describe(member="Member to add", team_referral_code="Team referral code")
    @staff_only()
    async def team_invite(self, interaction: discord.Interaction, member: discord.Member, team_referral_code: str) -> None:
        team = await self._find_team_by_referral_code(team_referral_code)
        if team is None:
            await interaction.response.send_message("❌ Team referral code not found.", ephemeral=True)
            return

        target_user_row = await self.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        if target_user_row is None:
            target_user_id = await resolve_or_create_user(
                self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
            )
        else:
            target_user_id = int(target_user_row["id"])

        await self._attach_user_to_team(target_user_id, int(team["id"]))
        await interaction.response.send_message(
            f"✅ {member.mention} was assigned to **{self.format_team_label(team['prefix'], team['name'])}**.",
            ephemeral=True,
        )

    @app_commands.command(name="team_manage", description="Staff: edit team name, prefix, or remove members.")
    @app_commands.describe(team_name="Team name to set", team_prefix="Team prefix to set", member="Member to remove from the team")
    @staff_only()
    async def team_manage(
        self,
        interaction: discord.Interaction,
        team_name: str | None = None,
        team_prefix: str | None = None,
        member: discord.Member | None = None,
    ) -> None:
        if team_name is None and team_prefix is None and member is None:
            await interaction.response.send_message(
                "❌ Provide at least one change: team name, prefix, or member to remove.",
                ephemeral=True,
            )
            return

        if member is None:
            await interaction.response.send_message(
                "❌ You must specify which member to remove from the team.",
                ephemeral=True,
            )
            return

        target_user_row = await self.bot.db.fetchone("SELECT id FROM users WHERE discord_id = ?", (member.id,))
        if target_user_row is None:
            await interaction.response.send_message("❌ Target user is not linked to the bot yet.", ephemeral=True)
            return

        team = await self._resolve_member_team(member)
        if team is None:
            await interaction.response.send_message("❌ That user is not in a team.", ephemeral=True)
            return

        await self._staff_manage_team(
            int(team["id"]),
            new_name=team_name,
            new_prefix=team_prefix,
            member_to_remove_id=int(target_user_row["id"]),
        )

        if team_name or team_prefix:
            updated_team = await self.bot.db.fetchone(
                "SELECT name, prefix FROM teams WHERE id = ?",
                (team["id"],),
            )
            name_value = updated_team["name"] if updated_team else team["name"]
            prefix_value = updated_team["prefix"] if updated_team else team["prefix"]
            await interaction.response.send_message(
                f"✅ Team updated: **{name_value}** | `{prefix_value}`\nRemoved member: {member.mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Removed {member.mention} from **{self.format_team_label(team['prefix'], team['name'])}**.",
                ephemeral=True,
            )

    @app_commands.command(name="team_list", description="Staff: list all teams in the server.")
    @staff_only()
    async def team_list(self, interaction: discord.Interaction) -> None:
        rows = await self._list_all_teams_for_staff()
        if not rows:
            await safe_booster_response(interaction, "✅ No teams exist yet.", ephemeral=True)
            return

        embed = create_lawliet_embed(
            self.bot,
            title="👥 Team List",
            description="Current teams and member counts.",
            color=THEME_COLORS["primary"],
        )
        lines = []
        for row in rows:
            name = str(row["name"])
            prefix = str(row["prefix"] or "-")
            member_count = int(row["member_count"])
            earnings = money_cents_to_eur(int(row["team_earnings_cents"]))
            lines.append(f"• {self.format_team_label(row['prefix'], name)} | Members: {member_count} | Earnings: {earnings}")
        embed.add_field(name="Teams", value="\n".join(lines[:15]), inline=False)
        if len(rows) > 15:
            embed.set_footer(text=f"Showing {len(rows[:15])} of {len(rows)} teams")
        await safe_booster_response(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="team_delete", description="Staff: delete a team by name.")
    @app_commands.describe(team_name="Exact team name to delete")
    @staff_only()
    async def team_delete(self, interaction: discord.Interaction, team_name: str) -> None:
        cleaned_name = team_name.strip()
        if not cleaned_name:
            await safe_booster_response(interaction, "❌ Please provide a team name.", ephemeral=True)
            return

        team = await self.bot.db.fetchone(
            "SELECT id, name, prefix FROM teams WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (cleaned_name,),
        )
        if team is None:
            await safe_booster_response(interaction, "❌ Team not found.", ephemeral=True)
            return

        await self.bot.db.execute("DELETE FROM teams WHERE id = ?", (int(team["id"]),))
        await interaction.response.send_message(
            f"✅ Team **{self.format_team_label(team['prefix'], team['name'])}** was deleted.",
            ephemeral=True,
        )

    async def _build_verify_dashboard_embed(self, member: discord.Member) -> discord.Embed:
        user_row = await self.bot.db.fetchone(
            "SELECT verified_booster, rules_confirmed_at FROM users WHERE discord_id = ?",
            (member.id,),
        )
        booster_role_id = getattr(self.settings, "booster_role_id", None)
        approval_role_id = getattr(self.settings, "booster_approval_role_id", None)
        has_booster_role = bool(
            booster_role_id and any(role.id == booster_role_id for role in member.roles)
        )
        has_approval_role = bool(
            approval_role_id and any(role.id == approval_role_id for role in member.roles)
        )
        rules_confirmed = bool(user_row and int(user_row["verified_booster"]) == 1 and user_row["rules_confirmed_at"] and has_booster_role)

        if has_booster_role and rules_confirmed:
            description = "Your booster verification is active. You can use the booster commands now."
            status = "Verified Booster"
            next_step = "You are ready to use the booster commands and dashboard."
        elif has_approval_role and not rules_confirmed:
            description = "Your booster application was approved. Please confirm the rules to unlock booster access."
            status = "Application Approved"
            next_step = "Run /verify_rules to confirm the booster rules and unlock your booster role."
        elif has_booster_role and not rules_confirmed:
            description = "You have the booster role, but you still need to confirm the booster rules."
            status = "Booster Role Pending"
            next_step = "Run /verify_rules to confirm the booster rules and enable access."
        else:
            description = "You are a regular member. Get the booster role and confirm the rules to unlock booster access."
            status = "Member"
            next_step = "Ask staff for the Booster approval role, then use /verify_rules."

        embed = create_lawliet_embed(
            self.bot,
            title="✅ Verification Dashboard",
            description=description,
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="Access status", value=status, inline=True)
        embed.add_field(name="Approval role", value="Yes" if has_approval_role else "No", inline=True)
        embed.add_field(name="Booster role", value="Yes" if has_booster_role else "No", inline=True)
        embed.add_field(name="Rules confirmed", value="Yes" if rules_confirmed else "No", inline=True)
        embed.add_field(name="Next step", value=next_step, inline=False)
        return embed

    async def _post_public_verify_dashboard(self) -> discord.Message | None:
        if self.settings.verification_role_id is None:
            return None
        if self.settings.verify_channel_id is None:
            return None
        channel = self.bot.get_channel(self.settings.verify_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return None
        embed = create_lawliet_embed(
            self.bot,
            title="✅ Verification Dashboard",
            description="Click the button below to receive the member role and unlock access to the server.",
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="What you need", value="Press the button once to receive the @Member role.", inline=False)
        return await channel.send(embed=embed, view=VerifyMemberRoleView(self))

    @app_commands.command(name="verify_dashboard", description="Posts a public verification dashboard for the whole server.")
    @app_commands.guild_only()
    async def verify_dashboard(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        if self.settings.verification_role_id is None:
            await safe_booster_response(
                interaction,
                "❌ The member verification role is not configured yet. Ask staff to set it first.",
                ephemeral=True,
            )
            return
        if self.settings.verify_channel_id is None:
            await safe_booster_response(
                interaction,
                "❌ The verification channel is not configured yet. Ask staff to set it first.",
                ephemeral=True,
            )
            return
        channel = self.bot.get_channel(self.settings.verify_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await safe_booster_response(
                interaction,
                "❌ The configured verification channel could not be found.",
                ephemeral=True,
            )
            return

        await self._post_public_verify_dashboard()
        await safe_booster_response(
            interaction,
            f"✅ Public verification dashboard posted in {channel.mention}.",
            ephemeral=True,
        )

    async def _post_public_rules_dashboard(self) -> discord.Message | None:
        if self.settings.booster_verify_channel_id is None:
            return None
        channel = self.bot.get_channel(self.settings.booster_verify_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return None
        embed = create_lawliet_embed(
            self.bot,
            title="✅ Booster Access Verification",
            description="Click below to confirm the booster rules and activate your booster access.",
            color=THEME_COLORS["primary"],
        )
        embed.add_field(
            name="What you need",
            value="You must already have the approved booster role before accepting the booster access.",
            inline=False,
        )
        embed.add_field(
            name="Status",
            value="This is the booster-specific dashboard, separate from the member verification panel.",
            inline=False,
        )
        return await channel.send(embed=embed, view=self.rules_view)

    @app_commands.command(
        name="verify_rules_dashboard",
        description="Posts a public booster rules confirmation dashboard for approved applicants.",
    )
    @app_commands.guild_only()
    async def verify_rules_dashboard(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        if self.settings.booster_verify_channel_id is None:
            await safe_booster_response(
                interaction,
                "❌ The booster verification channel is not configured yet. Ask staff to set it first.",
                ephemeral=True,
            )
            return
        channel = self.bot.get_channel(self.settings.booster_verify_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await safe_booster_response(
                interaction,
                "❌ The configured booster verification channel could not be found.",
                ephemeral=True,
            )
            return

        await self._post_public_rules_dashboard()
        await safe_booster_response(
            interaction,
            f"✅ Booster rules dashboard posted in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="verify_rules", description="Confirm the booster rules and enable booster access.")
    async def verify_rules(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        _, message = await self.confirm_booster_rules(member)
        await safe_booster_response(interaction, message, ephemeral=True)

    @app_commands.command(
        name="booster_dashboard",
        description="Shows your booster overview.",
    )
    @verified_booster_only()
    async def booster_dashboard(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        stats = await self._resolve_user_stats(member)
        active_orders = await self.bot.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM orders
            WHERE assigned_booster_user_id = ?
              AND status IN ('claimed', 'in_progress')
            """,
            (stats["user_id"],),
        )
        embed = create_lawliet_embed(
            self.bot,
            title="🎮 Booster Dashboard",
            description="Overview of your current booster profile.",
            color=THEME_COLORS["secondary"],
        )
        embed.add_field(name="Level", value=str(stats["booster_level"]))
        embed.add_field(name="Since", value=str(stats["boosted_since"]) if stats["boosted_since"] else "-")
        embed.add_field(name="Active orders", value=str(active_orders["count"]))
        embed.add_field(name="Completed", value=str(stats["completed_orders_count"]))
        embed.add_field(name="Balance", value=money_cents_to_eur(int(stats["balance_cents"])))
        embed.add_field(
            name="Total earned",
            value=money_cents_to_eur(int(stats["total_earned_cents"])),
        )
        embed.add_field(
            name="Referral earned",
            value=money_cents_to_eur(int(stats["referral_earned_cents"])),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard_boosters", description="Shows the booster leaderboard.")
    async def leaderboard_boosters(self, interaction: discord.Interaction) -> None:
        if not self.settings.booster_leaderboard_channel_id:
            await interaction.response.send_message("❌ No leaderboard channel configured.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Leaderboard is being prepared.", ephemeral=True)

    async def build_booster_leaderboard_embed(self, page: int = 1, page_size: int = 10) -> discord.Embed:
        page = max(1, page)
        offset = (page - 1) * page_size
        eligible_discord_ids: set[int] = set()
        if self.settings.booster_role_id is not None and self.bot.get_guild is not None:
            guild = self.bot.get_guild(self.bot.settings.guild_id) if getattr(self.bot, "settings", None) else None
            if guild is not None:
                for member in guild.members:
                    if any(role.id == self.settings.booster_role_id for role in member.roles):
                        eligible_discord_ids.add(member.id)

        sql_filters = ["completed_orders_count > 0"]
        params: list[int] = []
        eligible_conditions = ["role_type = 'booster'"]
        if eligible_discord_ids:
            placeholders = ", ".join("?" for _ in eligible_discord_ids)
            eligible_conditions.append(f"discord_id IN ({placeholders})")
            params.extend(sorted(eligible_discord_ids))
        sql_filters.append(f"({' OR '.join(eligible_conditions)})")

        where_sql = " AND ".join(sql_filters)
        rows = await self.bot.db.fetchall(
            f"""
            SELECT username, booster_level, completed_orders_count, total_earned_cents,
                   COALESCE((SELECT SUM(total_bonus_cents) FROM referrals WHERE referrer_user_id = users.id), 0) AS referral_earned_cents,
                   boosted_since
            FROM users
            WHERE {where_sql}
            ORDER BY total_earned_cents DESC, completed_orders_count DESC, username ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [page_size, offset]),
        )
        total_rows = await self.bot.db.fetchone(
            f"""
            SELECT COUNT(*) AS total
            FROM users
            WHERE {where_sql}
            """,
            tuple(params),
        )
        total_count = int(total_rows["total"]) if total_rows is not None else 0
        entries = []
        for index, row in enumerate(rows, start=offset + 1):
            joined = row["boosted_since"] if row["boosted_since"] else "-"
            player_name = str(row["username"])[:20]
            entries.append(
                (
                    index,
                    player_name,
                    int(row["completed_orders_count"]),
                    money_cents_to_eur(int(row["total_earned_cents"])),
                    joined,
                )
            )

        if not entries:
            embed = create_lawliet_embed(
                self.bot,
                title=f"🏆 Booster Leaderboard (Page {page})",
                description="No boosters with completed orders yet.",
                color=THEME_COLORS["warning"],
            )
        else:
            summary_lines = [
                "Top performers by total earnings.",
            ]
            for index, player_name, completed_count, total_earned, joined in entries:
                summary_lines.append(
                    f"{index}. {player_name} • {completed_count} orders • {total_earned} • {joined}"
                )
            embed = create_lawliet_embed(
                self.bot,
                title=f"🏆 Booster Leaderboard (Page {page})",
                description="\n".join(summary_lines),
                color=THEME_COLORS["warning"],
            )
            for index, player_name, completed_count, total_earned, joined in entries:
                medal = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"#{index}"
                embed.add_field(
                    name=f"{medal} {player_name}",
                    value=(
                        f"Completed: **{completed_count}**\n"
                        f"Earnings: **{total_earned}**\n"
                        f"Since: **{joined}**"
                    ),
                    inline=False,
                )

        if total_count:
            total_pages = max(1, (total_count + page_size - 1) // page_size)
            bot_user = getattr(self.bot, "user", None)
            avatar_url = getattr(getattr(bot_user, "display_avatar", None), "url", None)
            embed.set_footer(text=f"Page {page}/{total_pages} • Rankify", icon_url=avatar_url)
        return embed

    @app_commands.command(name="leaderboard_teams", description="Shows the team leaderboard.")
    async def leaderboard_teams(self, interaction: discord.Interaction) -> None:
        if not self.settings.team_leaderboard_channel_id:
            await interaction.response.send_message("❌ No team leaderboard channel configured.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Team leaderboard is being prepared.", ephemeral=True)

    async def build_team_leaderboard_embed(self) -> discord.Embed:
        rows = await self.bot.db.fetchall(
            """
            SELECT t.name,
                   COUNT(tm.user_id) AS member_count,
                   COALESCE(SUM(u.completed_orders_count), 0) AS total_completed_orders,
                   COALESCE(SUM(u.total_earned_cents), 0) AS team_earnings_cents
            FROM teams t
            LEFT JOIN team_members tm ON tm.team_id = t.id
            LEFT JOIN users u ON u.id = tm.user_id
            GROUP BY t.id, t.name
            ORDER BY total_completed_orders DESC, team_earnings_cents DESC, t.name ASC
            LIMIT 10
            """
        )
        if not rows:
            embed = create_lawliet_embed(
                self.bot,
                title="🏆 Team Leaderboard",
                description="No teams available yet.",
                color=THEME_COLORS["primary"],
            )
            return embed

        embed = create_lawliet_embed(
            self.bot,
            title="🏆 Team Leaderboard",
            description="Team performance ranking.",
            color=THEME_COLORS["primary"],
        )
        for index, row in enumerate(rows, start=1):
            medal = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"#{index}"
            team_name = str(row["name"])[:20]
            embed.add_field(
                name=f"{medal} {team_name}",
                value=(
                    f"Members: **{row['member_count']}**\n"
                    f"Completed: **{row['total_completed_orders']}**\n"
                    f"Earnings: **{money_cents_to_eur(int(row['team_earnings_cents']))}**"
                ),
                inline=False,
            )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BoosterCog(bot))
