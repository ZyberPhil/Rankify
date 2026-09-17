from __future__ import annotations

from io import BytesIO
import json
import mimetypes

import discord
from discord import app_commands
from discord.ext import commands
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
                    "✅ Your booster application was accepted. Please accept the booster rules in <#1544119579335196752> in the server to unlock booster access."
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

    async def _archive_ticket_channel(self, channel: discord.TextChannel) -> None:
        archive_category = self._get_channel(self.settings.archived_ticket_category_id)
        archive_category_obj = archive_category if isinstance(archive_category, discord.CategoryChannel) else None
        guild = channel.guild
        overwrites = self._staff_overwrites(guild)

        bot_member = guild.me or guild.get_member(self.bot.user.id)
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        edit_kwargs: dict[str, object] = {
            "name": f"closed-{channel.name}"[:90],
            "overwrites": overwrites,
            "reason": "Ticket archived",
        }
        if archive_category_obj is not None:
            edit_kwargs["category"] = archive_category_obj
        await channel.edit(**edit_kwargs)

    async def _build_ticket_transcript(self, channel: discord.TextChannel) -> bytes:
        messages = [message async for message in channel.history(limit=None, oldest_first=True)]
        output = BytesIO()
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "TicketTitle", parent=styles["Heading1"], fontName="Helvetica-Bold",
            fontSize=18, leading=22, textColor=colors.white, spaceAfter=2,
        )
        subtitle_style = ParagraphStyle(
            "TicketSubtitle", parent=styles["Normal"], fontName="Helvetica",
            fontSize=9, leading=12, textColor=colors.HexColor("#949ba4"),
        )
        author_style = ParagraphStyle(
            "Author", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=10, leading=13, textColor=colors.white,
        )
        content_style = ParagraphStyle(
            "Content", parent=styles["Normal"], fontName="Helvetica",
            fontSize=10, leading=14, textColor=colors.HexColor("#dbdee1"),
            wordWrap="CJK",
        )
        meta_style = ParagraphStyle(
            "Meta", parent=styles["Normal"], fontName="Helvetica",
            fontSize=7.5, leading=10, textColor=colors.HexColor("#949ba4"),
        )
        link_style = ParagraphStyle(
            "Link", parent=content_style, textColor=colors.HexColor("#00a8fc"),
        )
        story = [
            Paragraph(f"#{self._pdf_escape(channel.name)}", title_style),
            Paragraph("Ticket transcript", subtitle_style),
            Spacer(1, 8 * mm),
        ]
        for message in messages:
            author = self._pdf_escape(message.author.display_name)
            timestamp = self._pdf_escape(message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
            author_initials = self._pdf_escape(message.author.display_name[:2].upper() or "?")
            header = Paragraph(f"{author} <font color='#949ba4' size='7.5'>{timestamp}</font>", author_style)
            content = self._pdf_escape(message.content).replace("\n", "<br/>") or "<font color='#949ba4'>(no text)</font>"
            body = [header, Paragraph(content, content_style)]
            for attachment in message.attachments:
                attachment_url = self._pdf_escape(attachment.url)
                attachment_size = self._format_attachment_size(getattr(attachment, "size", 0))
                body.append(Paragraph(
                    f"<b>Attachment:</b> <link href='{attachment_url}'>"
                    f"<font color='#00a8fc'>{self._pdf_escape(attachment.filename)}</font></link> "
                    f"<font color='#949ba4'>({attachment_size})</font><br/>"
                    f"<font size='7' color='#949ba4'>{attachment_url}</font>", link_style,
                ))
                attachment_type = str(getattr(attachment, "content_type", "") or "")
                is_image = attachment_type.startswith("image/") or (
                    mimetypes.guess_type(attachment.filename)[0] or ""
                ).startswith("image/")
                if is_image:
                    try:
                        image_data = await attachment.read()
                        image_reader = ImageReader(BytesIO(image_data))
                        image_width, image_height = image_reader.getSize()
                        max_width = 135 * mm
                        max_height = 90 * mm
                        scale = min(max_width / image_width, max_height / image_height, 1)
                        image = Image(
                            BytesIO(image_data),
                            width=image_width * scale,
                            height=image_height * scale,
                        )
                        image.hAlign = "LEFT"
                        image._restrictSize(max_width, max_height)
                        body.append(image)
                        body.append(Spacer(1, 2 * mm))
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound, OSError, ValueError):
                        pass
            for embed in message.embeds:
                embed_title = self._pdf_escape(embed.title or "Embed")
                embed_description = self._pdf_escape(embed.description or "").replace("\n", "<br/>")
                body.append(Table(
                    [[Paragraph(f"<b>{embed_title}</b><br/>{embed_description}", content_style)]],
                    colWidths=[140 * mm],
                    style=TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#2b2d31")),
                        ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor("#5865f2")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]),
                ))
            message_table = Table(
                [[Paragraph(f"<b>{author_initials}</b>", author_style), body]],
                colWidths=[13 * mm, 140 * mm],
                hAlign="LEFT",
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#5865f2")),
                    ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#313338")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0, 0), (0, 0), "CENTER"),
                    ("LEFTPADDING", (0, 0), (0, 0), 4),
                    ("RIGHTPADDING", (0, 0), (0, 0), 4),
                    ("LEFTPADDING", (1, 0), (1, 0), 10),
                    ("RIGHTPADDING", (1, 0), (1, 0), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]),
            )
            story.extend([message_table, Spacer(1, 3 * mm)])

        def draw_background(canvas, document) -> None:
            canvas.saveState()
            canvas.setFillColor(colors.HexColor("#313338"))
            canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#1e1f22"))
            canvas.rect(0, A4[1] - 12 * mm, A4[0], 12 * mm, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#949ba4"))
            canvas.setFont("Helvetica", 7)
            canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {document.page}")
            canvas.restoreState()

        document = SimpleDocTemplate(
            output, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
            topMargin=18 * mm, bottomMargin=15 * mm,
            title=f"Ticket transcript: {channel.name}",
        )
        document.build(story, onFirstPage=draw_background, onLaterPages=draw_background)
        return output.getvalue()

    @staticmethod
    def _pdf_escape(value: str) -> str:
        return (
            str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&#39;")
        )

    @staticmethod
    def _format_attachment_size(size: int | None) -> str:
        value = max(int(size or 0), 0)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
            value /= 1024
        return "0 B"

    async def _send_ticket_transcript(self, channel: discord.TextChannel, opener_user_id: int) -> bool:
        try:
            transcript = await self._build_ticket_transcript(channel)
        except (discord.Forbidden, discord.HTTPException):
            return False

        filename = f"{channel.name}-transcript.pdf"
        original_files: list[tuple[str, bytes]] = []
        try:
            messages = [message async for message in channel.history(limit=None, oldest_first=True)]
            for message in messages:
                for attachment in message.attachments:
                    try:
                        original_files.append((attachment.filename, await attachment.read()))
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                        continue
        except (discord.Forbidden, discord.HTTPException):
            pass

        log_delivered = False
        transcript_channel = self._get_channel(self.settings.ticket_transcript_channel_id)
        if isinstance(transcript_channel, discord.TextChannel):
            try:
                await transcript_channel.send(
                    content=f"📄 PDF transcript for **{channel.name}**",
                    file=discord.File(BytesIO(transcript), filename=filename),
                )
                for original_filename, original_data in original_files:
                    await transcript_channel.send(
                        content=f"📎 Original attachment: **{original_filename}**",
                        file=discord.File(BytesIO(original_data), filename=original_filename),
                    )
                log_delivered = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        opener = await self.bot.db.fetchone(
            "SELECT discord_id FROM users WHERE id = ?", (opener_user_id,)
        )
        if opener is not None and opener["discord_id"] is not None:
            try:
                user = self.bot.get_user(int(opener["discord_id"]))
                if user is None:
                    user = await self.bot.fetch_user(int(opener["discord_id"]))
                await user.send(
                    content=f"📄 Here is the PDF transcript for your ticket **{channel.name}**.",
                    file=discord.File(BytesIO(transcript), filename=filename),
                )
                for original_filename, original_data in original_files:
                    await user.send(
                        content=f"📎 Original attachment: **{original_filename}**",
                        file=discord.File(BytesIO(original_data), filename=original_filename),
                    )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        return log_delivered

    async def _delete_ticket_channel(self, channel: discord.TextChannel) -> None:
        try:
            await channel.delete(reason="Ticket closed and transcript archived")
        except discord.NotFound:
            # The channel may already have been deleted by a duplicate close action.
            return
        except (discord.Forbidden, discord.HTTPException):
            try:
                await self._archive_ticket_channel(channel)
            except discord.NotFound:
                return

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
            transcript_sent = await self._send_ticket_transcript(channel, int(ticket_row["opener_user_id"]))
            if not transcript_sent:
                await safe_interaction_response(
                    interaction,
                    "❌ Transcript could not be delivered. The ticket remains open.",
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
                (closer_user_id, ticket_row["id"]),
            )
            await self._delete_ticket_channel(channel)

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if member is None or channel is None:
            raise app_commands.CheckFailure("Invalid context.")

        await interaction.response.defer(ephemeral=True)

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
        transcript_sent = await self._send_ticket_transcript(channel, int(ticket["opener_user_id"]))
        if not transcript_sent:
            await safe_interaction_response(
                interaction,
                "❌ Transcript could not be delivered. The ticket remains open.",
                ephemeral=True,
            )
            return
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
        await self._delete_ticket_channel(channel)
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
