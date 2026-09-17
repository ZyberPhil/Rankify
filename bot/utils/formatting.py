from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import discord

THEME_COLORS = {
    "primary": 0x7C3AED,
    "secondary": 0x22D3EE,
    "success": 0x34D399,
    "warning": 0xF59E0B,
    "danger": 0xEF4444,
    "panel": 0x111827,
}


def money_cents_to_eur(amount_cents: int) -> str:
    euros = amount_cents / 100
    return f"€{euros:,.2f}"


def create_lawliet_embed(
    bot,
    *,
    title: str | None = None,
    description: str | None = None,
    color: int = THEME_COLORS["primary"],
    **kwargs,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, **kwargs)
    bot_user = getattr(bot, "user", None)
    avatar_url = getattr(getattr(bot_user, "display_avatar", None), "url", None)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="Rankify", icon_url=avatar_url)
    return embed


def parse_eur_to_cents(raw_value: str) -> int:
    normalized = raw_value.replace(",", ".").strip()
    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("invalid money value") from exc
    return int(amount * 100)
