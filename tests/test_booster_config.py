import asyncio
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.cogs.admin import AdminCog
from bot.cogs.booster import BoosterCog, TeamLeaveConfirmationView, TeamSetupView, VerifyMemberRoleView
from bot.cogs.orders import ExpiredOrderActionView, OrdersCog
from bot.cogs.tickets import CloseTicketButton
from bot.config import Settings, _parse_role_ids, _parse_thresholds, add_role_id, persist_settings, remove_role_id
from bot.main import ValorantBot
from bot.db.manager import DatabaseManager
from bot.utils.ids import normalize_team_prefix
from bot.utils.permissions import has_any_role


class DummyRole:
    def __init__(self, role_id: int):
        self.id = role_id


class DummyMember:
    def __init__(self, *role_ids: int):
        self.roles = [DummyRole(role_id) for role_id in role_ids]


class BoosterConfigTests(unittest.TestCase):
    def test_parse_role_ids_ignores_empty_values(self):
        self.assertEqual(_parse_role_ids("1, 2, , 3"), {1, 2, 3})

    def test_parse_thresholds_sorts_and_deduplicates(self):
        self.assertEqual(_parse_thresholds("25, 10, 25, 50, 10"), (10, 25, 50))

    def test_has_any_role_matches_allowed_roles(self):
        member = DummyMember(10, 99)
        self.assertTrue(has_any_role(member, {99, 100}))
        self.assertFalse(has_any_role(member, {1, 2}))

    def test_add_and_remove_role_ids(self):
        roles = {10, 20}
        self.assertEqual(add_role_id(roles, 30), {10, 20, 30})
        self.assertEqual(remove_role_id({10, 20, 30}, 20), {10, 30})

    def test_booster_leaderboard_only_shows_completed_boosters_and_pages(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    for user_id, username, role_type, completed_orders_count in [
                        (11, "inactive-booster", "booster", 0),
                        (12, "active-booster", "booster", 3),
                        (13, "stale-role-booster", "staff", 2),
                    ]:
                        await db.execute(
                            """
                            INSERT INTO users (
                                discord_id, username, role_type, booster_level,
                                balance_cents, total_earned_cents, completed_orders_count,
                                boosted_since, verified_booster, rules_confirmed_at, updated_at
                            ) VALUES (?, ?, ?, 0, 0, 0, ?, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            (user_id, username, role_type, completed_orders_count),
                        )
                    fake_guild = SimpleNamespace(
                        members=[
                            SimpleNamespace(id=13, roles=[SimpleNamespace(id=999)])
                        ]
                    )
                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(
                            staff_role_ids=set(),
                            admin_role_ids=set(),
                            booster_role_id=999,
                            guild_id=42,
                        ),
                        get_guild=lambda guild_id: fake_guild,
                    )
                    cog = BoosterCog(bot)
                    embed = await cog.build_booster_leaderboard_embed(page=1, page_size=10)
                    self.assertIn("active-booster", embed.description)
                    self.assertIn("stale-role-booster", embed.description)
                    self.assertNotIn("inactive-booster", embed.description)

                    for i in range(11):
                        await db.execute(
                            """
                            INSERT INTO users (
                                discord_id, username, role_type, booster_level,
                                balance_cents, total_earned_cents, completed_orders_count,
                                boosted_since, verified_booster, rules_confirmed_at, updated_at
                            ) VALUES (?, ?, 'booster', 0, 0, 0, 1, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            (200 + i, f"page-{i}",),
                        )
                    page_two = await cog.build_booster_leaderboard_embed(page=2, page_size=10)
                    self.assertIn("page-9", page_two.description)
                    self.assertNotIn("active-booster", page_two.description)
                    self.assertNotIn("page-0", page_two.description)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_verify_rules_does_not_set_boosted_since_before_first_completed_order(self):
        async def _run() -> None:
            class DummyResponse:
                async def send_message(self, *args, **kwargs):
                    return None

            class FakeRole:
                def __init__(self, role_id):
                    self.id = role_id

            class FakeMember:
                def __init__(self):
                    self.id = 77
                    self.roles = [FakeRole(222)]
                    self.guild = None

                async def add_roles(self, role, reason=None):
                    self.roles.append(role)

                async def remove_roles(self, role, reason=None):
                    self.roles = [existing for existing in self.roles if existing.id != role.id]

            class FakeGuild:
                def get_role(self, role_id):
                    return FakeRole(role_id)

            calls = []

            async def fake_execute(sql, params=()):
                calls.append((sql, params))
                return 1

            bot = SimpleNamespace(
                db=SimpleNamespace(execute=fake_execute),
                settings=SimpleNamespace(
                    booster_role_id=999,
                    booster_approval_role_id=222,
                    staff_role_ids=set(),
                    admin_role_ids=set(),
                ),
            )
            member = FakeMember()
            member.guild = FakeGuild()
            interaction = SimpleNamespace(
                user=member,
                response=DummyResponse(),
            )
            cog = BoosterCog(bot)
            callback = cog.__class__.verify_rules.callback
            with patch("bot.cogs.booster.discord.Member", FakeMember), patch(
                "bot.cogs.booster.resolve_or_create_user", AsyncMock(return_value=42)
            ):
                await callback(cog, interaction)

            self.assertTrue(any("UPDATE users" in sql for sql, _ in calls))
            self.assertFalse(any("boosted_since = COALESCE(boosted_since, CURRENT_TIMESTAMP)" in sql for sql, _ in calls))
            self.assertEqual([role.id for role in member.roles], [999])

        asyncio.run(_run())

    def test_persist_settings_serializes_nullable_values(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    settings = Settings(
                        discord_token="token",
                        guild_id=None,
                        database_path=db_path,
                        staff_role_ids={1},
                        admin_role_ids=set(),
                        referral_allowed_role_ids=set(),
                        booster_completion_channel_id=None,
                        booster_completion_thresholds=(10, 25),
                        command_only_channel_ids=set(),
                        booster_role_id=None,
                        verification_role_id=None,
                        verify_channel_id=None,
                        booster_leaderboard_channel_id=None,
                        team_leaderboard_channel_id=None,
                        support_ticket_category_id=None,
                        payout_ticket_category_id=None,
                        application_ticket_category_id=None,
                        application_review_channel_id=None,
                        staff_audit_log_channel_id=None,
                        referral_log_channel_id=None,
                        expired_order_action_channel_id=None,
                        team_creation_role_id=None,
                        team_setup_channel_id=999,
                    )
                    await persist_settings(db, settings)
                    row = await db.fetchone("SELECT value FROM bot_settings WHERE name = 'guild_id'")
                    self.assertIsNotNone(row)
                    self.assertEqual(row[0], "")
                    setup_row = await db.fetchone("SELECT value FROM bot_settings WHERE name = 'team_setup_channel_id'")
                    self.assertIsNotNone(setup_row)
                    self.assertEqual(setup_row[0], "999")
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_single_team_membership_when_assigning_new_team(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (42, "member-one"),
                    )
                    team_a = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha", "ALP", "ALPHA1", user_id),
                    )
                    team_b = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Bravo", "BRV", "BRAVO1", user_id),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_a, user_id),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)
                    await cog._attach_user_to_team(user_id, team_b)
                    rows = await db.fetchall(
                        "SELECT team_id FROM team_members WHERE user_id = ? ORDER BY team_id",
                        (user_id,),
                    )
                    self.assertEqual([int(row["team_id"]) for row in rows], [team_b])
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_invite_same_team_is_ignored_without_unique_constraint_error(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (99, "same-team-member"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("SameTeam", "STM", "SAMETEAM", user_id),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_id, user_id),
                    )

                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)
                    await cog._attach_user_to_team(user_id, team_id)

                    rows = await db.fetchall(
                        "SELECT team_id FROM team_members WHERE user_id = ? ORDER BY team_id",
                        (user_id,),
                    )
                    self.assertEqual([int(row["team_id"]) for row in rows], [team_id])
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_list_handles_expired_interaction_without_404(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.sent = []
                    self.is_done = lambda: False

                async def send_message(self, *args, **kwargs):
                    self.sent.append((args, kwargs))
                    raise discord.NotFound(SimpleNamespace(), {"code": 10062, "message": "Unknown interaction"})

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, NULL)",
                        ("Alpha", "ALP", "ALPHA1"),
                    )
                    await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (101, "member-a"),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (1, 1),
                    )

                    bot = SimpleNamespace(db=db, settings=SimpleNamespace(staff_role_ids=set(), admin_role_ids=set()))
                    cog = BoosterCog(bot)
                    interaction = SimpleNamespace(response=FakeResponse())
                    await cog.team_list.callback(cog, interaction)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_order_claim_dm_contains_account_credentials_but_public_order_embed_does_not(self):
        bot = SimpleNamespace(settings=SimpleNamespace())
        cog = OrdersCog(bot)
        order = {
            "order_number": "ORD-123",
            "current_rank": "Silver",
            "target_rank": "Gold",
            "booster_payout_cents": 5000,
            "note": "Account boost",
        }
        message = cog._build_claim_dm(order, "boost_user", "boost_pass")
        self.assertIn("boost_user", message)
        self.assertIn("boost_pass", message)
        self.assertNotIn("boost_pass", str(cog._build_order_embed(order)))

    def test_verify_dashboard_shows_member_next_steps(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 0, NULL, CURRENT_TIMESTAMP)",
                        (99, "member-status"),
                    )
                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(booster_role_id=777, staff_role_ids=set(), admin_role_ids=set()),
                    )
                    cog = BoosterCog(bot)
                    member = SimpleNamespace(id=99, roles=[SimpleNamespace(id=999)])
                    embed = await cog._build_verify_dashboard_embed(member)
                    self.assertIn("Member", str(embed.fields[0].value))
                    self.assertTrue(any("/verify_rules" in str(field.value) for field in embed.fields))
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_verify_rules_dashboard_uses_clickable_button(self):
        from bot.cogs.booster import VerifyRulesView

        bot = SimpleNamespace(
            settings=SimpleNamespace(
                verify_channel_id=123,
                booster_role_id=777,
                booster_approval_role_id=999,
                staff_role_ids=set(),
                admin_role_ids=set(),
            ),
            get_channel=lambda channel_id: SimpleNamespace(id=channel_id, send=AsyncMock()),
        )
        cog = BoosterCog(bot)
        view = VerifyRulesView(cog)
        self.assertTrue(any(getattr(item, "custom_id", None) == "booster_access:accept" for item in view.children))
        self.assertTrue(any(getattr(item, "label", None) == "Accept booster access" for item in view.children))

    def test_verify_dashboard_message_uses_member_role_button(self):
        async def _run() -> None:
            bot = SimpleNamespace(settings=SimpleNamespace(verification_role_id=555))
            cog = BoosterCog(bot)
            view = VerifyMemberRoleView(cog)
            self.assertEqual(len(view.children), 1)
            self.assertEqual(view.children[0].label, "Get member role")

        asyncio.run(_run())

    def test_verify_member_role_button_handles_permission_error(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.message = None

                async def send_message(self, message, ephemeral=False):
                    self.message = (message, ephemeral)

            class FakeRole:
                def __init__(self):
                    self.id = 555
                    self.mention = "@Member"

            class FakeMember:
                def __init__(self):
                    self.id = 42
                    self.roles = []
                    self.add_roles = AsyncMock(side_effect=discord.Forbidden(
                        response=SimpleNamespace(status=403, reason="Forbidden"),
                        message="Missing Permissions",
                    ))

            interaction = SimpleNamespace(
                user=FakeMember(),
                guild=SimpleNamespace(get_role=lambda role_id: FakeRole() if role_id == 555 else None),
                response=FakeResponse(),
            )
            with patch("bot.cogs.booster.discord.Member", FakeMember):
                cog = BoosterCog(SimpleNamespace(settings=SimpleNamespace(verification_role_id=555)))
                view = VerifyMemberRoleView(cog)
                await view.claim_member_role.callback(interaction)
            self.assertEqual(interaction.response.message[1], True)
            self.assertIn("Manage Roles", interaction.response.message[0])

        asyncio.run(_run())

    def test_welcome_and_member_exit_logs_use_staff_channel(self):
        async def _run() -> None:
            class FakeTextChannel:
                def __init__(self):
                    self.send = AsyncMock()

            class FakeChannel:
                def __init__(self):
                    self.send = AsyncMock()

            class FakeMember:
                def __init__(self):
                    self.id = 42
                    self.name = "joined-user"
                    self.mention = "@joined-user"
                    self.bot = False
                    self.guild = SimpleNamespace(name="Rankify", members=[self])
                    self.display_avatar = SimpleNamespace(url="https://example.com/avatar.png")

            class FakeAuditEntry:
                def __init__(self, action, user, target, reason, created_at):
                    self.action = action
                    self.user = user
                    self.target = target
                    self.reason = reason
                    self.created_at = created_at

            class FakeGuild:
                def __init__(self, member):
                    self.name = "Rankify"
                    self.members = [member]
                    self._audit_entries = [
                        FakeAuditEntry(discord.AuditLogAction.kick, SimpleNamespace(mention="@mod", id=77), member, "spam", datetime.now(UTC) - timedelta(minutes=1)),
                    ]

                def audit_logs(self, limit):
                    async def _gen():
                        for entry in self._audit_entries[:limit]:
                            yield entry
                    return _gen()

            channel = FakeTextChannel()
            guild = FakeGuild(None)
            member = FakeMember()
            guild._audit_entries[0].target = member
            member.guild = guild
            guild.members = [member]

            bot = SimpleNamespace(
                settings=SimpleNamespace(staff_audit_log_channel_id=321, welcome_channel_id=123),
                get_channel=lambda channel_id: channel if channel_id in {123, 321} else None,
            )
            bot.user = SimpleNamespace(display_avatar=SimpleNamespace(url="https://example.com/bot.png"))

            settings = Settings(
                discord_token="token",
                guild_id=1,
                database_path=Path("./data/test.db"),
                staff_role_ids=set(),
                admin_role_ids=set(),
                referral_allowed_role_ids=set(),
                team_creation_role_id=None,
                team_setup_channel_id=None,
                booster_completion_channel_id=None,
                booster_completion_thresholds=(),
                command_only_channel_ids=set(),
                booster_role_id=None,
                verification_role_id=None,
                verify_channel_id=None,
                booster_leaderboard_channel_id=None,
                team_leaderboard_channel_id=None,
                support_ticket_category_id=None,
                payout_ticket_category_id=None,
                application_ticket_category_id=None,
                application_review_channel_id=None,
                staff_audit_log_channel_id=321,
                referral_log_channel_id=None,
                expired_order_action_channel_id=None,
                welcome_channel_id=123,
            )
            app = ValorantBot(settings)
            app.get_channel = lambda channel_id: channel if channel_id in {123, 321} else None
            app.settings = settings
            app._user = bot.user
            with patch("bot.main.discord.TextChannel", FakeTextChannel):
                await app.on_member_join(member)
                await app.on_member_remove(member)
            self.assertEqual(channel.send.await_count, 2)

        asyncio.run(_run())

    def test_member_join_only_sends_one_welcome_message(self):
        async def _run() -> None:
            class FakeTextChannel:
                def __init__(self):
                    self.send = AsyncMock()

            class FakeMember:
                def __init__(self):
                    self.id = 42
                    self.name = "joined-user"
                    self.mention = "@joined-user"
                    self.bot = False
                    self.guild = SimpleNamespace(name="Rankify", members=[self])
                    self.display_avatar = SimpleNamespace(url="https://example.com/avatar.png")

            channel = FakeTextChannel()
            member = FakeMember()
            settings = Settings(
                discord_token="token",
                guild_id=1,
                database_path=Path("./data/test.db"),
                staff_role_ids=set(),
                admin_role_ids=set(),
                referral_allowed_role_ids=set(),
                team_creation_role_id=None,
                team_setup_channel_id=None,
                booster_completion_channel_id=None,
                booster_completion_thresholds=(),
                command_only_channel_ids=set(),
                booster_role_id=None,
                verification_role_id=None,
                verify_channel_id=None,
                booster_leaderboard_channel_id=None,
                team_leaderboard_channel_id=None,
                support_ticket_category_id=None,
                payout_ticket_category_id=None,
                application_ticket_category_id=None,
                application_review_channel_id=None,
                staff_audit_log_channel_id=None,
                referral_log_channel_id=None,
                expired_order_action_channel_id=None,
                welcome_channel_id=123,
            )
            app = ValorantBot(settings)
            app.get_channel = lambda channel_id: channel if channel_id == 123 else None
            app.settings = settings
            with patch("bot.main.discord.TextChannel", FakeTextChannel):
                await app.on_member_join(member)
                await app.on_member_join(member)
            self.assertEqual(channel.send.await_count, 1)

        asyncio.run(_run())

    def test_ready_only_starts_background_loops_once(self):
        async def _run() -> None:
            settings = Settings(
                discord_token="token",
                guild_id=1,
                database_path=Path("./data/test.db"),
                staff_role_ids=set(),
                admin_role_ids=set(),
                referral_allowed_role_ids=set(),
                team_creation_role_id=None,
                team_setup_channel_id=None,
                booster_completion_channel_id=None,
                booster_completion_thresholds=(),
                command_only_channel_ids=set(),
                booster_role_id=None,
                verification_role_id=None,
                verify_channel_id=None,
                booster_leaderboard_channel_id=None,
                team_leaderboard_channel_id=None,
                support_ticket_category_id=None,
                payout_ticket_category_id=None,
                application_ticket_category_id=None,
                application_review_channel_id=None,
                staff_audit_log_channel_id=None,
                referral_log_channel_id=None,
                expired_order_action_channel_id=None,
                welcome_channel_id=None,
            )
            app = ValorantBot(settings)
            app._leaderboard_loop = MagicMock()
            app._order_deadline_loop = MagicMock()
            app.loop = SimpleNamespace(create_task=MagicMock())

            await app.on_ready()
            await app.on_ready()

            self.assertEqual(app.loop.create_task.call_count, 2)
            self.assertEqual(app._background_loops_started, True)

        asyncio.run(_run())

    def test_verify_dashboard_posts_in_configured_channel_for_everyone(self):
        async def _run() -> None:
            class FakeTextChannel:
                def __init__(self):
                    self.mention = "#verify"
                    self.send = AsyncMock()

            class FakeResponse:
                def __init__(self):
                    self.messages = []
                    self.send_message = AsyncMock()

            channel = FakeTextChannel()
            bot = SimpleNamespace(
                settings=SimpleNamespace(verification_role_id=555, verify_channel_id=123),
                get_channel=lambda channel_id: channel if channel_id == 123 else None,
            )
            cog = BoosterCog(bot)
            class FakeMember:
                def __init__(self):
                    self.id = 42
                    self.roles = []

            member = FakeMember()
            interaction = SimpleNamespace(
                user=member,
                response=FakeResponse(),
            )
            with patch("bot.cogs.booster.discord.Member", FakeMember), patch("bot.cogs.booster.discord.TextChannel", FakeTextChannel):
                await cog.verify_dashboard.callback(cog, interaction)
            channel.send.assert_awaited_once()
            interaction.response.send_message.assert_awaited_once()
            self.assertIn("#verify", str(interaction.response.send_message.await_args.args[0]))

        asyncio.run(_run())

    def test_cog_load_recreates_public_dashboards_when_missing(self):
        async def _run() -> None:
            class FakeTextChannel:
                def __init__(self, channel_id):
                    self.id = channel_id
                    self.send = AsyncMock()
                    self.history = AsyncMock(return_value=[])
                    self.mention = f"#channel-{channel_id}"
                    self.embeds = []
                    self.author = SimpleNamespace(id=111)

            bot = SimpleNamespace(
                settings=SimpleNamespace(
                    team_setup_channel_id=None,
                    verification_role_id=555,
                    verify_channel_id=123,
                    booster_verify_channel_id=456,
                ),
                user=SimpleNamespace(id=999),
                get_channel=lambda channel_id: {
                    123: FakeTextChannel(123),
                    456: FakeTextChannel(456),
                }.get(channel_id),
                add_view=MagicMock(),
            )
            bot.db = SimpleNamespace(
                fetchone=AsyncMock(return_value=None),
                execute=AsyncMock(),
            )
            cog = BoosterCog(bot)
            cog._refresh_runtime_settings = AsyncMock()
            cog._post_public_verify_dashboard = AsyncMock(return_value=None)
            cog._post_public_rules_dashboard = AsyncMock(return_value=None)

            with patch("bot.cogs.booster.discord.TextChannel", FakeTextChannel):
                await cog.cog_load()

            self.assertEqual(cog._post_public_verify_dashboard.call_count, 1)
            self.assertEqual(cog._post_public_rules_dashboard.call_count, 1)

        asyncio.run(_run())

    def test_admin_setting_update_refreshes_live_cog_settings(self):
        settings = Settings(
            discord_token="token",
            guild_id=1,
            database_path=Path("./data/test.db"),
            staff_role_ids=set(),
            admin_role_ids=set(),
            referral_allowed_role_ids=set(),
            team_creation_role_id=None,
            team_setup_channel_id=None,
            booster_completion_channel_id=None,
            booster_completion_thresholds=(),
            command_only_channel_ids=set(),
            booster_role_id=None,
            verification_role_id=None,
            verify_channel_id=None,
            booster_leaderboard_channel_id=None,
            team_leaderboard_channel_id=None,
            support_ticket_category_id=None,
            payout_ticket_category_id=None,
            application_ticket_category_id=None,
            application_review_channel_id=None,
            staff_audit_log_channel_id=None,
            referral_log_channel_id=None,
            expired_order_action_channel_id=None,
        )
        bot = SimpleNamespace(settings=settings, db=None)
        bot.get_cog = lambda name: {"BoosterCog": booster_cog, "OrdersCog": None, "TicketsCog": None}.get(name)
        booster_cog = BoosterCog(bot)
        admin_cog = AdminCog(bot)

        admin_cog._apply_settings(verification_role_id=555, verify_channel_id=777)

        self.assertEqual(bot.settings.verification_role_id, 555)
        self.assertEqual(bot.settings.verify_channel_id, 777)
        self.assertEqual(admin_cog.settings.verification_role_id, 555)
        self.assertEqual(booster_cog.settings.verification_role_id, 555)
        self.assertEqual(booster_cog.settings.verify_channel_id, 777)

    def test_claim_order_ignores_unknown_interaction_after_message_delete(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    member_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (111, "claim-user"),
                    )
                    order_id = await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, claim_deadline_hours, account_username, account_password, posted_channel_id, posted_message_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 24, 'user', 'pass', 1, 2)",
                        ("ORD-CLAIM", "Customer", "EU", "Ranked", "Silver", "Gold", 1000, 1000, 50, member_user_id),
                    )
                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(staff_role_ids=set(), admin_role_ids=set()),
                    )
                    cog = OrdersCog(bot)

                    class FakeResponse:
                        def __init__(self):
                            self.send_message = AsyncMock(
                                side_effect=discord.NotFound(
                                    SimpleNamespace(status=404, reason="Not Found"),
                                    "Unknown interaction",
                                )
                            )

                    class FakeMessage:
                        def __init__(self):
                            self.embeds = []
                            self.delete = AsyncMock()

                    class FakeGuild:
                        pass

                    class FakeMember:
                        def __init__(self):
                            self.id = 999
                            self.roles = []
                            self.guild_permissions = SimpleNamespace(administrator=False)

                    interaction = SimpleNamespace(
                        user=FakeMember(),
                        response=FakeResponse(),
                        message=FakeMessage(),
                        guild=FakeGuild(),
                    )
                    interaction.user.send = AsyncMock()
                    with patch("bot.cogs.orders.discord.Member", FakeMember):
                        await cog.claim_order(interaction)
                    self.assertTrue(True)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_expired_claimed_order_sends_staff_action_message(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    booster_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (123, "expired-booster"),
                    )
                    created_by_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'staff', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (456, "staff-user"),
                    )
                    order_id = await db.execute(
                        "INSERT INTO orders (order_number, customer_name, note, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, claim_deadline_hours, deadline_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', 0, 10000, 0, ?, ?, 24, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        ("ORD-EXP-1", "Customer", "Test", "EU", "General", "Silver", "Gold", created_by_id, booster_id, (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    channel = SimpleNamespace(send=AsyncMock())
                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(
                            staff_role_ids=set(),
                            admin_role_ids=set(),
                            expired_order_action_channel_id=999,
                            team_creation_role_id=None,
                            team_setup_channel_id=None,
                            booster_completion_channel_id=None,
                            booster_completion_thresholds=(10, 25),
                            command_only_channel_ids=set(),
                            booster_role_id=None,
                            verification_role_id=None,
                            verify_channel_id=None,
                            booster_leaderboard_channel_id=None,
                            team_leaderboard_channel_id=None,
                            support_ticket_category_id=None,
                            payout_ticket_category_id=None,
                            application_ticket_category_id=None,
                            application_review_channel_id=None,
                            staff_audit_log_channel_id=None,
                            referral_log_channel_id=None,
                        ),
                        get_channel=lambda channel_id: channel if channel_id == 999 else None,
                        get_user=lambda user_id: None,
                        user=None,
                    )
                    cog = OrdersCog(bot)
                    await cog._check_claimed_order_deadlines()
                    channel.send.assert_awaited_once()
                    row = await db.fetchone("SELECT status, assigned_booster_user_id, deadline_at FROM orders WHERE id = ?", (order_id,))
                    self.assertEqual(row["status"], "claimed")
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_expired_order_action_reassign_uses_connection_cursor(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    booster_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (901, "expired-booster"),
                    )
                    order_id = await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, claimed_at, deadline_at, note) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
                        ("ORD-EXPIRED", "Customer", "EU", "Ranked", "Silver", "Gold", 1000, 1000, 50, 1, booster_user_id, "Waiting for follow up"),
                    )

                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(staff_role_ids={1}, admin_role_ids=set()),
                    )
                    cog = OrdersCog(bot)
                    view = ExpiredOrderActionView(cog, order_id, booster_user_id, "ORD-EXPIRED")

                    class FakePermissions:
                        administrator = True

                    class FakeMember:
                        def __init__(self):
                            self.guild_permissions = FakePermissions()
                            self.roles = []
                            self.id = 123

                    class FakeResponse:
                        def __init__(self):
                            self.send_message = AsyncMock()

                    class FakeMessage:
                        def __init__(self):
                            self.delete = AsyncMock()

                    interaction = SimpleNamespace(
                        user=SimpleNamespace(guild_permissions=FakePermissions(), roles=[], id=123),
                        response=FakeResponse(),
                        message=FakeMessage(),
                    )

                    await view._perform_action(interaction, "reassign")
                    row = await db.fetchone("SELECT status, assigned_booster_user_id FROM orders WHERE id = ?", (order_id,))
                    self.assertEqual(row["status"], "claimed")
                    self.assertEqual(int(row["assigned_booster_user_id"]), booster_user_id)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_kick_button_only_allows_owner(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    owner_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (10, "owner-user"),
                    )
                    member_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (20, "member-user"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha", "ALP", "ALPHA1", owner_user_id),
                    )
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_id, owner_user_id))
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_id, member_user_id))

                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)

                    class FakeResponse:
                        def __init__(self):
                            self.send_message = AsyncMock()
                            self.send_modal = AsyncMock()

                    member = SimpleNamespace(id=20)
                    interaction = SimpleNamespace(user=member, response=FakeResponse())

                    await cog.handle_team_setup(interaction, "kick")

                    interaction.response.send_message.assert_awaited_once()
                    interaction.response.send_modal.assert_not_called()
                    args, _ = interaction.response.send_message.await_args
                    self.assertIn("Only the team owner can kick members", args[0])
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_owner_bonus_uses_transaction_cursor_for_fetchone(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    member_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (42, "boosting-member"),
                    )
                    owner_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (99, "team-owner"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha", "ALP", "ALPHA1", owner_user_id),
                    )
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_id, member_user_id))
                    order_id = await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, deadline_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, NULL)",
                        ("ORD-123", "Customer", "EU", "Ranked", "Gold", "Platinum", 1000, 1000, 0, 1),
                    )

                    bot = SimpleNamespace(settings=SimpleNamespace())
                    cog = OrdersCog(bot)
                    async with db.transaction() as connection:
                        result = await cog._apply_team_owner_bonus(connection, member_user_id, team_id, 1000, order_id)
                        self.assertEqual(result, 50)

                    owner_row = await db.fetchone("SELECT balance_cents, total_earned_cents FROM users WHERE id = ?", (owner_user_id,))
                    self.assertEqual(int(owner_row["balance_cents"]), 50)
                    self.assertEqual(int(owner_row["total_earned_cents"]), 50)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_membership_has_unique_user_constraint(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (101, "multi-team-user"),
                    )
                    team_a = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha", "ALP", "ALPHA1", user_id),
                    )
                    team_b = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Bravo", "BRV", "BRAVO1", user_id),
                    )
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_a, user_id))
                    with self.assertRaises(Exception):
                        await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_b, user_id))
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_staff_team_management_updates_name_prefix_and_member_removal(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    owner_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (555, "owner-user"),
                    )
                    member_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (777, "member-user"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Bad Name", "BAD", "TEAMCODE", owner_user_id),
                    )
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_id, owner_user_id))
                    await db.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (team_id, member_user_id))

                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)
                    await cog._staff_manage_team(team_id, new_name="Clean Name", new_prefix="CLN", member_to_remove_id=member_user_id)

                    team = await db.fetchone("SELECT name, prefix FROM teams WHERE id = ?", (team_id,))
                    self.assertEqual(team["name"], "Clean Name")
                    self.assertEqual(team["prefix"], "CLN")
                    remaining = await db.fetchall("SELECT user_id FROM team_members WHERE team_id = ? ORDER BY user_id", (team_id,))
                    self.assertEqual([int(row["user_id"]) for row in remaining], [owner_user_id])
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_runtime_settings_reload_uses_persisted_team_setup_channel(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    stale_settings = Settings(
                        discord_token="token",
                        guild_id=42,
                        database_path=db_path,
                        staff_role_ids=set(),
                        admin_role_ids=set(),
                        referral_allowed_role_ids=set(),
                        team_creation_role_id=None,
                        team_setup_channel_id=None,
                        booster_completion_channel_id=None,
                        booster_completion_thresholds=(10, 25),
                        command_only_channel_ids=set(),
                        booster_role_id=None,
                        verification_role_id=None,
                        verify_channel_id=None,
                        booster_leaderboard_channel_id=None,
                        team_leaderboard_channel_id=None,
                        support_ticket_category_id=None,
                        payout_ticket_category_id=None,
                        application_ticket_category_id=None,
                        application_review_channel_id=None,
                        staff_audit_log_channel_id=None,
                        referral_log_channel_id=None,
                        expired_order_action_channel_id=None,
                    )
                    await persist_settings(db, Settings(
                        discord_token="token",
                        guild_id=42,
                        database_path=db_path,
                        staff_role_ids=set(),
                        admin_role_ids=set(),
                        referral_allowed_role_ids=set(),
                        team_creation_role_id=None,
                        team_setup_channel_id=987654321,
                        booster_completion_channel_id=None,
                        booster_completion_thresholds=(10, 25),
                        command_only_channel_ids=set(),
                        booster_role_id=None,
                        verification_role_id=None,
                        verify_channel_id=None,
                        booster_leaderboard_channel_id=None,
                        team_leaderboard_channel_id=None,
                        support_ticket_category_id=None,
                        payout_ticket_category_id=None,
                        application_ticket_category_id=None,
                        application_review_channel_id=None,
                        staff_audit_log_channel_id=None,
                        referral_log_channel_id=None,
                        expired_order_action_channel_id=None,
                    ))
                    bot = SimpleNamespace(db=db, settings=stale_settings)
                    cog = BoosterCog(bot)
                    self.assertIsNone(cog.settings.team_setup_channel_id)
                    await cog._refresh_runtime_settings()
                    self.assertEqual(cog.settings.team_setup_channel_id, 987654321)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_staff_team_list_and_delete_flow(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    owner_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (1001, "team-owner"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha Squad", "ALP", "ALPHA1", owner_user_id),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_id, owner_user_id),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)
                    rows = await cog._list_all_teams_for_staff()
                    self.assertTrue(any(row["name"] == "Alpha Squad" for row in rows))

                    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
                    await cog.team_delete.callback(cog, interaction, "Alpha Squad")
                    self.assertIsNone(await db.fetchone("SELECT id FROM teams WHERE name = ?", ("Alpha Squad",)))
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_leave_panel_has_leave_button_and_owner_leave_deletes_team(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    owner_user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (2001, "team-owner"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha Squad", "ALP", "ALPHA1", owner_user_id),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_id, owner_user_id),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = BoosterCog(bot)
                    view = TeamSetupView(cog)
                    self.assertIn("team_setup_leave", {item.custom_id for item in view.children if hasattr(item, "custom_id")})

                    owner = SimpleNamespace(id=2001, roles=[])
                    interaction = SimpleNamespace(
                        user=owner,
                        response=SimpleNamespace(send_message=AsyncMock()),
                    )
                    await cog.handle_team_setup(interaction, "leave")
                    interaction.response.send_message.assert_awaited()

                    confirm_view = TeamLeaveConfirmationView(cog, {"id": team_id, "name": "Alpha Squad", "prefix": "ALP", "created_by_user_id": owner_user_id})
                    confirm_button = next(item for item in confirm_view.children if getattr(item, "custom_id", None) == "team_leave_confirm")
                    confirm_interaction = SimpleNamespace(
                        user=owner,
                        response=SimpleNamespace(send_message=AsyncMock()),
                    )
                    await confirm_button.callback(confirm_interaction)
                    self.assertIsNone(await db.fetchone("SELECT id FROM teams WHERE name = ?", ("Alpha Squad",)))
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_owner_gets_five_percent_of_member_boost_payout(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    owner_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (5001, "owner"),
                    )
                    member_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (5002, "member"),
                    )
                    team_id = await db.execute(
                        "INSERT INTO teams (name, prefix, referral_code, created_by_user_id) VALUES (?, ?, ?, ?)",
                        ("Alpha", "ALP", "ALPHA2", owner_id),
                    )
                    await db.execute(
                        "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                        (team_id, member_id),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    cog = OrdersCog(bot)
                    bonus_cents = await cog._apply_team_owner_bonus(db, member_id, team_id, 10000, 1)
                    self.assertEqual(bonus_cents, 500)
                    owner_row = await db.fetchone("SELECT balance_cents, total_earned_cents FROM users WHERE id = ?", (owner_id,))
                    self.assertEqual(int(owner_row["balance_cents"]), 500)
                    self.assertEqual(int(owner_row["total_earned_cents"]), 500)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_prefix_is_sanitized_to_three_chars(self):
        self.assertEqual(normalize_team_prefix("alpha"), "ALP")
        self.assertEqual(normalize_team_prefix("a1b2"), "A1B")
        self.assertEqual(normalize_team_prefix("!a!"), "A")

    def test_team_label_uses_prefix_before_name(self):
        self.assertEqual(BoosterCog.format_team_label("ALP", "Alpha"), "[ALP] Alpha")
        self.assertEqual(BoosterCog.format_team_label("", "Alpha"), "Alpha")

    def test_team_setup_view_buttons_are_persistent(self):
        from bot.cogs.booster import TeamSetupView

        fake_cog = SimpleNamespace()
        panel = TeamSetupView(fake_cog)
        self.assertIsNone(panel.timeout)
        custom_ids = {item.custom_id for item in panel.children if hasattr(item, "custom_id")}
        self.assertEqual(len(custom_ids), 5)
        self.assertTrue(all(custom_id for custom_id in custom_ids))

    def test_ticket_close_view_has_complete_order_button(self):
        fake_cog = SimpleNamespace()
        view = CloseTicketButton(fake_cog)
        custom_ids = {item.custom_id for item in view.children if hasattr(item, "custom_id")}
        self.assertIn("ticket:complete_order", custom_ids)
        self.assertTrue(any(getattr(item, "label", None) == "Complete Order" for item in view.children))

    def test_same_booster_and_approval_role_ids_do_not_duplicate_or_remove_role(self):
        async def _run() -> None:
            class FakeRole:
                def __init__(self, role_id):
                    self.id = role_id

                def __eq__(self, other):
                    return isinstance(other, FakeRole) and self.id == other.id

            class FakeMember:
                def __init__(self, member_id, role_ids=None):
                    self.id = member_id
                    self.mention = f"<@{member_id}>"
                    self.roles = [FakeRole(role_id) for role_id in (role_ids or [])]

                async def add_roles(self, role, reason=None):
                    if all(existing.id != role.id for existing in self.roles):
                        self.roles.append(role)

                async def remove_roles(self, role, reason=None):
                    self.roles = [existing for existing in self.roles if existing.id != role.id]

            class FakeGuild:
                def __init__(self):
                    self._roles = {111: FakeRole(111)}

                def get_role(self, role_id):
                    return self._roles.get(role_id)

            class FakeDB:
                async def execute(self, *args, **kwargs):
                    return 1

            bot = SimpleNamespace(
                db=FakeDB(),
                settings=SimpleNamespace(
                    booster_role_id=111,
                    booster_approval_role_id=111,
                    staff_role_ids=set(),
                    admin_role_ids=set(),
                ),
            )
            guild = FakeGuild()
            member = FakeMember(77, [111])
            interaction = SimpleNamespace(
                user=member,
                guild=guild,
                response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
                channel=None,
            )
            cog = BoosterCog(bot)
            with patch("bot.cogs.booster.discord.Member", FakeMember), patch(
                "bot.cogs.booster.resolve_or_create_user", AsyncMock(return_value=42)
            ):
                await cog.verify_rules.callback(cog, interaction)
            self.assertEqual([role.id for role in member.roles], [111])

        asyncio.run(_run())

    def test_rules_confirmation_requires_approval_role(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.sent = None
                    self.is_done = lambda: False

                async def send_message(self, message, ephemeral=False):
                    self.sent = (message, ephemeral)

            class FakeMember:
                def __init__(self, member_id, role_ids=None):
                    self.id = member_id
                    self.mention = f"<@{member_id}>"
                    self.roles = [SimpleNamespace(id=role_id) for role_id in (role_ids or [])]
                    self.guild = SimpleNamespace()

                async def add_roles(self, role, reason=None):
                    self.roles.append(SimpleNamespace(id=role.id))

            class FakeGuild:
                def __init__(self):
                    self._roles = {111: SimpleNamespace(id=111), 222: SimpleNamespace(id=222)}

                def get_role(self, role_id):
                    return self._roles.get(role_id)

            class FakeMemberClass(FakeMember):
                pass

            class FakeDiscordMember(FakeMemberClass):
                pass

            class FakeDB:
                async def fetchone(self, query, params=()):
                    if "SELECT verified_booster, rules_confirmed_at" in query:
                        return {"verified_booster": 1, "rules_confirmed_at": "2024-01-01 00:00:00"}
                    if "SELECT id FROM users" in query:
                        return {"id": 42}
                    return None

                async def execute(self, *args, **kwargs):
                    return 1

            bot = SimpleNamespace(
                db=FakeDB(),
                settings=SimpleNamespace(
                    booster_role_id=111,
                    booster_approval_role_id=222,
                    staff_role_ids=set(),
                    admin_role_ids=set(),
                ),
            )
            guild = FakeGuild()
            member = FakeDiscordMember(77, [])
            interaction = SimpleNamespace(
                user=member,
                guild=guild,
                response=FakeResponse(),
                channel=None,
            )
            cog = BoosterCog(bot)
            cog.settings = bot.settings
            with patch("bot.cogs.booster.discord.Member", FakeDiscordMember), patch(
                "bot.cogs.booster.resolve_or_create_user", AsyncMock(return_value=42)
            ):
                await cog.verify_rules.callback(cog, interaction)
            self.assertIn("approval role", interaction.response.sent[0].lower())

        asyncio.run(_run())

    def test_accept_booster_ticket_assigns_only_the_approval_role(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.sent = None

                async def send_message(self, message, ephemeral=False):
                    self.sent = (message, ephemeral)

            class FakeUser:
                def __init__(self):
                    self.sent = []

                async def send(self, message):
                    self.sent.append(message)

            class FakeRole:
                def __init__(self, role_id):
                    self.id = role_id

            class FakeMember:
                def __init__(self, member_id, role_ids=None):
                    self.id = member_id
                    self.mention = f"<@{member_id}>"
                    self.roles = list(role_ids or [])

                async def add_roles(self, role, reason=None):
                    if not any(existing.id == role.id for existing in self.roles):
                        self.roles.append(role)

                async def remove_roles(self, role, reason=None):
                    self.roles = [existing for existing in self.roles if existing.id != role.id]

            class FakeGuild:
                def __init__(self):
                    self._members = {77: FakeMember(77, [FakeRole(222), FakeRole(333)])}
                    self._roles = {222: FakeRole(222), 333: FakeRole(333)}

                def get_member(self, user_id):
                    return self._members.get(user_id)

                def get_role(self, role_id):
                    return self._roles.get(role_id)

            class FakeDB:
                def __init__(self):
                    self.calls = []

                async def fetchone(self, query, params=()):
                    self.calls.append((query, params))
                    if "SELECT id, opener_user_id, related_application_id" in query:
                        return {"id": 7, "opener_user_id": 99, "related_application_id": 12}
                    if "SELECT discord_id FROM users" in query:
                        return {"discord_id": 77}
                    return None

                async def execute(self, query, params=()):
                    self.calls.append((query, params))
                    return 1

            class FakeBot:
                def __init__(self):
                    self.db = FakeDB()
                    self.sent_user = FakeUser()
                    self.guild = FakeGuild()

                def get_user(self, user_id):
                    return self.sent_user if user_id == 77 else None

            class FakeTextChannel:
                def __init__(self):
                    self.id = 333
                    self.name = "application-1"

            bot = FakeBot()
            interaction = SimpleNamespace(
                user=FakeMember(42),
                guild=bot.guild,
                channel=FakeTextChannel(),
                response=FakeResponse(),
            )
            approval_role_id = 333
            booster_role_id = 222
            cog = SimpleNamespace(
                bot=bot,
                settings=SimpleNamespace(
                    staff_role_ids={5},
                    admin_role_ids=set(),
                    booster_role_id=booster_role_id,
                    booster_approval_role_id=approval_role_id,
                ),
            )
            from bot.cogs.tickets import ApplicationTicketButton
            view = ApplicationTicketButton(cog)
            with patch("bot.cogs.tickets.is_staff_member", return_value=True), patch("bot.cogs.tickets.send_staff_audit_log", AsyncMock()):
                await view.accept_booster.callback(interaction)
            member = bot.guild.get_member(77)
            self.assertTrue(any(role.id == approval_role_id for role in member.roles))
            self.assertTrue(any(role.id == booster_role_id for role in member.roles))

        asyncio.run(_run())

    def test_accept_booster_ticket_resets_previous_rules_confirmation(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.sent = None

                async def send_message(self, message, ephemeral=False):
                    self.sent = (message, ephemeral)

            class FakeUser:
                def __init__(self):
                    self.sent = []

                async def send(self, message):
                    self.sent.append(message)

            class FakeRole:
                def __init__(self, role_id):
                    self.id = role_id

            class FakeMember:
                def __init__(self, member_id, role_ids=None):
                    self.id = member_id
                    self.mention = f"<@{member_id}>"
                    self.roles = list(role_ids or [])

                async def add_roles(self, role, reason=None):
                    if not any(existing.id == role.id for existing in self.roles):
                        self.roles.append(role)

                async def remove_roles(self, role, reason=None):
                    self.roles = [existing for existing in self.roles if existing.id != role.id]

            class FakeGuild:
                def __init__(self):
                    self._members = {77: FakeMember(77, [])}
                    self._roles = {222: FakeRole(222), 333: FakeRole(333)}

                def get_member(self, user_id):
                    return self._members.get(user_id)

                def get_role(self, role_id):
                    return self._roles.get(role_id)

            class FakeDB:
                def __init__(self):
                    self.calls = []

                async def fetchone(self, query, params=()):
                    self.calls.append((query, params))
                    if "SELECT id, opener_user_id, related_application_id" in query:
                        return {"id": 7, "opener_user_id": 99, "related_application_id": 12}
                    if "SELECT discord_id FROM users" in query:
                        return {"discord_id": 77}
                    return None

                async def execute(self, query, params=()):
                    self.calls.append((query, params))
                    return 1

            class FakeBot:
                def __init__(self):
                    self.db = FakeDB()
                    self.sent_user = FakeUser()
                    self.guild = FakeGuild()
                    self._cogs = {}

                def get_user(self, user_id):
                    return self.sent_user if user_id == 77 else None

                def get_cog(self, name):
                    return self._cogs.get(name)

            class FakeTextChannel:
                def __init__(self):
                    self.id = 333
                    self.name = "application-1"

            bot = FakeBot()
            interaction = SimpleNamespace(
                user=FakeMember(42),
                guild=bot.guild,
                channel=FakeTextChannel(),
                response=FakeResponse(),
            )
            approval_role_id = 333
            booster_role_id = 222

            cog = SimpleNamespace(
                bot=bot,
                settings=SimpleNamespace(
                    staff_role_ids={5},
                    admin_role_ids=set(),
                    booster_role_id=booster_role_id,
                    booster_approval_role_id=approval_role_id,
                ),
            )
            from bot.cogs.tickets import ApplicationTicketButton
            view = ApplicationTicketButton(cog)
            with patch("bot.cogs.tickets.is_staff_member", return_value=True), patch("bot.cogs.tickets.send_staff_audit_log", AsyncMock()):
                await view.accept_booster.callback(interaction)
            self.assertTrue(any(role.id == approval_role_id for role in bot.guild.get_member(77).roles))
            self.assertFalse(any(role.id == booster_role_id for role in bot.guild.get_member(77).roles))
            self.assertTrue(any("verified_booster = 0" in query for query, _ in bot.db.calls))

        asyncio.run(_run())

    def test_accept_booster_ticket_sends_rules_prompt_to_applicant(self):
        async def _run() -> None:
            class FakeResponse:
                def __init__(self):
                    self.sent = None

                async def send_message(self, message, ephemeral=False):
                    self.sent = (message, ephemeral)

            class FakeUser:
                def __init__(self):
                    self.sent = []

                async def send(self, message):
                    self.sent.append(message)

            class FakeRole:
                def __init__(self, role_id):
                    self.id = role_id

            class FakeMember:
                def __init__(self, member_id, role_ids=None):
                    self.id = member_id
                    self.mention = f"<@{member_id}>"
                    self.roles = list(role_ids or [])

                async def add_roles(self, role, reason=None):
                    self.roles.append(role)

            class FakeGuild:
                def __init__(self):
                    self._members = {77: FakeMember(77, [])}
                    self._roles = {222: FakeRole(222), 333: FakeRole(333)}

                def get_member(self, user_id):
                    return self._members.get(user_id)

                def get_role(self, role_id):
                    return self._roles.get(role_id)

            class FakeDB:
                def __init__(self):
                    self.calls = []

                async def fetchone(self, query, params=()):
                    self.calls.append((query, params))
                    if "SELECT id, opener_user_id, related_application_id" in query:
                        return {"id": 7, "opener_user_id": 99, "related_application_id": 12}
                    if "SELECT discord_id FROM users" in query:
                        return {"discord_id": 77}
                    return None

                async def execute(self, query, params=()):
                    self.calls.append((query, params))
                    return 1

            class FakeBot:
                def __init__(self):
                    self.db = FakeDB()
                    self.sent_user = FakeUser()
                    self.guild = FakeGuild()

                def get_user(self, user_id):
                    return self.sent_user if user_id == 77 else None

            class FakeTextChannel:
                def __init__(self):
                    self.id = 333
                    self.name = "application-1"

            bot = FakeBot()
            interaction = SimpleNamespace(
                user=FakeMember(42),
                guild=bot.guild,
                channel=FakeTextChannel(),
                response=FakeResponse(),
            )
            approval_role_id = 333
            booster_role_id = 222
            cog = SimpleNamespace(
                bot=bot,
                settings=SimpleNamespace(
                    staff_role_ids={5},
                    admin_role_ids=set(),
                    booster_role_id=booster_role_id,
                    booster_approval_role_id=approval_role_id,
                ),
            )
            from bot.cogs.tickets import ApplicationTicketButton
            view = ApplicationTicketButton(cog)
            with patch("bot.cogs.tickets.is_staff_member", return_value=True), patch("bot.cogs.tickets.send_staff_audit_log", AsyncMock()):
                await view.accept_booster.callback(interaction)
            self.assertIn("/verify_rules", " ".join(bot.sent_user.sent))
            self.assertTrue(any(role.id == approval_role_id for role in bot.guild.get_member(77).roles))
            self.assertFalse(any(role.id == booster_role_id for role in bot.guild.get_member(77).roles))

        asyncio.run(_run())

    def test_claimed_order_deadline_reopens_expired_order(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (7001, "claim-owner"),
                    )
                    order_id = await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, claim_deadline_hours, deadline_at, claimed_at) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        ("ORD-700", "Customer", "EU", "Ranked", "Gold", "Platinum", 2000, 1000, 0, user_id, user_id, 1, "2000-01-01 00:00:00"),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace(), get_channel=lambda _: None, get_user=lambda _: None)
                    cog = OrdersCog(bot)
                    await cog._check_claimed_order_deadlines()
                    row = await db.fetchone("SELECT status, assigned_booster_user_id, deadline_at FROM orders WHERE id = ?", (order_id,))
                    self.assertEqual(str(row["status"]), "open")
                    self.assertIsNone(row["assigned_booster_user_id"])
                    self.assertIsNone(row["deadline_at"])
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_expired_order_alert_shows_account_credentials(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (7002, "expired-booster"),
                    )
                    await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, claim_deadline_hours, deadline_at, claimed_at, account_username, account_password) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)",
                        ("ORD-701", "Customer", "EU", "Ranked", "Gold", "Platinum", 2000, 1000, 0, user_id, user_id, 1, "2000-01-01 00:00:00", "test-user", "test-pass"),
                    )
                    channel = SimpleNamespace(send=AsyncMock())
                    bot = SimpleNamespace(
                        db=db,
                        settings=SimpleNamespace(expired_order_action_channel_id=777),
                        get_channel=lambda channel_id: channel if channel_id == 777 else None,
                        get_user=lambda _: None,
                    )
                    cog = OrdersCog(bot)
                    await cog._check_claimed_order_deadlines()
                    channel.send.assert_awaited_once()
                    embed = channel.send.call_args.kwargs["embed"]
                    self.assertTrue(any(field.name == "Username" and "test-user" in str(field.value) for field in embed.fields))
                    self.assertTrue(any(field.name == "Password" and "test-pass" in str(field.value) for field in embed.fields))
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_history_embed_shows_rank_progress_and_recent_orders(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 5000, 250000, 12, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (3001, "history-user"),
                    )
                    await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        ("ORD-100", "Alice", "EU", "Ranked", "Silver", "Gold", 5000, 2500, 0, user_id, user_id),
                    )
                    await db.execute(
                        "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        ("ORD-101", "Bob", "NA", "Unranked", "Gold", "Platinum", 7000, 3500, 0, user_id, user_id),
                    )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    from bot.cogs.economy import EconomyCog
                    cog = EconomyCog(bot)
                    embed = await cog.build_history_embed_for_user(user_id)
                    self.assertIn("Current rank", embed.fields[0].name)
                    self.assertIn("Next milestone", embed.fields[1].name)
                    self.assertIn("ORD-101", embed.description)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_full_history_embed_includes_all_completed_orders(self):
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "settings.db"
                db = DatabaseManager(db_path, Path("bot/db/schema.sql"), Path("bot/db/migrations"))
                try:
                    await db.initialize()
                    user_id = await db.execute(
                        "INSERT INTO users (discord_id, username, role_type, booster_level, balance_cents, total_earned_cents, completed_orders_count, boosted_since, verified_booster, rules_confirmed_at, updated_at) VALUES (?, ?, 'booster', 0, 5000, 250000, 12, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (3002, "full-history-user"),
                    )
                    for i in range(8):
                        await db.execute(
                            "INSERT INTO orders (order_number, customer_name, region, queue_type, current_rank, target_rank, status, order_price_cents, booster_payout_cents, referral_bonus_cents, created_by_user_id, assigned_booster_user_id, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                            (f"ORD-{200 + i}", f"Alice{i}", "EU", "Ranked", "Silver", "Gold", 5000, 2500, 0, user_id, user_id),
                        )
                    bot = SimpleNamespace(db=db, settings=SimpleNamespace())
                    from bot.cogs.economy import EconomyCog
                    cog = EconomyCog(bot)
                    embed = await cog.build_full_history_embed_for_user(user_id)
                    self.assertIn("Full Booster History", embed.title)
                    self.assertIn("ORD-200", embed.description)
                    self.assertIn("ORD-207", embed.description)
                finally:
                    await db.close()

        asyncio.run(_run())

    def test_team_prefix_is_added_before_member_name(self):
        self.assertEqual(BoosterCog.format_member_name_with_team_tag("PlayerName", "PT"), "[PT] PlayerName")
        self.assertEqual(BoosterCog.format_member_name_with_team_tag("[OLD] PlayerName", "PT"), "[PT] PlayerName")
        self.assertEqual(BoosterCog.format_member_name_with_team_tag("PlayerName", None), "PlayerName")

    def test_booster_order_limit_by_rank(self):
        self.assertEqual(OrdersCog.get_active_order_limit_for_booster_level(0), 1)
        self.assertEqual(OrdersCog.get_active_order_limit_for_booster_level(2), 1)
        self.assertEqual(OrdersCog.get_active_order_limit_for_booster_level(3), 2)
        self.assertEqual(OrdersCog.get_active_order_limit_for_booster_level(4), 2)
        self.assertEqual(OrdersCog.get_active_order_limit_for_booster_level(5), 2)
        self.assertIsNone(OrdersCog.get_active_order_limit_for_booster_level(6))
        self.assertIsNone(OrdersCog.get_active_order_limit_for_booster_level(10))


if __name__ == "__main__":
    unittest.main()
