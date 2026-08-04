"""Thin discord.py adapter for Krubit's Phase 0 service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import discord
from discord import app_commands

from krubit.config import Settings
from krubit.discord.cards import render_card
from krubit.discord.install import phase_zero_intents
from krubit.domain.models import Card, CardField, GuildEvent
from krubit.services.foundation import (
    AuthorizationError,
    FoundationService,
    GuildDisabledError,
)


class FetchCommands(app_commands.Group):
    def __init__(self, service: FoundationService) -> None:
        super().__init__(name="fetch", description="Ask Krubit to fetch a system result")
        self._service = service

    @app_commands.command(name="status", description="Fetch Krubit's Phase 0 status")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return
        snapshot = await self._service.status(interaction.guild_id)
        card = Card(
            kind="fetched",
            title="🦴 Fetched: Krubit Status",
            description="Krubit's Phase 0 foundation status.",
            fields=(
                CardField("Enabled", "Yes" if snapshot.enabled else "No", inline=True),
                CardField("Events", str(snapshot.event_count), inline=True),
                CardField("Receipts", str(snapshot.receipt_count), inline=True),
                CardField(
                    "Database", "Healthy" if snapshot.database_healthy else "Unavailable", inline=True
                ),
            ),
        )
        await interaction.response.send_message(embed=render_card(card), ephemeral=True)

    @app_commands.command(name="test-card", description="Fetch an administrator test card")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def test_card(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return
        user = interaction.user
        can_manage_guild = isinstance(user, discord.Member) and user.guild_permissions.manage_guild
        try:
            card = await self._service.test_card(
                interaction.guild_id,
                actor_id=user.id,
                can_manage_guild=can_manage_guild,
            )
        except (AuthorizationError, GuildDisabledError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(embed=render_card(card), ephemeral=True)


class KrubitBot(discord.Client):
    """Discord transport; community judgment remains outside this class."""

    def __init__(self, settings: Settings, service: FoundationService) -> None:
        super().__init__(intents=phase_zero_intents(), application_id=settings.application_id)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(FetchCommands(service))
        self._service = service
        self._boot_id = uuid4().hex

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_guild_available(self, guild: discord.Guild) -> None:
        event = GuildEvent(
            event_id=f"guild_available:{guild.id}:{self._boot_id}",
            guild_id=guild.id,
            event_type="guild_available",
            occurred_at=datetime.now(UTC),
            payload={"guild_name": guild.name},
        )
        try:
            await self._service.ingest(event)
        except GuildDisabledError:
            return

