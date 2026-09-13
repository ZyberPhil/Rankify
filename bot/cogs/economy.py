from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.services import resolve_or_create_user
from bot.utils.audit import send_staff_audit_log
from bot.utils.checks import staff_only
from bot.utils.formatting import create_lawliet_embed, money_cents_to_eur, parse_eur_to_cents, THEME_COLORS
from bot.utils.ids import generate_referral_code
from bot.utils.permissions import has_any_role


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings

    @staticmethod
    def _rank_name_from_level(level: int) -> str:
        ranks = [
            "Unranked",
            "Newcomer",
            "Reliable",
            "Veteran",
            "Elite",
            "Pro",
            "Legend",
        ]
        index = max(0, min(level, len(ranks) - 1))
        return ranks[index]

    @staticmethod
    def _next_rank_goal(level: int) -> tuple[str, int, int]:
        thresholds = [0, 5, 15, 35, 60, 100, 150]
        current = thresholds[min(level, len(thresholds) - 1)]
        if level >= len(thresholds) - 1:
            return ("Max rank reached", current, current)
        next_threshold = thresholds[level + 1]
        return (f"Level {level + 1}", current, next_threshold)

    async def build_history_embed_for_user(self, user_id: int) -> discord.Embed:
        row = await self.bot.db.fetchone(
            """
            SELECT u.username, u.booster_level, u.completed_orders_count, u.total_earned_cents,
                   u.balance_cents, u.verified_booster, u.boosted_since,
                   COALESCE((SELECT SUM(total_bonus_cents) FROM referrals WHERE referrer_user_id = u.id), 0) AS referral_earned_cents
            FROM users u
            WHERE u.id = ?
            """,
            (user_id,),
        )
        if row is None:
            raise RuntimeError("User not found.")

        completed_orders = await self.bot.db.fetchall(
            """
            SELECT order_number, current_rank, target_rank, status, booster_payout_cents, completed_at
            FROM orders
            WHERE assigned_booster_user_id = ? AND status = 'completed'
            ORDER BY completed_at DESC, created_at DESC
            LIMIT 5
            """,
            (user_id,),
        )

        level = int(row["booster_level"])
        current_rank = self._rank_name_from_level(level)
        _, current_goal, next_goal = self._next_rank_goal(level)
        progress_value = max(0, min(100, int((max(0, int(row["completed_orders_count"])) / max(1, next_goal)) * 100)))

        description_lines = []
        if completed_orders:
            description_lines.append("Recent completed orders:")
            for order in completed_orders:
                description_lines.append(f"• {order['order_number']} — {order['current_rank']} → {order['target_rank']} ({money_cents_to_eur(int(order['booster_payout_cents']))})")
        else:
            description_lines.append("No completed orders yet.")

        embed = create_lawliet_embed(
            self.bot,
            title="📜 Boost History",
            description="\n".join(description_lines),
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="Current rank", value=current_rank, inline=True)
        embed.add_field(name="Next milestone", value=f"{current_goal} / {next_goal} orders" if isinstance(next_goal, int) else str(current_goal), inline=True)
        embed.add_field(name="Progress", value=f"{progress_value}%", inline=True)
        embed.add_field(name="Completed orders", value=str(int(row["completed_orders_count"])), inline=True)
        embed.add_field(name="Total earned", value=money_cents_to_eur(int(row["total_earned_cents"])), inline=True)
        embed.add_field(name="Available balance", value=money_cents_to_eur(int(row["balance_cents"])), inline=True)
        embed.add_field(name="Referral bonus", value=money_cents_to_eur(int(row["referral_earned_cents"])), inline=True)
        embed.add_field(name="Verification", value="Verified" if int(row["verified_booster"]) else "Pending", inline=True)
        embed.add_field(name="Boosted since", value=str(row["boosted_since"]) if row["boosted_since"] else "-", inline=True)
        return embed

    async def build_full_history_embed_for_user(self, user_id: int) -> discord.Embed:
        row = await self.bot.db.fetchone(
            """
            SELECT u.username, u.booster_level, u.completed_orders_count, u.total_earned_cents,
                   u.balance_cents, u.verified_booster, u.boosted_since,
                   COALESCE((SELECT SUM(total_bonus_cents) FROM referrals WHERE referrer_user_id = u.id), 0) AS referral_earned_cents
            FROM users u
            WHERE u.id = ?
            """,
            (user_id,),
        )
        if row is None:
            raise RuntimeError("User not found.")

        completed_orders = await self.bot.db.fetchall(
            """
            SELECT order_number, current_rank, target_rank, status, booster_payout_cents, completed_at
            FROM orders
            WHERE assigned_booster_user_id = ? AND status = 'completed'
            ORDER BY completed_at DESC, created_at DESC
            """,
            (user_id,),
        )

        level = int(row["booster_level"])
        current_rank = self._rank_name_from_level(level)
        _, current_goal, next_goal = self._next_rank_goal(level)
        progress_value = max(0, min(100, int((max(0, int(row["completed_orders_count"])) / max(1, next_goal)) * 100)))

        description_lines = []
        if completed_orders:
            description_lines.append("All completed orders:")
            for order in completed_orders:
                description_lines.append(f"• {order['order_number']} — {order['current_rank']} → {order['target_rank']} ({money_cents_to_eur(int(order['booster_payout_cents']))})")
        else:
            description_lines.append("No completed orders yet.")

        embed = create_lawliet_embed(
            self.bot,
            title="📜 Full Booster History",
            description="\n".join(description_lines),
            color=THEME_COLORS["primary"],
        )
        embed.add_field(name="Current rank", value=current_rank, inline=True)
        embed.add_field(name="Next milestone", value=f"{current_goal} / {next_goal} orders" if isinstance(next_goal, int) else str(current_goal), inline=True)
        embed.add_field(name="Progress", value=f"{progress_value}%", inline=True)
        embed.add_field(name="Completed orders", value=str(int(row["completed_orders_count"])), inline=True)
        embed.add_field(name="Total earned", value=money_cents_to_eur(int(row["total_earned_cents"])), inline=True)
        embed.add_field(name="Available balance", value=money_cents_to_eur(int(row["balance_cents"])), inline=True)
        embed.add_field(name="Referral bonus", value=money_cents_to_eur(int(row["referral_earned_cents"])), inline=True)
        embed.add_field(name="Verification", value="Verified" if int(row["verified_booster"]) else "Pending", inline=True)
        embed.add_field(name="Boosted since", value=str(row["boosted_since"]) if row["boosted_since"] else "-", inline=True)
        return embed

    async def _ensure_member_user(self, interaction: discord.Interaction) -> tuple[discord.Member, int]:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            raise app_commands.CheckFailure("Guild-only command.")
        user_id = await resolve_or_create_user(
            self.bot.db, member, self.settings.staff_role_ids, self.settings.admin_role_ids
        )
        return member, user_id

    @app_commands.command(name="history", description="Shows your booster history, rank, and recent orders.")
    async def history(self, interaction: discord.Interaction) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        embed = await self.build_history_embed_for_user(user_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="history_full", description="Shows your complete booster history with all completed orders.")
    async def history_full(self, interaction: discord.Interaction) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        embed = await self.build_full_history_embed_for_user(user_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="balance", description="Shows your current balance.")
    async def balance(self, interaction: discord.Interaction) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        row = await self.bot.db.fetchone(
            """
            SELECT balance_cents, total_earned_cents, total_paid_out_cents, completed_orders_count
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        )
        if row is None:
            raise RuntimeError("User not found.")
        embed = create_lawliet_embed(
            self.bot,
            title="💰 Your Account",
            description="Your current wallet and order summary.",
            color=THEME_COLORS["warning"],
        )
        embed.add_field(name="Available", value=money_cents_to_eur(int(row["balance_cents"])))
        embed.add_field(name="Total earned", value=money_cents_to_eur(int(row["total_earned_cents"])))
        embed.add_field(name="Paid out", value=money_cents_to_eur(int(row["total_paid_out_cents"])))
        embed.add_field(name="Completed orders", value=str(row["completed_orders_count"]))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="referral_create", description="Creates your referral code.")
    async def referral_create(self, interaction: discord.Interaction) -> None:
        member, user_id = await self._ensure_member_user(interaction)
        allowed_roles = self.settings.referral_allowed_role_ids
        if allowed_roles and not has_any_role(member, allowed_roles):
            await interaction.response.send_message(
                "❌ You do not have an authorized role to create a referral link.",
                ephemeral=True,
            )
            return

        existing = await self.bot.db.fetchone(
            "SELECT code FROM referral_codes WHERE owner_user_id = ? AND is_active = 1",
            (user_id,),
        )
        if existing is not None:
            await interaction.response.send_message(
                f"✅ Your active code: `{existing['code']}`", ephemeral=True
            )
            return

        code = generate_referral_code()
        while await self.bot.db.fetchone("SELECT id FROM referral_codes WHERE code = ?", (code,)):
            code = generate_referral_code()

        await self.bot.db.execute(
            """
            INSERT INTO referral_codes (owner_user_id, code, is_active)
            VALUES (?, ?, 1)
            """,
            (user_id, code),
        )
        await interaction.response.send_message(
            f"✅ New referral code created: `{code}`",
            ephemeral=True,
        )

    @app_commands.command(name="referral_stats", description="Shows your referral statistics.")
    async def referral_stats(self, interaction: discord.Interaction) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        code_rows = await self.bot.db.fetchall(
            "SELECT code FROM referral_codes WHERE owner_user_id = ? AND is_active = 1",
            (user_id,),
        )
        if not code_rows:
            await interaction.response.send_message(
                "You do not have an active referral code yet. Use `/referral_create`.",
                ephemeral=True,
            )
            return
        stats = await self.bot.db.fetchone(
            """
            SELECT
                COALESCE(SUM(successful_orders_count), 0) AS orders_count,
                COALESCE(SUM(total_bonus_cents), 0) AS bonus_cents
            FROM referrals
            WHERE referrer_user_id = ?
            """,
            (user_id,),
        )
        embed = create_lawliet_embed(
            self.bot,
            title="👥 Referral Statistics",
            description="Your referral performance overview.",
            color=THEME_COLORS["success"],
        )
        embed.add_field(
            name="Active codes",
            value=", ".join(f"`{row['code']}`" for row in code_rows),
            inline=False,
        )
        embed.add_field(name="Successful orders", value=str(stats["orders_count"]))
        embed.add_field(name="Total bonus", value=money_cents_to_eur(int(stats["bonus_cents"])))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="withdraw_request", description="Creates a manual withdrawal request."
    )
    @app_commands.describe(amount_eur="Amount in EUR", note="Withdrawal details")
    async def withdraw_request(
        self, interaction: discord.Interaction, amount_eur: str, note: str
    ) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        try:
            amount_cents = parse_eur_to_cents(amount_eur)
        except ValueError:
            await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
            return
        if amount_cents <= 0:
            await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
            return
        row = await self.bot.db.fetchone("SELECT balance_cents FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise RuntimeError("User not found.")
        if int(row["balance_cents"]) < amount_cents:
            await interaction.response.send_message(
                f"❌ Insufficient balance. Available: {money_cents_to_eur(int(row['balance_cents']))}",
                ephemeral=True,
            )
            return
        transaction_id = await self.bot.db.execute(
            """
            INSERT INTO transactions (
                user_id, type, status, amount_cents, note, requested_by_user_id
            )
            VALUES (?, 'withdrawal_request', 'pending', ?, ?, ?)
            """,
            (user_id, -amount_cents, note, user_id),
        )
        await interaction.response.send_message(
            f"✅ Withdrawal request #{transaction_id} created.",
            ephemeral=True,
        )

    @app_commands.command(name="withdraw_approve", description="Staff: approve and process a withdrawal.")
    @app_commands.describe(transaction_id="Withdrawal transaction ID")
    @staff_only()
    async def withdraw_approve(self, interaction: discord.Interaction, transaction_id: int) -> None:
        member, staff_user_id = await self._ensure_member_user(interaction)

        async with self.bot.db.transaction() as db:
            async with db.execute(
                """
                SELECT id, user_id, status, amount_cents
                FROM transactions
                WHERE id = ? AND type = 'withdrawal_request'
                """,
                (transaction_id,),
            ) as cursor:
                tx = await cursor.fetchone()
            if tx is None:
                await interaction.response.send_message(
                    "❌ Withdrawal transaction not found.", ephemeral=True
                )
                return
            if tx["status"] != "pending":
                await interaction.response.send_message(
                    "❌ Withdrawal is no longer pending.", ephemeral=True
                )
                return
            debit_cents = abs(int(tx["amount_cents"]))
            async with db.execute("SELECT balance_cents FROM users WHERE id = ?", (tx["user_id"],)) as uc:
                user_row = await uc.fetchone()
            if user_row is None:
                await interaction.response.send_message("❌ User not found.", ephemeral=True)
                return
            if int(user_row["balance_cents"]) < debit_cents:
                await db.execute(
                    """
                    UPDATE transactions
                    SET status = 'rejected',
                        approved_by_user_id = ?,
                        note = COALESCE(note, '') || ' | Auto-Reject: insufficient balance',
                        processed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (staff_user_id, tx["id"]),
                )
                await interaction.response.send_message(
                    "❌ Request rejected: insufficient balance.",
                    ephemeral=True,
                )
                return

            await db.execute(
                """
                UPDATE users
                SET balance_cents = balance_cents - ?,
                    total_paid_out_cents = total_paid_out_cents + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (debit_cents, debit_cents, tx["user_id"]),
            )
            await db.execute(
                """
                UPDATE transactions
                SET status = 'paid',
                    approved_by_user_id = ?,
                    processed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (staff_user_id, tx["id"]),
            )

        await interaction.response.send_message(
            f"✅ Withdrawal #{transaction_id} approved and processed.",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Withdrawal Approved",
            description=f"{member.mention} approved a withdrawal.",
            fields=(
                ("Transaction", f"#{transaction_id}", True),
                ("Amount", money_cents_to_eur(debit_cents), True),
                ("User ID", str(tx["user_id"]), True),
            ),
        )

    @app_commands.command(name="withdraw_reject", description="Staff: reject a withdrawal.")
    @staff_only()
    async def withdraw_reject(
        self, interaction: discord.Interaction, transaction_id: int, reason: str
    ) -> None:
        member, staff_user_id = await self._ensure_member_user(interaction)
        tx = await self.bot.db.fetchone(
            """
            SELECT id, status
            FROM transactions
            WHERE id = ? AND type = 'withdrawal_request'
            """,
            (transaction_id,),
        )
        if tx is None:
            await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return
        if tx["status"] != "pending":
            await interaction.response.send_message("❌ Request is not pending.", ephemeral=True)
            return
        await self.bot.db.execute(
            """
            UPDATE transactions
            SET status = 'rejected',
                approved_by_user_id = ?,
                note = COALESCE(note, '') || ' | Reject reason: ' || ?,
                processed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (staff_user_id, reason, tx["id"]),
        )
        await interaction.response.send_message(
            f"✅ Withdrawal #{transaction_id} rejected.",
            ephemeral=True,
        )
        await send_staff_audit_log(
            self.bot,
            title="Withdrawal Rejected",
            description=f"{member.mention} rejected a withdrawal.",
            fields=(
                ("Transaction", f"#{transaction_id}", True),
                ("Reason", reason, False),
            ),
        )

    @app_commands.command(name="transactions", description="Shows your recent transactions.")
    async def transactions(self, interaction: discord.Interaction) -> None:
        _, user_id = await self._ensure_member_user(interaction)
        rows = await self.bot.db.fetchall(
            """
            SELECT id, type, status, amount_cents, created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id,),
        )
        if not rows:
            await interaction.response.send_message("No transactions found.", ephemeral=True)
            return
        embed = create_lawliet_embed(
            self.bot,
            title="📒 Your Transactions",
            description="Recent payment and payout activity.",
            color=THEME_COLORS["primary"],
        )
        for row in rows:
            sign = "+" if int(row["amount_cents"]) > 0 else "-"
            embed.add_field(
                name=f"#{row['id']} • {row['type']}",
                value=(
                    f"Status: `{row['status']}`\n"
                    f"Amount: {sign}{money_cents_to_eur(abs(int(row['amount_cents'])))}\n"
                    f"Date: {row['created_at']}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @withdraw_approve.error
    @withdraw_reject.error
    async def staff_command_error(
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
    await bot.add_cog(EconomyCog(bot))
