from __future__ import annotations

import json

import discord
from discord import app_commands
from discord.ext import commands

from bot.services import resolve_or_create_user
from bot.utils.audit import send_staff_audit_log
from bot.utils.checks import guild_only, staff_only
from bot.utils.formatting import create_lawliet_embed, money_cents_to_eur, parse_eur_to_cents, THEME_COLORS
from bot.utils.permissions import is_staff_member


async def safe_interaction_response(interaction: discord.Interaction, message: str | None = None, *, modal: discord.ui.Modal | None = None, ephemeral: bool = True) -> None:
    response = getattr(interaction, "response", None)
    try:
        if modal is not None:
            if response is not None and hasattr(response, "is_done") and response.is_done():
                if hasattr(interaction, "followup"):
                    await interaction.followup.send(message or "✅ Done.", ephemeral=ephemeral)
                return
            await interaction.response.send_modal(modal)
            return

        if response is not None and hasattr(response, "is_done") and response.is_done():
            if hasattr(interaction, "followup"):
                await interaction.followup.send(message, ephemeral=ephemeral)
            return

        await interaction.response.send_message(message, ephemeral=ephemeral)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        pass


class ApplicationModal(discord.ui.Modal, title="Booster Application"):
    ingame_name = discord.ui.TextInput(label="In-game Name + Tag", max_length=64)
    age = discord.ui.TextInput(label="Age", max_length=3)
    rank = discord.ui.TextInput(label="Current Rank", max_length=32)
    experience = discord.ui.TextInput(
        label="Boosting Experience",
        style=discord.TextStyle.paragraph,
        max_length=800,
    )
    motivation = discord.ui.TextInput(
        label="Why should we pick you?",
        style=discord.TextStyle.paragraph,
        max_length=800,
    )

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Applications are only possible in the server.")
        user_id = await resolve_or_create_user(
            self.cog.bot.db,
            member,
            self.cog.settings.staff_role_ids,
            self.cog.settings.admin_role_ids,
        )
        payload = {
            "ingame_name": str(self.ingame_name),
            "age": str(self.age),
            "rank": str(self.rank),
            "experience": str(self.experience),
            "motivation": str(self.motivation),
        }
        application_id = await self.cog.bot.db.execute(
            """
            INSERT INTO applications (applicant_user_id, status, form_payload_json)
            VALUES (?, 'pending', ?)
            """,
            (user_id, json.dumps(payload, ensure_ascii=True)),
        )

        # Post application into a dedicated application ticket channel
        embed = create_lawliet_embed(
            self.cog.bot,
            title=f"Booster Application #{application_id}",
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="IGN", value=payload["ingame_name"], inline=True)
        embed.add_field(name="Age", value=payload["age"], inline=True)
        embed.add_field(name="Rank", value=payload["rank"], inline=True)
        embed.add_field(name="Experience", value=payload["experience"], inline=False)
        embed.add_field(name="Motivation", value=payload["motivation"], inline=False)

        channel = await self.cog._create_ticket_channel(
            member,
            "application",
            self.cog.settings.application_ticket_category_id,
            embed,
        )

        # Link ticket <-> application in DB
        await self.cog.bot.db.execute(
            """
            INSERT INTO tickets (ticket_type, opener_user_id, channel_id, status, related_application_id)
            VALUES ('application', ?, ?, 'open', ?)
            """,
            (user_id, channel.id, application_id),
        )

        await safe_interaction_response(
            interaction,
            f"✅ Your application has been submitted. Ticket created: {channel.mention}",
            ephemeral=True,
        )


class SupportTicketModal(discord.ui.Modal, title="Open Support Ticket"):
    topic = discord.ui.TextInput(label="Topic", max_length=80)
    details = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        max_length=1200,
    )

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_support_ticket(interaction, str(self.topic), str(self.details))


