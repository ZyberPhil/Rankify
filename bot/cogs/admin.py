from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import add_role_id, persist_settings
from bot.services import resolve_or_create_user
from bot.utils.audit import send_staff_audit_log
from bot.utils.checks import admin_only, command_channel_only, staff_only
from bot.utils.formatting import money_cents_to_eur, parse_eur_to_cents


async def safe_admin_response(interaction: discord.Interaction, message: str | None = None, *, ephemeral: bool = True) -> None:
    try:
        response = getattr(interaction, "response", None)
        if response is not None and getattr(response, "is_done", lambda: False)():
            if hasattr(interaction, "followup"):
                await interaction.followup.send(message, ephemeral=ephemeral)
            return
        await interaction.response.send_message(message, ephemeral=ephemeral)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        pass


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings

    def _apply_settings(self, **overrides):
        current = self.bot.settings
        merged = {
            "discord_token": current.discord_token,
            "guild_id": current.guild_id,
            "database_path": current.database_path,
            "staff_role_ids": current.staff_role_ids,
            "admin_role_ids": current.admin_role_ids,
            "referral_allowed_role_ids": current.referral_allowed_role_ids,
            "team_creation_role_id": current.team_creation_role_id,
            "team_setup_channel_id": current.team_setup_channel_id,
            "booster_completion_channel_id": current.booster_completion_channel_id,
            "booster_completion_thresholds": current.booster_completion_thresholds,
            "command_only_channel_ids": current.command_only_channel_ids,
            "booster_role_id": current.booster_role_id,
            "booster_approval_role_id": current.booster_approval_role_id,
            "verification_role_id": current.verification_role_id,
            "verify_channel_id": current.verify_channel_id,
            "booster_verify_channel_id": current.booster_verify_channel_id,
            "welcome_channel_id": current.welcome_channel_id,
            "booster_leaderboard_channel_id": current.booster_leaderboard_channel_id,
            "team_leaderboard_channel_id": current.team_leaderboard_channel_id,
            "support_ticket_category_id": current.support_ticket_category_id,
            "payout_ticket_category_id": current.payout_ticket_category_id,
            "application_ticket_category_id": current.application_ticket_category_id,
            "application_review_channel_id": current.application_review_channel_id,
            "staff_audit_log_channel_id": current.staff_audit_log_channel_id,
            "referral_log_channel_id": current.referral_log_channel_id,
            "expired_order_action_channel_id": current.expired_order_action_channel_id,
        }
        merged.update(overrides)
        self.bot.settings = current.__class__(**merged)
        self.settings = self.bot.settings

        booster_cog = self.bot.get_cog("BoosterCog")
        if booster_cog is not None:
            booster_cog.settings = self.bot.settings
        orders_cog = self.bot.get_cog("OrdersCog")
        if orders_cog is not None:
            orders_cog.settings = self.bot.settings
        tickets_cog = self.bot.get_cog("TicketsCog")
        if tickets_cog is not None:
            tickets_cog.settings = self.bot.settings

        return self.bot.settings

    async def _resolve_actor_user(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, int]:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Guild-only command.")
        user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        return member, user_id

    @app_commands.command(name="admin_ping", description="Staff health check command.")
    @staff_only()
    async def admin_ping(self, interaction: discord.Interaction) -> None:
        await self._resolve_actor_user(interaction)
        await safe_admin_response(interaction, "✅ Admin area active.", ephemeral=True)

    @app_commands.command(
        name="command_channel_setup",
        description="Configures a channel as a command-only/bot-only channel.",
    )
    @app_commands.describe(channel="Text channel that only allows commands/bot messages")
    @staff_only()
    async def command_channel_setup(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        allowed = set(self.settings.command_only_channel_ids)
        allowed.add(channel.id)
        self._apply_settings(command_only_channel_ids=allowed)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Channel {channel.mention} configured as a command-only channel.",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_booster_role",
        description="Sets the Discord role that identifies a booster.",
    )
    @app_commands.describe(role="Role that should count as a booster")
    @staff_only()
    async def set_booster_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        self._apply_settings(booster_role_id=role.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Booster role set to {role.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_booster_approval_role",
        description="Sets the Discord role granted when a booster application is accepted before rules confirmation.",
    )
    @app_commands.describe(role="Role assigned after application approval, before final booster verification")
    @staff_only()
    async def set_booster_approval_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        self._apply_settings(booster_approval_role_id=role.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Booster approval role set to {role.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_verification_role",
        description="Sets the Discord role that members can claim through the verification dashboard.",
    )
    @app_commands.describe(role="Role that should be granted to members via verification")
    @staff_only()
    async def set_verification_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        self._apply_settings(verification_role_id=role.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Verification role set to {role.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_verify_channel",
        description="Sets the channel where the member verification dashboard message is posted.",
    )
    @app_commands.describe(channel="Text channel for the member verification message")
    @staff_only()
    async def set_verify_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        self._apply_settings(verify_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Member verification channel set to {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_booster_verify_channel",
        description="Sets the channel where the booster access dashboard message is posted.",
    )
    @app_commands.describe(channel="Text channel for the booster access message")
    @staff_only()
    async def set_booster_verify_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        self._apply_settings(booster_verify_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Booster verification channel set to {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_welcome_channel",
        description="Sets the channel where the welcome message is posted for new members.",
    )
    @app_commands.describe(channel="Text channel for join welcome messages")
    @staff_only()
    async def set_welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        self._apply_settings(welcome_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Welcome channel set to {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_referral_log_channel",
        description="Sets the channel where referral payout logs are posted.",
    )
    @app_commands.describe(channel="Text channel for referral payout logs")
    @staff_only()
    async def set_referral_log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._apply_settings(referral_log_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Referral log channel set to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_expired_order_action_channel",
        description="Sets the channel where expired claimed orders get staff action buttons.",
    )
    @app_commands.describe(channel="Text channel for expired order action controls")
    @staff_only()
    async def set_expired_order_action_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._apply_settings(expired_order_action_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Expired order action channel set to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_booster_leaderboard_channel",
        description="Sets the channel where the booster leaderboard is posted.",
    )
    @app_commands.describe(channel="Text channel for the booster leaderboard")
    @staff_only()
    async def set_booster_leaderboard_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._apply_settings(booster_leaderboard_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Booster leaderboard channel set to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="set_team_leaderboard_channel",
        description="Sets the channel where the team leaderboard is posted.",
    )
    @app_commands.describe(channel="Text channel for the team leaderboard")
    @staff_only()
    async def set_team_leaderboard_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._apply_settings(team_leaderboard_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Team leaderboard channel set to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="add_referral_allowed_role",
        description="Allows a Discord role to create or use referral links.",
    )
    @app_commands.describe(role="Role allowed to use the referral system")
    @staff_only()
    async def add_referral_allowed_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        allowed = add_role_id(self.bot.settings.referral_allowed_role_ids, role.id)
        self._apply_settings(referral_allowed_role_ids=allowed)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Referral role added: {role.mention}", ephemeral=True)

    @app_commands.command(
        name="set_team_creation_role",
        description="Sets the Discord role allowed to create a team.",
    )
    @app_commands.describe(role="Role allowed to create teams")
    @staff_only()
    async def set_team_creation_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        self._apply_settings(team_creation_role_id=role.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Team creation role set to {role.mention}.", ephemeral=True)

    @app_commands.command(
        name="set_team_setup_channel",
        description="Sets the permanent channel for the team control panel.",
    )
    @app_commands.describe(channel="Channel where the persistent team panel should be posted")
    @staff_only()
    async def set_team_setup_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._apply_settings(team_setup_channel_id=channel.id)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(
            interaction,
            f"✅ Team setup channel set to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="add_staff_role",
        description="Adds a role to the bot staff-role list.",
    )
    @app_commands.describe(role="Role that should count as staff")
    @admin_only()
    async def add_staff_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        new_roles = add_role_id(self.bot.settings.staff_role_ids, role.id)
        self._apply_settings(staff_role_ids=new_roles)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Staff role added: {role.mention}", ephemeral=True)

    @app_commands.command(
        name="add_admin_role",
        description="Adds a role to the bot admin-role list.",
    )
    @app_commands.describe(role="Role that should count as admin")
    @admin_only()
    async def add_admin_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        new_roles = add_role_id(self.bot.settings.admin_role_ids, role.id)
        self._apply_settings(admin_role_ids=new_roles)
        await persist_settings(self.bot.db, self.bot.settings)
        await safe_admin_response(interaction, f"✅ Admin role added: {role.mention}", ephemeral=True)

    @app_commands.command(
        name="application_decide",
        description="Staff: accept or reject applications.",
    )
    @app_commands.describe(
        application_id="ID from the applications table",
        decision="approve or reject",
        note="Staff note",
        team_referral_code="Optional team referral code if the applicant joins a team",
    )
    @staff_only()
    async def application_decide(
        self,
        interaction: discord.Interaction,
        application_id: int,
        decision: str,
        note: str | None = None,
        team_referral_code: str | None = None,
    ) -> None:
        member, staff_user_id = await self._resolve_actor_user(interaction)
        decision_normalized = decision.lower().strip()
        if decision_normalized not in {"approve", "reject"}:
            await interaction.response.send_message(
                "❌ decision must be `approve` or `reject`.",
                ephemeral=True,
            )
            return
        status = "approved" if decision_normalized == "approve" else "rejected"
        async with self.bot.db.transaction() as db:
            async with db.execute(
                """
                SELECT id, applicant_user_id, status
                FROM applications
                WHERE id = ?
                """,
                (application_id,),
            ) as cursor:
                app_row = await cursor.fetchone()
            if app_row is None:
                await interaction.response.send_message("❌ Application not found.", ephemeral=True)
                return
            if app_row["status"] != "pending":
                await interaction.response.send_message(
                    "❌ Application has already been processed.", ephemeral=True
                )
                return

            await db.execute(
                """
                UPDATE applications
                SET status = ?,
                    reviewed_by_user_id = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, staff_user_id, app_row["id"]),
            )
            if status == "approved":
                await db.execute(
                    """
                    UPDATE users
                    SET role_type = 'booster', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (app_row["applicant_user_id"],),
                )
                if team_referral_code:
                    async with db.execute(
                        "SELECT id, name, prefix FROM teams WHERE UPPER(referral_code) = ? LIMIT 1",
                        (team_referral_code.strip().upper(),),
                    ) as team_cursor:
                        team_row = await team_cursor.fetchone()
                    if team_row is None:
                        await interaction.response.send_message(
                            "❌ Team referral code not found for approved application.",
                            ephemeral=True,
                        )
                        return
                    await db.execute("DELETE FROM team_members WHERE user_id = ?", (app_row["applicant_user_id"],))
                    await db.execute(
                        "INSERT OR IGNORE INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_row["id"], app_row["applicant_user_id"]),
                    )
        msg = f"✅ Application #{application_id} was `{status}`."
        if note:
            msg += f"\nNote: {note}"
        await interaction.response.send_message(msg, ephemeral=True)
        await send_staff_audit_log(
            self.bot,
            title="Application Decision",
            description=f"{member.mention} decided application #{application_id}.",
            fields=(
                ("Decision", status, True),
                ("Staff", f"{member} ({member.id})", False),
                ("Note", note or "-", False),
            ),
        )

    @app_commands.command(
        name="admin_adjust_balance",
        description="Staff: record a manual balance adjustment.",
    )
    @app_commands.describe(member="User", amount_eur="Amount (+/-) in EUR", note="Reason for adjustment")
    @admin_only()
    async def admin_adjust_balance(
        self, interaction: discord.Interaction, member: discord.Member, amount_eur: str, note: str
    ) -> None:
        actor_member, staff_user_id = await self._resolve_actor_user(interaction)
        try:
            amount_cents = parse_eur_to_cents(amount_eur)
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
            return
        if amount_cents == 0:
            await interaction.response.send_message("❌ Amount cannot be 0.", ephemeral=True)
            return

        target_user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        async with self.bot.db.transaction() as db:
            async with db.execute(
                "SELECT balance_cents FROM users WHERE id = ?", (target_user_id,)
            ) as cursor:
                user_row = await cursor.fetchone()
            if user_row is None:
                await interaction.response.send_message("❌ Target user not found.", ephemeral=True)
                return
            new_balance = int(user_row["balance_cents"]) + amount_cents
            if new_balance < 0:
                await interaction.response.send_message(
                    "❌ This adjustment would create a negative balance.",
                    ephemeral=True,
                )
                return

            await db.execute(
                """
                UPDATE users
                SET balance_cents = ?,
                    total_earned_cents = total_earned_cents + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_balance, max(amount_cents, 0), target_user_id),
            )
            await db.execute(
                """
                INSERT INTO transactions (
                    user_id, requested_by_user_id, approved_by_user_id,
                    type, status, amount_cents, note, processed_at
                )
                VALUES (?, ?, ?, 'manual_adjustment', 'approved', ?, ?, CURRENT_TIMESTAMP)
                """,
                (target_user_id, staff_user_id, staff_user_id, amount_cents, note),
            )
        await interaction.response.send_message(
            (
                f"✅ Balance adjusted for {member.mention}.\n"
                f"Change: {money_cents_to_eur(amount_cents)}\n"
                f"New balance: {money_cents_to_eur(new_balance)}"
            ),
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Manual Balance Adjustment",
            description=f"{actor_member.mention} adjusted the balance.",
            fields=(
                ("Target", f"{member} ({member.id})", False),
                ("Change", money_cents_to_eur(amount_cents), True),
                ("New Balance", money_cents_to_eur(new_balance), True),
                ("Note", note, False),
            ),
        )

    @admin_ping.error
    @application_decide.error
    @admin_adjust_balance.error
    @command_channel_setup.error
    @set_booster_role.error
    @set_booster_leaderboard_channel.error
    @set_team_leaderboard_channel.error
    @set_team_creation_role.error
    @set_team_setup_channel.error
    @add_referral_allowed_role.error
    @add_staff_role.error
    @add_admin_role.error
    async def admin_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"❌ {error}", ephemeral=True)
                else:
                    await interaction.followup.send(f"❌ {error}", ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                pass
            return
        if isinstance(error, discord.NotFound):
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
