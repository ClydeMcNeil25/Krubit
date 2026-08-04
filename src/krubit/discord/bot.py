"""Thin discord.py adapter for Krubit's Phase 0 service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import discord
from aiohttp import BaseConnector
from discord import app_commands

from krubit.config import Settings
from krubit.discord.cards import render_card
from krubit.discord.events import guild_event
from krubit.discord.install import phase_one_intents
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
                    "Database",
                    "Healthy" if snapshot.database_healthy else "Unavailable",
                    inline=True,
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

    def __init__(
        self,
        settings: Settings,
        service: FoundationService,
        *,
        connector: BaseConnector | None = None,
    ) -> None:
        super().__init__(
            intents=phase_one_intents(),
            application_id=settings.application_id,
            connector=connector,
        )
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(FetchCommands(service))
        self._service = service
        self._boot_id = uuid4().hex

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def _record_guild_connection(self, guild: discord.Guild) -> None:
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

    async def on_guild_available(self, guild: discord.Guild) -> None:
        await self._record_guild_connection(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._record_guild_connection(guild)

    async def _ingest_change(
        self,
        event_type: str,
        guild_id: int,
        entity_id: int,
        before: dict[str, str | bool | int | None] | None,
        after: dict[str, str | bool | int | None] | None,
    ) -> None:
        try:
            await self._service.ingest(
                guild_event(event_type, guild_id, entity_id, datetime.now(UTC), before, after)
            )
        except GuildDisabledError:
            return

    async def on_member_join(self, member: discord.Member) -> None:
        await self._ingest_change(
            "member_joined", member.guild.id, member.id, None, {"bot": member.bot}
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        await self._ingest_change(
            "member_left", member.guild.id, member.id, {"bot": member.bot}, None
        )

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_roles = ",".join(str(role.id) for role in before.roles)
        after_roles = ",".join(str(role.id) for role in after.roles)
        if before_roles != after_roles:
            await self._ingest_change(
                "member_roles_updated",
                after.guild.id,
                after.id,
                {"role_ids": before_roles},
                {"role_ids": after_roles},
            )

    @staticmethod
    def _role_state(role: discord.Role) -> dict[str, str | bool | int | None]:
        return {
            "name": role.name,
            "position": role.position,
            "permissions": str(role.permissions.value),
            "managed": role.managed,
        }

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._ingest_change(
            "role_created", role.guild.id, role.id, None, self._role_state(role)
        )

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        await self._ingest_change(
            "role_updated",
            after.guild.id,
            after.id,
            self._role_state(before),
            self._role_state(after),
        )

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._ingest_change(
            "role_deleted", role.guild.id, role.id, self._role_state(role), None
        )

    @staticmethod
    def _channel_state(
        channel: discord.abc.GuildChannel,
    ) -> dict[str, str | bool | int | None]:
        return {
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": (
                str(channel.category_id) if getattr(channel, "category_id", None) else None
            ),
        }

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self._ingest_change(
            "channel_created", channel.guild.id, channel.id, None, self._channel_state(channel)
        )

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        await self._ingest_change(
            "channel_updated",
            after.guild.id,
            after.id,
            self._channel_state(before),
            self._channel_state(after),
        )

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._ingest_change(
            "channel_deleted", channel.guild.id, channel.id, self._channel_state(channel), None
        )

    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        await self._ingest_change(
            "scheduled_event_created", event.guild_id, event.id, None, {"name": event.name}
        )

    async def on_scheduled_event_update(
        self, before: discord.ScheduledEvent, after: discord.ScheduledEvent
    ) -> None:
        await self._ingest_change(
            "scheduled_event_updated",
            after.guild_id,
            after.id,
            {"name": before.name, "status": str(before.status)},
            {"name": after.name, "status": str(after.status)},
        )

    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        await self._ingest_change(
            "scheduled_event_deleted", event.guild_id, event.id, {"name": event.name}, None
        )

    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        await self._ingest_change(
            "webhooks_updated", channel.guild.id, channel.id, None, {"channel": channel.name}
        )

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        await self._ingest_change(
            "guild_updated",
            after.id,
            after.id,
            {"name": before.name},
            {"name": after.name},
        )

    @staticmethod
    def _automod_state(rule: discord.AutoModRule) -> dict[str, str | bool | int | None]:
        return {"name": rule.name, "enabled": rule.enabled}

    async def on_automod_rule_create(self, rule: discord.AutoModRule) -> None:
        await self._ingest_change(
            "automod_rule_created",
            rule.guild.id,
            rule.id,
            None,
            self._automod_state(rule),
        )

    async def on_automod_rule_update(
        self, before: discord.AutoModRule, after: discord.AutoModRule
    ) -> None:
        await self._ingest_change(
            "automod_rule_updated",
            after.guild.id,
            after.id,
            self._automod_state(before),
            self._automod_state(after),
        )

    async def on_automod_rule_delete(self, rule: discord.AutoModRule) -> None:
        await self._ingest_change(
            "automod_rule_deleted",
            rule.guild.id,
            rule.id,
            self._automod_state(rule),
            None,
        )

    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        await self._ingest_change(
            "automod_action_executed",
            execution.guild_id,
            execution.rule_id,
            None,
            {
                "user_id": str(execution.user_id),
                "channel_id": str(execution.channel_id),
            },
        )