class PayoutTicketModal(discord.ui.Modal, title="Withdrawal Request"):
    amount = discord.ui.TextInput(label="Amount in EUR (e.g. 25.50)", max_length=16)
    details = discord.ui.TextInput(
        label="Note / withdrawal details",
        style=discord.TextStyle.paragraph,
        max_length=1200,
    )

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount_cents = parse_eur_to_cents(str(self.amount))
        except ValueError:
            await safe_interaction_response(interaction, "❌ Invalid amount.", ephemeral=True)
            return
        if amount_cents <= 0:
            await safe_interaction_response(interaction, "❌ Amount must be greater than 0.", ephemeral=True)
            return
        await self.cog.create_payout_ticket(interaction, amount_cents, str(self.details))


class OrderCompletionTicketModal(discord.ui.Modal, title="Order Completion Request"):
    order_number = discord.ui.TextInput(label="Order Number", max_length=32)
    proof_note = discord.ui.TextInput(
        label="Proof / Finish Note",
        style=discord.TextStyle.paragraph,
        max_length=1200,
    )

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_order_completion_ticket(interaction, str(self.order_number), str(self.proof_note))


class CloseTicketButton(discord.ui.View):
    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Complete Order",
        style=discord.ButtonStyle.success,
        custom_id="ticket:complete_order",
        emoji="✅",
    )
    async def complete_order(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.complete_order_from_ticket(interaction)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
        emoji="🔒",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.close_ticket(interaction)


class ApplicationTicketButton(discord.ui.View):
    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Accept Booster",
        style=discord.ButtonStyle.success,
        custom_id="ticket:accept_booster",
        emoji="✅",
    )
    async def accept_booster(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        member = interaction.user if getattr(interaction.user, "id", None) is not None else None
        channel = interaction.channel if getattr(interaction.channel, "id", None) is not None else None
        if member is None or channel is None:
            raise app_commands.CheckFailure("Invalid context.")

        is_staff = is_staff_member(member, self.cog.settings.staff_role_ids, self.cog.settings.admin_role_ids)
        if not is_staff:
            await safe_interaction_response(interaction, "❌ Only staff can accept booster applications.", ephemeral=True)
            return

        ticket = await self.cog.bot.db.fetchone(
            "SELECT id, opener_user_id, related_application_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (channel.id,),
        )
        if ticket is None:
            await safe_interaction_response(interaction, "❌ This channel is not an open booster application.", ephemeral=True)
            return

        related_application_id = ticket["related_application_id"]
        if related_application_id is None:
            await safe_interaction_response(interaction, "❌ This application is not linked to a booster application.", ephemeral=True)
            return

        staff_user_id = await resolve_or_create_user(
            self.cog.bot.db,
            member,
            self.cog.settings.staff_role_ids,
            self.cog.settings.admin_role_ids,
        )
        await self.cog.bot.db.execute(
            """
            UPDATE applications
            SET status = 'approved',
                reviewed_by_user_id = ?,
                reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (staff_user_id, int(related_application_id)),
        )
        await self.cog.bot.db.execute(
            """
            UPDATE users
            SET role_type = 'booster',
                verified_booster = 0,
                rules_confirmed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(ticket["opener_user_id"]),),
        )

        applicant_row = await self.cog.bot.db.fetchone(
            "SELECT discord_id FROM users WHERE id = ?",
            (int(ticket["opener_user_id"]),),
        )

        if applicant_row and applicant_row["discord_id"] is not None:
            guild = interaction.guild
            if guild is not None:
                target_member = guild.get_member(int(applicant_row["discord_id"]))
                if target_member is not None:
                    approval_role_id = self.cog.settings.booster_approval_role_id
                    approval_role = guild.get_role(approval_role_id) if approval_role_id is not None else None
                    if approval_role is not None and not any(role.id == approval_role.id for role in target_member.roles):
                        try:
                            await target_member.add_roles(approval_role, reason="Booster application approved")
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            try:
                user = self.cog.bot.get_user(int(applicant_row["discord_id"]))
                if user is None:
                    user = await self.cog.bot.fetch_user(int(applicant_row["discord_id"]))
                await user.send(
                    "✅ Your booster application was accepted. Please accept the booster rules with `/verify_rules` in the server to unlock booster access."
                )
            except (discord.NotFound, discord.Forbidden):
                pass

        await safe_interaction_response(interaction, "✅ Booster application accepted.", ephemeral=True)

        try:
            await send_staff_audit_log(
                self.cog.bot,
                title="Booster Application Accepted",
                description=f"{member.mention} accepted a booster application.",
                fields=(
                    ("User", f"<@{int(ticket['opener_user_id'])}>", False),
                    ("Application ID", str(int(related_application_id)), True),
                    ("Channel", f"{channel.name} ({channel.id})", False),
                ),
            )
        except (discord.NotFound, discord.Forbidden):
            pass

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
        emoji="🔒",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.close_ticket(interaction)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Booster Application",
        style=discord.ButtonStyle.success,
        custom_id="panel:apply",
        emoji="📝",
    )
    async def apply(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await safe_interaction_response(interaction, modal=ApplicationModal(self.cog))

    @discord.ui.button(
        label="Support Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="panel:support",
        emoji="🎫",
    )
    async def support(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await safe_interaction_response(interaction, modal=SupportTicketModal(self.cog))

    @discord.ui.button(
        label="Withdrawal",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:payout",
        emoji="💸",
    )
    async def payout(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await safe_interaction_response(interaction, modal=PayoutTicketModal(self.cog))

    @discord.ui.button(
        label="Order Completion",
        style=discord.ButtonStyle.success,
        custom_id="panel:order_completion",
        emoji="✅",
    )
    async def order_completion(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await safe_interaction_response(interaction, modal=OrderCompletionTicketModal(self.cog))


class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self.panel_view = TicketPanelView(self)
        self.close_view = CloseTicketButton(self)
        self.application_view = ApplicationTicketButton(self)

    async def cog_load(self) -> None:
        self.bot.add_view(self.panel_view)
        self.bot.add_view(self.close_view)
        self.bot.add_view(self.application_view)

    def _get_channel(self, channel_id: int | None) -> discord.abc.GuildChannel | None:
        if channel_id is None:
            return None
        return self.bot.get_channel(channel_id)

    def _staff_overwrites(self, guild: discord.Guild) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        for role_id in self.settings.staff_role_ids | self.settings.admin_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        return overwrites

    async def _create_ticket_channel(
        self,
        member: discord.Member,
        ticket_type: str,
        category_id: int | None,
        intro_embed: discord.Embed,
    ) -> discord.TextChannel:
        guild = member.guild
        category = self._get_channel(category_id)
        category_obj = category if isinstance(category, discord.CategoryChannel) else None

        overwrites = self._staff_overwrites(guild)
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

        # Ensure the bot itself can see/send in the created ticket channel
        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        channel_name = f"{ticket_type}-{member.name}".lower().replace(" ", "-")[:90]
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category_obj,
            overwrites=overwrites,
            reason=f"{ticket_type} ticket for {member} ({member.id})",
        )
        view = self.application_view if ticket_type == "application" else self.close_view
        await channel.send(content=member.mention, embed=intro_embed, view=view)
        return channel

    async def complete_order_from_ticket(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if member is None or channel is None:
            raise app_commands.CheckFailure("Invalid context.")

        ticket = await self.bot.db.fetchone(
            "SELECT id, opener_user_id, ticket_type, related_transaction_id, related_order_number FROM tickets WHERE channel_id = ? AND status = 'open'",
            (channel.id,),
        )
        if ticket is None:
            await safe_interaction_response(interaction, "❌ This channel is not an open ticket.", ephemeral=True)
            return

        is_staff = is_staff_member(member, self.settings.staff_role_ids, self.settings.admin_role_ids)
        if not is_staff:
            await safe_interaction_response(interaction, "❌ Only staff can complete an order from a ticket.", ephemeral=True)
            return

        if str(ticket["ticket_type"]) == "order_completion":
            order_number = str(ticket["related_order_number"] or "").strip()
            if not order_number:
                await safe_interaction_response(interaction, "❌ This ticket does not contain a valid order number.", ephemeral=True)
                return
            orders_cog = self.bot.get_cog("OrdersCog")
            if orders_cog is None:
                await safe_interaction_response(interaction, "❌ The order completion command is not available right now.", ephemeral=True)
                return
            await orders_cog.order_complete(interaction, order_number, "Completed via order completion ticket")
            return

        related_tx = await self.bot.db.fetchone(
            "SELECT id, user_id, amount_cents, note FROM transactions WHERE id = ? AND type = 'withdrawal_request' AND status = 'pending'",
            (int(ticket["related_transaction_id"]),),
        )
        if related_tx is None:
            await safe_interaction_response(interaction, "❌ No pending withdrawal request is linked to this ticket.", ephemeral=True)
            return

        await self.bot.db.execute(
            "UPDATE transactions SET status = 'approved', processed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(related_tx["id"]),),
        )
        await self.bot.db.execute(
            "UPDATE users SET balance_cents = balance_cents + ?, total_paid_out_cents = total_paid_out_cents + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (abs(int(related_tx["amount_cents"])), abs(int(related_tx["amount_cents"])), int(related_tx["user_id"])),
        )

        await safe_interaction_response(interaction, "✅ Order marked as completed and payout processed.", ephemeral=True)
        ticket_row = await self.bot.db.fetchone(
            "SELECT id, opener_user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (channel.id,),
        )
        if ticket_row is not None:
            closer_user_id = await resolve_or_create_user(
                self.bot.db,
                member,
                self.settings.staff_role_ids,
                self.settings.admin_role_ids,
            )
            await self.bot.db.execute(
                """
                UPDATE tickets
                SET status = 'closed',
                    closed_by_user_id = ?,
                    closed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (closer_user_id, ticket_row["id"]),
            )
            await channel.edit(name=f"closed-{channel.name}"[:90])

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if member is None or channel is None:
            raise app_commands.CheckFailure("Invalid context.")

        ticket = await self.bot.db.fetchone(
            "SELECT id, opener_user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (channel.id,),
        )
        if ticket is None:
            await safe_interaction_response(
                interaction,
                "❌ This channel is not an open ticket.",
                ephemeral=True,
            )
            return

        opener = await self.bot.db.fetchone(
            "SELECT discord_id FROM users WHERE id = ?", (ticket["opener_user_id"],)
        )
        is_opener = opener is not None and int(opener["discord_id"]) == member.id
        is_staff = is_staff_member(member, self.settings.staff_role_ids, self.settings.admin_role_ids)
        if not is_opener and not is_staff:
            await safe_interaction_response(
                interaction,
                "❌ Only staff or the ticket creator can close it.",
                ephemeral=True,
            )
            return

        closer_user_id = await resolve_or_create_user(
            self.bot.db,
            member,
            self.settings.staff_role_ids,
            self.settings.admin_role_ids,
        )
        await self.bot.db.execute(
            """
            UPDATE tickets
            SET status = 'closed',
                closed_by_user_id = ?,
                closed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (closer_user_id, ticket["id"]),
        )
        await safe_interaction_response(interaction, "🔒 Closing ticket.", ephemeral=True)
        await channel.edit(name=f"closed-{channel.name}"[:90])
        await send_staff_audit_log(
            self.bot,
            title="Ticket Closed",
            description=f"{member.mention} closed a ticket.",
            fields=(
                ("Channel", f"{channel.name} ({channel.id})", False),
                ("Ticket ID", str(ticket["id"]), True),
                ("Is Staff", "Yes" if is_staff else "No", True),
            ),
        )

    async def create_support_ticket(
        self, interaction: discord.Interaction, topic: str, details: str
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        opener_user_id = await resolve_or_create_user(
            self.bot.db,
            member,
            self.settings.staff_role_ids,
            self.settings.admin_role_ids,
        )
        existing = await self.bot.db.fetchone(
            """
            SELECT t.id
            FROM tickets t
            WHERE t.opener_user_id = ? AND t.ticket_type = 'support' AND t.status = 'open'
            """,
            (opener_user_id,),
        )
        if existing is not None:
            await safe_interaction_response(
                interaction,
                "❌ You already have an open support ticket.",
                ephemeral=True,
            )
            return

        embed = create_lawliet_embed(
            self.bot,
            title="Support Ticket",
            color=THEME_COLORS["secondary"],
        )
        embed.add_field(name="Topic", value=topic, inline=False)
        embed.add_field(name="Details", value=details, inline=False)
        channel = await self._create_ticket_channel(
            member, "support", self.settings.support_ticket_category_id, embed
        )
        await self.bot.db.execute(
            """
            INSERT INTO tickets (ticket_type, opener_user_id, channel_id, status)
            VALUES ('support', ?, ?, 'open')
            """,
            (opener_user_id, channel.id),
        )
        await safe_interaction_response(
            interaction,
            f"✅ Support ticket created: {channel.mention}",
            ephemeral=True,
        )

    async def create_payout_ticket(
        self, interaction: discord.Interaction, amount_cents: int, details: str
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        opener_user_id = await resolve_or_create_user(
            self.bot.db,
            member,
            self.settings.staff_role_ids,
            self.settings.admin_role_ids,
        )
        user_row = await self.bot.db.fetchone(
            "SELECT balance_cents FROM users WHERE id = ?", (opener_user_id,)
        )
        if user_row is None:
            raise RuntimeError("User not found after upsert.")
        if int(user_row["balance_cents"]) < amount_cents:
            await safe_interaction_response(
                interaction,
                f"❌ Insufficient balance. Available: {money_cents_to_eur(int(user_row['balance_cents']))}",
                ephemeral=True,
            )
            return

        existing = await self.bot.db.fetchone(
            """
            SELECT t.id
            FROM tickets t
            WHERE t.opener_user_id = ? AND t.ticket_type = 'payout' AND t.status = 'open'
            """,
            (opener_user_id,),
        )
        if existing is not None:
            await safe_interaction_response(
                interaction,
                "❌ You already have an open withdrawal ticket.",
                ephemeral=True,
            )
            return

        embed = create_lawliet_embed(
            self.bot,
            title="Withdrawal Request",
            color=THEME_COLORS["warning"],
        )
        embed.add_field(name="Amount", value=money_cents_to_eur(amount_cents), inline=False)
        embed.add_field(name="Note", value=details, inline=False)
        channel = await self._create_ticket_channel(
            member, "payout", self.settings.payout_ticket_category_id, embed
        )
        transaction_id = await self.bot.db.execute(
            """
            INSERT INTO transactions (
                user_id, type, status, amount_cents, note, requested_by_user_id, payout_ticket_channel_id
            ) VALUES (?, 'withdrawal_request', 'pending', ?, ?, ?, ?)
            """,
            (opener_user_id, -amount_cents, details, opener_user_id, channel.id),
        )
        await self.bot.db.execute(
            """
            INSERT INTO tickets (ticket_type, opener_user_id, channel_id, status, related_transaction_id)
            VALUES ('payout', ?, ?, 'open', ?)
            """,
            (opener_user_id, channel.id, transaction_id),
        )
        await safe_interaction_response(
            interaction,
            f"✅ Withdrawal ticket created: {channel.mention}",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Payout Ticket Created",
            description=f"{member.mention} created a withdrawal ticket.",
            fields=(
                ("Transaction", f"#{transaction_id}", True),
                ("Amount", money_cents_to_eur(amount_cents), True),
                ("Channel", f"{channel.name} ({channel.id})", False),
            ),
        )

    async def create_order_completion_ticket(
        self, interaction: discord.Interaction, order_number: str, proof_note: str
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")

        normalized_order_number = order_number.strip()
        if not normalized_order_number:
            await safe_interaction_response(interaction, "❌ Please provide an order number.", ephemeral=True)
            return

        opener_user_id = await resolve_or_create_user(
            self.bot.db,
            member,
            self.settings.staff_role_ids,
            self.settings.admin_role_ids,
        )

        order_row = await self.bot.db.fetchone(
            """
            SELECT id, status, assigned_booster_user_id, current_rank, target_rank, booster_payout_cents
            FROM orders
            WHERE order_number = ?
            """,
            (normalized_order_number,),
        )
        if order_row is None:
            await safe_interaction_response(interaction, "❌ Order not found.", ephemeral=True)
            return
        if int(order_row["assigned_booster_user_id"]) != opener_user_id:
            await safe_interaction_response(interaction, "❌ Only the assigned booster can request completion for this order.", ephemeral=True)
            return
        if str(order_row["status"]) not in {"claimed", "in_progress"}:
            await safe_interaction_response(interaction, "❌ This order is not currently active and cannot be completed through a ticket.", ephemeral=True)
            return

        existing = await self.bot.db.fetchone(
            """
            SELECT t.id
            FROM tickets t
            WHERE t.opener_user_id = ? AND t.ticket_type = 'order_completion' AND t.status = 'open'
            """,
            (opener_user_id,),
        )
        if existing is not None:
            await safe_interaction_response(interaction, "❌ You already have an open order completion ticket.", ephemeral=True)
            return

        embed = create_lawliet_embed(
            self.bot,
            title="Order Completion Request",
            color=THEME_COLORS["success"],
        )
        embed.add_field(name="Order", value=normalized_order_number, inline=True)
        embed.add_field(name="Route", value=f"{order_row['current_rank']} → {order_row['target_rank']}", inline=True)
        embed.add_field(name="Payout", value=money_cents_to_eur(int(order_row["booster_payout_cents"])), inline=True)
        if proof_note.strip():
            embed.add_field(name="Proof / Note", value=proof_note.strip(), inline=False)

        channel = await self._create_ticket_channel(
            member, "order-completion", self.settings.support_ticket_category_id, embed
        )
        await self.bot.db.execute(
            """
            INSERT INTO tickets (ticket_type, opener_user_id, channel_id, status, related_order_number)
            VALUES ('order_completion', ?, ?, 'open', ?)
            """,
            (opener_user_id, channel.id, normalized_order_number),
        )
        await safe_interaction_response(
            interaction,
            f"✅ Order completion ticket created: {channel.mention}",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Order Completion Ticket Created",
            description=f"{member.mention} requested completion for an order.",
            fields=(
                ("Order", normalized_order_number, True),
                ("Channel", f"{channel.name} ({channel.id})", False),
                ("Proof", proof_note.strip() or "-", False),
            ),
        )

    @app_commands.command(
        name="tickets_setup",
        description="Posts the ticket/application panel in the current channel.",
    )
    @staff_only()
    async def tickets_setup(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if channel is None:
            raise app_commands.CheckFailure("Only available in text channels.")

        embed = create_lawliet_embed(
            self.bot,
            title="Support & Application",
            description=(
                "Use the buttons below for support, withdrawal, or a booster application."
            ),
            color=THEME_COLORS["primary"],
        )
        await channel.send(embed=embed, view=self.panel_view)
        await safe_interaction_response(interaction, "✅ Panel posted.", ephemeral=True)

    @app_commands.command(
        name="ticket_close",
        description="Closes the current ticket (staff or creator).",
    )
    @guild_only()
    async def ticket_close(self, interaction: discord.Interaction) -> None:
        await self.close_ticket(interaction)

    @tickets_setup.error
    @ticket_close.error
    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ You do not have permission for this.", ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        "❌ You do not have permission for this.", ephemeral=True
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                pass
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketCog(bot))
