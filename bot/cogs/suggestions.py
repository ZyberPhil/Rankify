from __future__ import annotations

import discord
from discord.ext import commands


async def safe_suggestion_response(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = True,
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class SuggestionModal(discord.ui.Modal, title="Create suggestion"):
    title_input = discord.ui.TextInput(
        label="Suggestion title",
        placeholder="What should be improved or added?",
        max_length=100,
    )
    details_input = discord.ui.TextInput(
        label="Details",
        placeholder="Describe your idea and its benefit.",
        style=discord.TextStyle.paragraph,
        max_length=1800,
    )

    def __init__(self, cog: "SuggestionsCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = self.cog._get_suggestion_channel()
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if channel is None or member is None:
            await safe_suggestion_response(interaction, "❌ Suggestions are not configured yet.")
            return

        suggestion_id = await self.cog.bot.db.execute(
            """
            INSERT INTO suggestions (author_discord_id, author_name, title, details, channel_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (member.id, member.display_name, str(self.title_input), str(self.details_input), channel.id),
        )
        embed = discord.Embed(
            title=f"Suggestion #{suggestion_id}: {str(self.title_input)}",
            description=str(self.details_input),
            color=0x5865F2,
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Status", value="🟡 Open", inline=True)
        embed.add_field(name="Votes", value="👍 0   👎 0", inline=True)
        embed.set_footer(text=f"Suggestion ID: {suggestion_id}")
        try:
            message = await channel.send(embed=embed, view=SuggestionVoteView(self.cog, suggestion_id))
            await message.add_reaction("👍")
            await message.add_reaction("👎")
            await self.cog.bot.db.execute(
                "UPDATE suggestions SET message_id = ? WHERE id = ?",
                (message.id, suggestion_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            await safe_suggestion_response(interaction, "❌ I could not post the suggestion in the configured channel.")
            return
        await safe_suggestion_response(interaction, f"✅ Your suggestion #{suggestion_id} was posted.")


class SuggestionDashboardView(discord.ui.View):
    def __init__(self, cog: "SuggestionsCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Make a suggestion",
        style=discord.ButtonStyle.primary,
        emoji="💡",
        custom_id="suggestions:open",
    )
    async def open_modal(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.cog._get_suggestion_channel() is None:
            await safe_suggestion_response(interaction, "❌ The suggestions channel is not configured.")
            return
        try:
            await interaction.response.send_modal(SuggestionModal(self.cog))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


class SuggestionVoteView(discord.ui.View):
    def __init__(self, cog: "SuggestionsCog", suggestion_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id
        approve_button = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"suggestions:approve:{suggestion_id}",
        )
        reject_button = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"suggestions:reject:{suggestion_id}",
        )
        approve_button.callback = self._approve
        reject_button.callback = self._reject
        self.add_item(approve_button)
        self.add_item(reject_button)

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self._set_status(interaction, "approved", "✅ Approved")

    async def _reject(self, interaction: discord.Interaction) -> None:
        await self._set_status(interaction, "rejected", "❌ Rejected")

    async def _set_status(self, interaction: discord.Interaction, status: str, label: str) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or not (
            member.guild_permissions.administrator
            or any(role.id in self.cog.settings.staff_role_ids | self.cog.settings.admin_role_ids for role in member.roles)
        ):
            await safe_suggestion_response(interaction, "❌ Only staff can review suggestions.")
            return
        await self.cog.bot.db.execute(
            "UPDATE suggestions SET status = ?, reviewed_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, member.id, self.suggestion_id),
        )
        if interaction.message is not None and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            for index, field in enumerate(embed.fields):
                if field.name == "Status":
                    embed.set_field_at(index, name="Status", value=label, inline=field.inline)
                    break
            await interaction.message.edit(embed=embed)
        await safe_suggestion_response(interaction, f"✅ Suggestion #{self.suggestion_id}: {label}")


class SuggestionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self.dashboard_view = SuggestionDashboardView(self)

    async def cog_load(self) -> None:
        self.bot.add_view(self.dashboard_view)

    def _get_suggestion_channel(self) -> discord.TextChannel | None:
        channel_id = self.settings.suggestion_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update_vote_count(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update_vote_count(payload)

    async def _update_vote_count(self, payload: discord.RawReactionActionEvent) -> None:
        emoji = str(payload.emoji)
        bot_user_id = getattr(self.bot.user, "id", None)
        if emoji not in {"👍", "👎"} or payload.user_id == bot_user_id:
            return

        suggestion = await self.bot.db.fetchone(
            "SELECT id FROM suggestions WHERE message_id = ? AND channel_id = ?",
            (payload.message_id, payload.channel_id),
        )
        if suggestion is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            embed = message.embeds[0] if message.embeds else None
            if embed is None:
                return
            votes = {"👍": 0, "👎": 0}
            for reaction in message.reactions:
                reaction_emoji = str(reaction.emoji)
                if reaction_emoji in votes:
                    # The bot adds one starter reaction to each option.
                    votes[reaction_emoji] = max(reaction.count - 1, 0)
            for index, field in enumerate(embed.fields):
                if field.name == "Votes":
                    embed.set_field_at(
                        index,
                        name="Votes",
                        value=f"👍 {votes['👍']}   👎 {votes['👎']}",
                        inline=field.inline,
                    )
                    break
            await message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def post_dashboard(self, interaction: discord.Interaction) -> None:
        channel = self._get_suggestion_channel()
        if channel is None:
            await interaction.response.send_message("❌ Configure a suggestion channel first.", ephemeral=True)
            return
        embed = discord.Embed(
            title="💡 Suggestions",
            description="Share your ideas with the team. Click the button below to submit a suggestion.",
            color=0x5865F2,
        )
        embed.set_footer(text="Please keep suggestions constructive and specific.")
        try:
            await channel.send(embed=embed, view=self.dashboard_view)
            await interaction.response.send_message(f"✅ Suggestions dashboard posted in {channel.mention}.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("❌ I cannot send messages in that channel.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SuggestionsCog(bot))