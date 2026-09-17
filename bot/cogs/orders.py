from __future__ import annotations

from datetime import datetime, timedelta, UTC

import discord
from discord import app_commands
from discord.ext import commands

from bot.services import can_transition_order, resolve_or_create_user
from bot.utils.audit import (
    send_booster_milestone_announcement,
    send_referral_audit_log,
    send_referral_earning_notification,
    send_staff_audit_log,
)
from bot.utils.checks import guild_only, staff_only
from bot.utils.formatting import create_lawliet_embed, money_cents_to_eur, parse_eur_to_cents, THEME_COLORS
from bot.utils.ids import generate_order_number


class ClaimOrderView(discord.ui.View):
    def __init__(self, cog: "OrdersCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Take Order",
        style=discord.ButtonStyle.success,
        custom_id="order:claim",
        emoji="📥",
    )
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.claim_order(interaction)


class ExpiredOrderActionView(discord.ui.View):
    def __init__(self, cog: "OrdersCog", order_id: int, original_booster_user_id: int | None, order_number: str) -> None:
        super().__init__(timeout=60 * 60 * 24)
        self.cog = cog
        self.order_id = order_id
        self.original_booster_user_id = original_booster_user_id
        self.order_number = order_number

    async def _require_staff(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False
        is_staff = member.guild_permissions.administrator or any(
            role.id in (self.cog.settings.staff_role_ids | self.cog.settings.admin_role_ids)
            for role in member.roles
        )
        if not is_staff:
            await interaction.response.send_message("❌ Only staff can act on expired orders.", ephemeral=True)
        return is_staff

    async def _perform_action(self, interaction: discord.Interaction, action: str) -> None:
        if not await self._require_staff(interaction):
            return
        async with self.cog.bot.db.transaction() as db:
            async with db.execute(
                "SELECT * FROM orders WHERE id = ?",
                (self.order_id,),
            ) as cursor:
                order = await cursor.fetchone()
            if order is None:
                await interaction.response.send_message("❌ This order no longer exists.", ephemeral=True)
                return
            if action == "repost":
                await db.execute(
                    """
                    UPDATE orders
                    SET status = 'open',
                        assigned_booster_user_id = NULL,
                        claimed_at = NULL,
                        deadline_at = NULL,
                        deadline_reminder_sent_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (self.order_id,),
                )
                await self.cog._repost_order_to_channel(dict(order))
                message = f"✅ Order `{self.order_number}` was reopened for claiming."
            elif action == "delete":
                await db.execute("DELETE FROM orders WHERE id = ?", (self.order_id,))
                message = f"✅ Order `{self.order_number}` was deleted."
            else:
                new_deadline = (datetime.now(UTC) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                await db.execute(
                    """
                    UPDATE orders
                    SET status = 'claimed',
                        assigned_booster_user_id = ?,
                        claimed_at = CURRENT_TIMESTAMP,
                        deadline_at = ?,
                        deadline_reminder_sent_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (self.original_booster_user_id, new_deadline, self.order_id),
                )
                message = f"✅ Order `{self.order_number}` was reassigned to the previous booster with a new 24h deadline."
        await interaction.response.send_message(message, ephemeral=True)
        if interaction.message is not None:
            try:
                await interaction.message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

    @discord.ui.button(label="Repost order", style=discord.ButtonStyle.success, custom_id="expired_order_action:repost")
    async def repost_order(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._perform_action(interaction, "repost")

    @discord.ui.button(label="Delete order", style=discord.ButtonStyle.danger, custom_id="expired_order_action:delete")
    async def delete_order(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._perform_action(interaction, "delete")

    @discord.ui.button(label="Reassign 24h", style=discord.ButtonStyle.primary, custom_id="expired_order_action:reassign")
    async def reassign_24h(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._perform_action(interaction, "reassign")


class OrdersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self.claim_view = ClaimOrderView(self)

    @staticmethod
    def get_active_order_limit_for_booster_level(booster_level: int) -> int | None:
        """Return the maximum number of active orders at the same time.

        Beginner boosters may hold one active order, pro boosters may hold two,
        and master boosters have no simultaneous-order cap.
        """
        if booster_level < 3:
            return 1
        if booster_level < 6:
            return 2
        return None

    @staticmethod
    def get_booster_tier_label(booster_level: int) -> str:
        if booster_level < 3:
            return "Beginner"
        if booster_level < 6:
            return "Pro"
        return "Master"

    async def _apply_team_owner_bonus(self, db, member_user_id: int, team_id: int, payout_cents: int, order_id: int | None = None) -> int:
        if hasattr(db, "_write_lock"):
            team_row = await db.fetchone(
                "SELECT created_by_user_id FROM teams WHERE id = ?",
                (team_id,),
            )
        else:
            async with db.execute(
                "SELECT created_by_user_id FROM teams WHERE id = ?",
                (team_id,),
            ) as cursor:
                team_row = await cursor.fetchone()
        if team_row is None:
            return 0
        owner_user_id = int(team_row["created_by_user_id"])
        if owner_user_id is None or owner_user_id == member_user_id:
            return 0
        bonus_cents = payout_cents * 5 // 100
        if bonus_cents <= 0:
            return 0
        final_order_id = order_id
        if final_order_id is not None:
            if hasattr(db, "_write_lock"):
                order_exists = await db.fetchone("SELECT id FROM orders WHERE id = ?", (final_order_id,))
            else:
                async with db.execute("SELECT id FROM orders WHERE id = ?", (final_order_id,)) as order_cursor:
                    order_exists = await order_cursor.fetchone()
            if order_exists is None:
                final_order_id = None

        await db.execute(
            "UPDATE users SET balance_cents = balance_cents + ?, total_earned_cents = total_earned_cents + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (bonus_cents, bonus_cents, owner_user_id),
        )
        await db.execute(
            "INSERT INTO transactions (user_id, order_id, type, status, amount_cents, note, processed_at) VALUES (?, ?, 'manual_adjustment', 'approved', ?, ?, CURRENT_TIMESTAMP)",
            (owner_user_id, final_order_id, bonus_cents, "Team owner bonus for team member payout"),
        )
        async with db.execute(
            "SELECT discord_id FROM users WHERE id = ?",
            (owner_user_id,),
        ) as owner_cursor:
            owner_row = await owner_cursor.fetchone()
        async with db.execute(
            "SELECT discord_id FROM users WHERE id = ?",
            (member_user_id,),
        ) as member_cursor:
            member_row = await member_cursor.fetchone()
        if owner_row is not None and member_row is not None:
            await send_referral_earning_notification(
                self.bot,
                int(owner_row["discord_id"]),
                int(member_row["discord_id"]),
            )
        return bonus_cents

    async def cog_load(self) -> None:
        self.bot.add_view(self.claim_view)

    async def _extract_order_id_from_message(self, interaction: discord.Interaction) -> int | None:
        message = interaction.message
        if message is None or not message.embeds:
            return None
        embed = message.embeds[0]
        footer = embed.footer.text or ""
        if not footer.startswith("order_id:"):
            return None
        try:
            return int(footer.split(":", maxsplit=1)[1])
        except ValueError:
            return None

    @staticmethod
    def _build_claim_dm(order: dict, account_username: str, account_password: str) -> str:
        lines = [
            f"✅ Order `{order['order_number']}` claimed.",
            f"Region: {order.get('region', '-')}",
            f"Route: {order.get('current_rank', '-')} → {order.get('target_rank', '-')}",
            f"Payout: {money_cents_to_eur(int(order.get('booster_payout_cents', 0)))}",
            "",
            "Account details:",
            f"Username: {account_username}",
            f"Password: {account_password}",
        ]
        if order.get("note"):
            lines.extend(["", f"Note: {order['note']}"])
        if order.get("customer_name"):
            lines.extend(["", f"Customer: {order['customer_name']}"])
        return "\n".join(lines)

    async def _build_order_embed(self, order: dict) -> discord.Embed:
        embed = create_lawliet_embed(
            self.bot,
            title="🎮 New Order",
            color=THEME_COLORS["success"],
            description=f"**{order['current_rank']} → {order['target_rank']}**",
        )
        embed.add_field(name="Order", value=order["order_number"], inline=True)
        embed.add_field(name="Region", value=order["region"], inline=True)
        embed.add_field(name="Booster Payout", value=money_cents_to_eur(int(order["booster_payout_cents"])), inline=True)
        if int(order.get("referral_bonus_cents") or 0):
            embed.add_field(name="Referral Bonus", value=money_cents_to_eur(int(order["referral_bonus_cents"])), inline=True)
        if order.get("note"):
            embed.add_field(name="Note", value=str(order["note"]), inline=False)
        if order.get("deadline_at"):
            embed.add_field(name="Deadline", value=str(order["deadline_at"]), inline=False)
        embed.set_footer(text=f"order_id:{order['id']}")
        return embed

    async def _repost_order_to_channel(self, order: dict) -> None:
        posted_channel_id = order.get("posted_channel_id")
        if posted_channel_id is None:
            return
        channel = self.bot.get_channel(int(posted_channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        embed = await self._build_order_embed(order)
        await channel.send(embed=embed, view=self.claim_view)

    async def _check_claimed_order_deadlines(self) -> None:
        rows = await self.bot.db.fetchall(
            """
            SELECT o.*, u.discord_id AS booster_discord_id
            FROM orders o
            LEFT JOIN users u ON u.id = o.assigned_booster_user_id
            WHERE o.status = 'claimed'
              AND o.deadline_at IS NOT NULL
            """
        )
        now = datetime.now(UTC)
        for row in rows:
            deadline = row["deadline_at"]
            if not deadline:
                continue
            if isinstance(deadline, str):
                try:
                    deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                except ValueError:
                    continue
            else:
                deadline_dt = deadline
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=UTC)
            remaining = deadline_dt - now
            if remaining.total_seconds() <= 0:
                action_channel_id = getattr(self.bot.settings, "expired_order_action_channel_id", None)
                if action_channel_id is not None:
                    channel = self.bot.get_channel(int(action_channel_id))
                    if channel is not None and hasattr(channel, "send"):
                        order_data = dict(row)
                        embed = create_lawliet_embed(
                            self.bot,
                            title="⚠️ Claimed order expired",
                            description=f"Order `{row['order_number']}` expired and needs staff action.",
                            color=THEME_COLORS["warning"],
                        )
                        embed.add_field(name="Order", value=row["order_number"], inline=True)
                        embed.add_field(name="Route", value=f"{row['current_rank']} → {row['target_rank']}", inline=True)
                        embed.add_field(name="Booster payout", value=money_cents_to_eur(int(row["booster_payout_cents"])), inline=True)
                        if row["account_username"] is not None:
                            embed.add_field(name="Username", value=str(row["account_username"]), inline=True)
                        if row["account_password"] is not None:
                            embed.add_field(name="Password", value=str(row["account_password"]), inline=True)
                        if row["booster_discord_id"] is not None:
                            embed.add_field(name="Assigned booster", value=f"<@{int(row['booster_discord_id'])}>", inline=False)
                        await channel.send(
                            embed=embed,
                            view=ExpiredOrderActionView(
                                self,
                                int(row["id"]),
                                int(row["assigned_booster_user_id"]) if row["assigned_booster_user_id"] is not None else None,
                                str(row["order_number"]),
                            ),
                        )
                    if row["booster_discord_id"] is not None:
                        user = self.bot.get_user(int(row["booster_discord_id"]))
                        if user is not None:
                            await user.send(
                                f"⏰ Order `{row['order_number']}` expired. Staff can now repost, delete, or reassign it."
                            )
                    continue
                await self.bot.db.execute(
                    """
                    UPDATE orders
                    SET status = 'open',
                        assigned_booster_user_id = NULL,
                        claimed_at = NULL,
                        deadline_at = NULL,
                        deadline_reminder_sent_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                await self._repost_order_to_channel(dict(row))
                if row["booster_discord_id"] is not None:
                    user = self.bot.get_user(int(row["booster_discord_id"]))
                    if user is not None:
                        await user.send(
                            f"⏰ Order `{row['order_number']}` expired and was reopened for claiming: {row['current_rank']} → {row['target_rank']}."
                        )
                continue
            reminder_threshold = timedelta(days=1)
            if remaining <= reminder_threshold:
                reminder_at = row["deadline_reminder_sent_at"]
                if not reminder_at:
                    if row["booster_discord_id"] is not None:
                        user = self.bot.get_user(int(row["booster_discord_id"]))
                        if user is not None:
                            deadline_label = deadline_dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
                            total_seconds = max(int(remaining.total_seconds()), 0)
                            hours, remainder = divmod(total_seconds, 3600)
                            minutes = remainder // 60
                            remaining_label = f"{hours}h {minutes}m"
                            await user.send(
                                "\n".join(
                                    [
                                        f"⏰ Deadline reminder for order `{row['order_number']}`.",
                                        f"Route: {row['current_rank']} → {row['target_rank']}",
                                        f"Deadline: {deadline_label}",
                                        f"Time left: {remaining_label}",
                                        "Please complete the order before the timestamp above.",
                                    ]
                                )
                            )
                    await self.bot.db.execute(
                        "UPDATE orders SET deadline_reminder_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (row["id"],),
                    )

    async def claim_order(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")

        order_id = await self._extract_order_id_from_message(interaction)
        if order_id is None:
            try:
                await interaction.response.send_message(
                    "❌ Order could not be recognized.", ephemeral=True
                )
            except discord.NotFound:
                return
            return
        booster_user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        async with self.bot.db.transaction() as db:
            async with db.execute(
                "SELECT status, order_number, current_rank, target_rank, region, note, customer_name, booster_payout_cents, claim_deadline_hours, deadline_at, account_username, account_password FROM orders WHERE id = ?",
                (order_id,),
            ) as cursor:
                order = await cursor.fetchone()
            if order is None:
                try:
                    await interaction.response.send_message("❌ Order does not exist.", ephemeral=True)
                except discord.NotFound:
                    return
                return
            current_status = str(order["status"])
            if not can_transition_order(current_status, "claimed"):
                try:
                    await interaction.response.send_message(
                        f"❌ Order cannot be claimed from status `{current_status}`.",
                        ephemeral=True,
                    )
                except discord.NotFound:
                    return
                return

            async with db.execute(
                "SELECT booster_level FROM users WHERE id = ?",
                (booster_user_id,),
            ) as booster_cursor:
                booster_row = await booster_cursor.fetchone()
            if booster_row is None:
                try:
                    await interaction.response.send_message(
                        "❌ Your booster profile could not be found.",
                        ephemeral=True,
                    )
                except discord.NotFound:
                    return
                return

            max_active_orders = self.get_active_order_limit_for_booster_level(int(booster_row["booster_level"]))
            if max_active_orders is not None:
                async with db.execute(
                    "SELECT COUNT(*) AS active_count FROM orders WHERE assigned_booster_user_id = ? AND status IN ('claimed', 'in_progress')",
                    (booster_user_id,),
                ) as active_cursor:
                    active_count_row = await active_cursor.fetchone()
                active_count = int(active_count_row["active_count"]) if active_count_row is not None else 0
                if active_count >= max_active_orders:
                    tier_label = self.get_booster_tier_label(int(booster_row["booster_level"]))
                    try:
                        await interaction.response.send_message(
                            f"❌ You are a {tier_label} booster and can only have {max_active_orders} active order(s) at the same time.",
                            ephemeral=True,
                        )
                    except discord.NotFound:
                        return
                    return

            deadline_at = None
            claim_deadline_hours = order["claim_deadline_hours"]
            if claim_deadline_hours is not None:
                deadline_at = (datetime.now(UTC) + timedelta(hours=int(claim_deadline_hours))).strftime("%Y-%m-%d %H:%M:%S")
            cursor = await db.execute(
                """
                UPDATE orders
                SET status = 'claimed',
                    assigned_booster_user_id = ?,
                    claimed_at = CURRENT_TIMESTAMP,
                    deadline_at = ?,
                    deadline_reminder_sent_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'open'
                """,
                (booster_user_id, deadline_at, order_id),
            )
            if cursor.rowcount != 1:
                try:
                    await interaction.response.send_message(
                        "❌ This order has already been claimed.", ephemeral=True
                    )
                except discord.NotFound:
                    return
                return

        if order["account_username"] is not None and order["account_password"] is not None:
            dm_message = self._build_claim_dm(
                {
                    "order_number": order["order_number"],
                    "region": order["region"],
                    "current_rank": order["current_rank"],
                    "target_rank": order["target_rank"],
                    "booster_payout_cents": order["booster_payout_cents"],
                    "note": order["note"],
                    "customer_name": order["customer_name"],
                },
                str(order["account_username"]),
                str(order["account_password"]),
            )
            try:
                await interaction.user.send(dm_message)
            except discord.Forbidden:
                try:
                    await interaction.followup.send(
                        "⚠️ The order was claimed, but I could not DM the account details to you. Please enable server DMs or ask staff for the credentials.",
                        ephemeral=True,
                    )
                except discord.NotFound:
                    pass
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except discord.Forbidden:
            pass
        except discord.NotFound:
            pass

        try:
            await interaction.response.send_message(
                f"✅ Order `{order['order_number']}` was claimed successfully.",
                ephemeral=True,
            )
        except discord.NotFound:
            pass

    @app_commands.command(name="order_create", description="Creates and posts a new order.")
    @app_commands.describe(
        region="Server region, e.g. EU",
        current_rank="Current rank",
        target_rank="Target rank",
        booster_payout_eur="Booster payout in EUR",
        account_username="Account username to be sent only in the booster DM",
        account_password="Account password to be sent only in the booster DM",
        referral_bonus_eur="Referral bonus in EUR (optional)",
        customer_name="Optional customer name",
        note="Optional internal note",
        deadline_hours="Deadline in hours (optional)",
        referral_code="Optional referral code",
    )
    @staff_only()
    async def order_create(
        self,
        interaction: discord.Interaction,
        region: str,
        current_rank: str,
        target_rank: str,
        booster_payout_eur: str,
        account_username: str,
        account_password: str,
        referral_bonus_eur: str = "0",
        customer_name: str | None = None,
        note: str | None = None,
        deadline_hours: int | None = None,
        referral_code: str | None = None,
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        if channel is None:
            raise app_commands.CheckFailure("Only available in text channels.")

        try:
            booster_payout_cents = parse_eur_to_cents(booster_payout_eur)
            referral_bonus_cents = parse_eur_to_cents(referral_bonus_eur)
        except ValueError:
            await interaction.response.send_message("❌ Invalid amounts.", ephemeral=True)
            return
        if booster_payout_cents <= 0 or referral_bonus_cents < 0:
            await interaction.response.send_message(
                "❌ Amounts are invalid.", ephemeral=True
            )
            return
        if not account_username.strip() or not account_password.strip():
            await interaction.response.send_message(
                "❌ Account username and password are required.", ephemeral=True
            )
            return

        created_by_user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        order_number = generate_order_number()
        deadline_at: str | None = None
        if deadline_hours is not None:
            if deadline_hours <= 0:
                await interaction.response.send_message(
                    "❌ deadline_hours must be > 0.", ephemeral=True
                )
                return
            deadline_at = (
                datetime.now(UTC) + timedelta(hours=deadline_hours)
            ).strftime("%Y-%m-%d %H:%M:%S")

        referral_id: int | None = None
        if referral_code:
            code_row = await self.bot.db.fetchone(
                """
                SELECT rc.id, rc.owner_user_id
                FROM referral_codes rc
                WHERE rc.code = ? AND rc.is_active = 1
                """,
                (referral_code.upper().strip(),),
            )
            if code_row is None:
                await interaction.response.send_message(
                    "❌ Referral code not found.", ephemeral=True
                )
                return
            referral_id = await self.bot.db.execute(
                """
                INSERT INTO referrals (
                    referral_code_id, referrer_user_id, referred_name
                ) VALUES (?, ?, ?)
                """,
                (
                    code_row["id"],
                    code_row["owner_user_id"],
                    customer_name or "unknown-customer",
                ),
            )

        order_id = await self.bot.db.execute(
            """
            INSERT INTO orders (
                order_number,
                customer_name,
                note,
                region,
                queue_type,
                current_rank,
                target_rank,
                status,
                order_price_cents,
                booster_payout_cents,
                referral_bonus_cents,
                created_by_user_id,
                referral_id,
                account_username,
                account_password,
                claim_deadline_hours,
                deadline_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_number,
                customer_name,
                note,
                region,
                "General",
                current_rank,
                target_rank,
                booster_payout_cents,
                referral_bonus_cents,
                created_by_user_id,
                referral_id,
                account_username.strip(),
                account_password.strip(),
                deadline_hours,
                deadline_at,
            ),
        )

        embed = create_lawliet_embed(
            self.bot,
            title="🎮 New Order",
            color=THEME_COLORS["success"],
            description=f"**{current_rank} → {target_rank}**",
        )
        embed.add_field(name="Order", value=order_number, inline=True)
        embed.add_field(name="Region", value=region, inline=True)
        embed.add_field(name="Booster Payout", value=money_cents_to_eur(booster_payout_cents), inline=True)
        if referral_bonus_cents:
            embed.add_field(
                name="Referral Bonus",
                value=money_cents_to_eur(referral_bonus_cents),
                inline=True,
            )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        if deadline_at:
            embed.add_field(name="Deadline", value=deadline_at, inline=False)
        embed.set_footer(text=f"order_id:{order_id}")
        msg = await channel.send(embed=embed, view=self.claim_view)
        await self.bot.db.execute(
            """
            UPDATE orders
            SET posted_channel_id = ?, posted_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (msg.channel.id, msg.id, order_id),
        )
        await interaction.response.send_message("✅ Order posted.", ephemeral=True)
        await send_staff_audit_log(
            self.bot,
            title="Order Created",
            description=f"{member.mention} created an order.",
            fields=(
                ("Order", order_number, True),
                ("Region", region, True),
                ("Payout", money_cents_to_eur(booster_payout_cents), True),
                ("Referral", referral_code.upper().strip() if referral_code else "-", True),
                ("Note", note or "-", False),
            ),
        )

    @app_commands.command(name="my_orders", description="Shows your claimed orders.")
    @guild_only()
    async def my_orders(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        booster_user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        rows = await self.bot.db.fetchall(
            """
            SELECT order_number, current_rank, target_rank, status, booster_payout_cents
            FROM orders
            WHERE assigned_booster_user_id = ?
              AND status IN ('claimed', 'in_progress')
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (booster_user_id,),
        )
        if not rows:
            await interaction.response.send_message("You currently have no active orders.", ephemeral=True)
            return

        embed = create_lawliet_embed(
            self.bot,
            title="📋 My Active Orders",
            description="Your currently active boosting tasks.",
            color=THEME_COLORS["primary"],
        )
        for row in rows:
            embed.add_field(
                name=row["order_number"],
                value=(
                    f"{row['current_rank']} → {row['target_rank']}\n"
                    f"Status: `{row['status']}`\n"
                    f"Payout: {money_cents_to_eur(int(row['booster_payout_cents']))}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="order_complete", description="Marks an order as completed.")
    @app_commands.describe(order_number="Order number like ORD-...", proof_note="Internal note/proof")
    @guild_only()
    async def order_complete(
        self, interaction: discord.Interaction, order_number: str, proof_note: str
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")

        is_staff_actor = member.guild_permissions.administrator or any(
            role.id in (self.settings.staff_role_ids | self.settings.admin_role_ids)
            for role in member.roles
        )
        actor_user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        async with self.bot.db.transaction() as db:
            async with db.execute(
                """
                SELECT id, status, assigned_booster_user_id, booster_payout_cents, referral_id, referral_bonus_cents
                FROM orders
                WHERE order_number = ?
                """,
                (order_number.strip(),),
            ) as cursor:
                order = await cursor.fetchone()
            if order is None:
                await interaction.response.send_message("❌ Order not found.", ephemeral=True)
                return
            current_status = str(order["status"])
            if not can_transition_order(current_status, "completed"):
                await interaction.response.send_message(
                    f"❌ Transition `{current_status} -> completed` is not allowed.",
                    ephemeral=True,
                )
                return
            if order["assigned_booster_user_id"] is None:
                await interaction.response.send_message(
                    "❌ The order is not assigned to any booster.", ephemeral=True
                )
                return
            if not is_staff_actor and int(order["assigned_booster_user_id"]) != actor_user_id:
                await interaction.response.send_message(
                    "❌ Only staff or the assigned booster can complete this.",
                    ephemeral=True,
                )
                return

            await db.execute(
                """
                UPDATE orders
                SET status = 'completed',
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (order["id"],),
            )
            payout = int(order["booster_payout_cents"])
            booster_user_id = int(order["assigned_booster_user_id"])
            async with db.execute(
                "SELECT completed_orders_count FROM users WHERE id = ?",
                (booster_user_id,),
            ) as user_cursor:
                user_row = await user_cursor.fetchone()
            previous_count = int(user_row["completed_orders_count"]) if user_row is not None else 0
            new_count = previous_count + 1
            boosted_since_sql = "boosted_since = COALESCE(boosted_since, CURRENT_TIMESTAMP)," if previous_count == 0 else ""
            await db.execute(
                f"""
                UPDATE users
                SET balance_cents = balance_cents + ?,
                    total_earned_cents = total_earned_cents + ?,
                    completed_orders_count = ?,
                    {boosted_since_sql}
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payout, payout, new_count, booster_user_id),
            )
            team_bonus_cents = 0
            async with db.execute(
                "SELECT team_id FROM team_members WHERE user_id = ? LIMIT 1",
                (booster_user_id,),
            ) as member_cursor:
                team_member_row = await member_cursor.fetchone()
            if team_member_row is not None:
                team_bonus_cents = await self._apply_team_owner_bonus(db, booster_user_id, int(team_member_row["team_id"]), payout, order["id"])
            await db.execute(
                """
                INSERT INTO transactions (
                    user_id, order_id, requested_by_user_id, approved_by_user_id,
                    type, status, amount_cents, note, processed_at
                )
                VALUES (?, ?, ?, ?, 'order_payout', 'approved', ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    booster_user_id,
                    order["id"],
                    actor_user_id,
                    actor_user_id if is_staff_actor else None,
                    payout,
                    proof_note,
                ),
            )
            reached_thresholds = [
                threshold
                for threshold in self.settings.booster_completion_thresholds
                if previous_count < threshold <= new_count
            ]
            if reached_thresholds:
                member_for_announcement = interaction.guild.get_member(booster_user_id) if interaction.guild else None
                user_display_name = str(member_for_announcement) if member_for_announcement else f"User #{booster_user_id}"
                await send_booster_milestone_announcement(
                    self.bot,
                    user_display_name,
                    new_count,
                    reached_thresholds,
                )

            referral_id = order["referral_id"]
            referral_bonus = int(order["referral_bonus_cents"])
            if referral_id is not None and referral_bonus > 0:
                async with db.execute(
                    "SELECT referrer_user_id FROM referrals WHERE id = ?", (referral_id,)
                ) as ref_cursor:
                    ref = await ref_cursor.fetchone()
                if ref is not None:
                    await db.execute(
                        """
                        UPDATE referrals
                        SET total_bonus_cents = total_bonus_cents + ?,
                            successful_orders_count = successful_orders_count + 1
                        WHERE id = ?
                        """,
                        (referral_bonus, referral_id),
                    )
                    await db.execute(
                        """
                        UPDATE users
                        SET balance_cents = balance_cents + ?,
                            total_earned_cents = total_earned_cents + ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (referral_bonus, referral_bonus, ref["referrer_user_id"]),
                    )
                    await db.execute(
                        """
                        INSERT INTO transactions (
                            user_id, order_id, referral_id, requested_by_user_id, approved_by_user_id,
                            type, status, amount_cents, note, processed_at
                        )
                        VALUES (?, ?, ?, ?, ?, 'referral_bonus', 'approved', ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            ref["referrer_user_id"],
                            order["id"],
                            referral_id,
                            actor_user_id,
                            actor_user_id if is_staff_actor else None,
                            referral_bonus,
                            f"Referral bonus for {order_number}",
                        ),
                    )
                    async with db.execute(
                        "SELECT discord_id, username FROM users WHERE id = ?",
                        (ref["referrer_user_id"],),
                    ) as referrer_cursor:
                        referrer_row = await referrer_cursor.fetchone()
                    if referrer_row is not None:
                        async with db.execute(
                            "SELECT discord_id FROM users WHERE id = ?",
                            (booster_user_id,),
                        ) as booster_cursor:
                            booster_row = await booster_cursor.fetchone()
                        if booster_row is not None:
                            await send_referral_earning_notification(
                                self.bot,
                                int(referrer_row["discord_id"]),
                                int(booster_row["discord_id"]),
                            )
                        await send_referral_audit_log(
                            self.bot,
                            title="Referral Bonus Earned",
                            description="A referral bonus was paid out to a referrer.",
                            fields=(
                                ("Referrer", f"<@{int(referrer_row['discord_id'])}>", True),
                                ("Order", order_number, True),
                                ("Amount", money_cents_to_eur(int(referral_bonus)), True),
                                ("Referrer Username", str(referrer_row["username"] or "unknown"), False),
                            ),
                        )
        await interaction.response.send_message(
            f"✅ Order `{order_number}` has been completed and paid out.",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Order Completed",
            description=f"{member.mention} completed an order.",
            fields=(
                ("Order", order_number, True),
                ("Booster Payout", money_cents_to_eur(int(order["booster_payout_cents"])), True),
                ("Referral Bonus", money_cents_to_eur(int(order["referral_bonus_cents"])), True),
                ("Proof Note", proof_note, False),
            ),
        )

    @app_commands.command(
        name="order_set_status",
        description="Staff: set the order status using allowed transitions.",
    )
    @app_commands.describe(order_number="Order number", target_status="New status")
    @staff_only()
    async def order_set_status(
        self, interaction: discord.Interaction, order_number: str, target_status: str
    ) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Only available in the server.")
        normalized_target = target_status.strip().lower()
        valid_targets = {"in_progress", "cancelled", "disputed"}
        if normalized_target not in valid_targets:
            await interaction.response.send_message(
                f"❌ target_status must be one of: {', '.join(sorted(valid_targets))}.",
                ephemeral=True,
            )
            return
        row = await self.bot.db.fetchone(
            "SELECT id, status FROM orders WHERE order_number = ?",
            (order_number.strip(),),
        )
        if row is None:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return
        current_status = str(row["status"])
        if not can_transition_order(current_status, normalized_target):
            await interaction.response.send_message(
                f"❌ Transition `{current_status} -> {normalized_target}` is not allowed.",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            """
            UPDATE orders
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (normalized_target, row["id"]),
        )
        await interaction.response.send_message(
            f"✅ Status of `{order_number}` was set to `{normalized_target}`.",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Order Status Changed",
            description=f"{member.mention} changed the order status.",
            fields=(
                ("Order", order_number, True),
                ("From", current_status, True),
                ("To", normalized_target, True),
            ),
        )

    @order_create.error
    @order_complete.error
    @order_set_status.error
    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ You do not have permission for this.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ You do not have permission for this.", ephemeral=True
                )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OrdersCog(bot))
