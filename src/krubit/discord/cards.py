"""Render framework-independent cards as Discord embeds."""

from __future__ import annotations

import discord

from krubit.domain.models import Card


def render_card(card: Card) -> discord.Embed:
    embed = discord.Embed(title=card.title, description=card.description, color=card.color)
    for field in card.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    embed.set_footer(text="Krubit · functional system card")
    return embed

