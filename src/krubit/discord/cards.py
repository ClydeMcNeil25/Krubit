"""Render framework-independent cards as bounded Discord embeds."""

from __future__ import annotations

import json

import discord

from krubit.domain.companion import HealthReport, SnapshotDiff
from krubit.domain.models import Card


def render_card(card: Card) -> discord.Embed:
    embed = discord.Embed(title=card.title, description=card.description, color=card.color)
    for field in card.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    embed.set_footer(text="Krubit · functional system card")
    return embed


def render_health_card(report: HealthReport, *, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=(
            f"Status: **{report.status.title()}**\nChecked: {report.checked_at.isoformat()}"
        ),
        color=0xEF4444 if report.status == "critical" else 0xF59E0B,
    )
    if not report.findings:
        embed.add_field(name="healthy", value="No operational findings.", inline=False)
    for finding in report.findings[:25]:
        embed.add_field(name=finding.code, value=finding.detail[:1024], inline=False)
    overflow = max(0, len(report.findings) - 25)
    footer = "Krubit · factual health card"
    if overflow:
        footer += f" · {overflow} additional findings"
    embed.set_footer(text=footer)
    return embed


def render_diff_card(diff: SnapshotDiff, *, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"Direction: {diff.direction}\nChanges: {len(diff.items)}",
        color=0x8B5CF6,
    )
    for item in diff.items[:25]:
        detail = json.dumps(item.fields, sort_keys=True, ensure_ascii=False)
        embed.add_field(
            name=f"{item.change}: {item.section}/{item.resource_id}",
            value=detail[:1024],
            inline=False,
        )
    overflow = max(0, len(diff.items) - 25)
    footer = "Krubit · factual change card"
    if overflow:
        footer += f" · {overflow} additional changes"
    embed.set_footer(text=footer)
    return embed
