"""Guild-scoped staff controls for the live-signal runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import discord
from discord import app_commands

from krubit.discord.cards import render_card
from krubit.discord.live_signals import build_live_view, render_live_embed
from krubit.domain.live_signals import LiveSignalSession, StreamingObservation, TwitchStream
from krubit.domain.models import Card, CardField

if TYPE_CHECKING:
    from krubit.discord.bot import FetchCommands


ReconcileCallback = Callable[[discord.Guild], Awaitable[int]]


class LiveStatusService(Protocol):
    async def status(self, guild_id: int) -> tuple[LiveSignalSession, ...]: ...

    async def integration_health(self, guild_id: int) -> str: ...


class LiveCommands(app_commands.Group):
    """Keep staff previews read-only while delegating real work to the runtime."""

    def __init__(
        self,
        parent: FetchCommands,
        live_service: LiveStatusService | None,
        reconcile_callback: ReconcileCallback | None,
    ) -> None:
        super().__init__(name="live", description="Fetch live-signal status and staff previews")
        self._parent = parent
        self._live_service = live_service
        self._reconcile_callback = reconcile_callback

    @app_commands.command(name="status", description="Fetch live-signal status")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_live_status")
        if context is None:
            return
        guild, actor_id = context
        sessions = await self._live_service.status(guild.id) if self._live_service else ()
        twitch_status = (
            await self._live_service.integration_health(guild.id)
            if self._live_service
            else "unavailable"
        )
        discord_checks = [item.last_discord_at for item in sessions if item.last_discord_at]
        twitch_checks = [item.last_twitch_at for item in sessions if item.last_twitch_at]
        states = sorted({item.status.value for item in sessions})
        card = Card(
            kind="fetched",
            title="Fetched: Live Signal Status",
            description="Guild-scoped live-signal operational facts.",
            fields=(
                CardField("Active sessions", str(len(sessions)), True),
                CardField("Session states", ", ".join(states) or "None", True),
                CardField("Twitch availability", twitch_status, True),
                CardField(
                    "Last Discord check",
                    max(discord_checks).isoformat() if discord_checks else "No check recorded.",
                    False,
                ),
                CardField(
                    "Last Twitch check",
                    max(twitch_checks).isoformat() if twitch_checks else "No check recorded.",
                    False,
                ),
            ),
        )
        await self._parent.finish(
            interaction,
            action="fetch_live_status",
            actor_id=actor_id,
            embed=render_card(card),
            detail={"active_session_count": len(sessions), "twitch_status": twitch_status},
        )

    @app_commands.command(name="test", description="Preview the live-signal card privately")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_live_test")
        if context is None:
            return
        guild, actor_id = context
        observed_at = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
        stream = TwitchStream(
            stream_id="staff-preview",
            user_login="krubit",
            user_name="Krubit Preview",
            title="Live signal staff preview",
            game_name="Just Chatting",
            started_at=datetime(2026, 8, 4, 20, 12, tzinfo=UTC),
            thumbnail_url="https://static-cdn.jtvnw.net/preview-640x360.jpg",
        )
        observation = StreamingObservation(
            guild_id=guild.id,
            member_id=actor_id,
            twitch_login=stream.user_login,
            twitch_url="https://twitch.tv/krubit",
            activity_started_at=observed_at,
            observed_at=observed_at,
        )
        await self._parent.finish(
            interaction,
            action="fetch_live_test",
            actor_id=actor_id,
            embed=render_live_embed(observation, stream),
            view=build_live_view(observation.twitch_url),
            detail={"preview": True},
        )

    @app_commands.command(name="reconcile", description="Reconcile this guild's live signals")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def reconcile(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_live_reconcile")
        if context is None:
            return
        guild, actor_id = context
        applied = await self._reconcile_callback(guild) if self._reconcile_callback else 0
        card = Card(
            kind="fetched",
            title="Fetched: Live Signal Reconciliation",
            description="The guild's idempotent live-signal reconciliation completed.",
            fields=(CardField("Plans applied", str(applied), True),),
        )
        await self._parent.finish(
            interaction,
            action="fetch_live_reconcile",
            actor_id=actor_id,
            embed=render_card(card),
            detail={"plans_applied": applied},
        )
