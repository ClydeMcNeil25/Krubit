"""Thin discord.py adapter for Krubit's Phase 0 service."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import uuid4

import discord
from aiohttp import BaseConnector
from discord import app_commands
from discord.ext import tasks

from krubit.config import Settings
from krubit.discord.cards import render_card, render_diff_card, render_health_card
from krubit.discord.events import guild_event
from krubit.discord.install import phase_two_intents, phase_two_permissions
from krubit.discord.inventory import InventoryCapture, capture_inventory
from krubit.discord.live_commands import LiveCommands, ReconcileCallback
from krubit.discord.live_runtime import LiveSignalRuntime
from krubit.domain.companion import SnapshotRecord
from krubit.domain.models import Card, CardField, GuildEvent
from krubit.integrations.twitch import TwitchClient, UnavailableTwitchClient
from krubit.services.daily_summary import DailySummaryResult, DailySummaryService
from krubit.services.foundation import (
    AuthorizationError,
    FoundationService,
    GuildDisabledError,
)
from krubit.services.health import HealthService
from krubit.services.live_signals import LiveSignalService
from krubit.services.snapshots import SnapshotService, compare_inventory


class FetchCommands(app_commands.Group):
    def __init__(
        self,
        service: FoundationService,
        *,
        live_service: LiveSignalService | None = None,
        reconcile_callback: ReconcileCallback | None = None,
        presence_intent: bool = False,
        twitch_credentials: bool = False,
        twitch_available: bool = False,
    ) -> None:
        super().__init__(name="fetch", description="Ask Krubit to fetch a system result")
        self._service = service
        self._snapshots = SnapshotService(service.store)
        self._health = HealthService()
        self._live_service = live_service
        self._presence_intent = presence_intent
        self._twitch_credentials = twitch_credentials
        self._twitch_available = twitch_available
        self.add_command(BackupCommands(self))
        self.add_command(LiveCommands(self, live_service, reconcile_callback))

    @property
    def snapshots(self) -> SnapshotService:
        return self._snapshots

    async def authorize(
        self, interaction: discord.Interaction, action: str
    ) -> tuple[discord.Guild, int] | None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return None
        user = interaction.user
        can_manage = isinstance(user, discord.Member) and user.guild_permissions.manage_guild
        try:
            await self._service.authorize_manager(
                interaction.guild_id, user.id, can_manage, action=action
            )
        except (AuthorizationError, GuildDisabledError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return None
        await interaction.response.defer(ephemeral=True, thinking=True)
        return interaction.guild, user.id

    async def capture(self, guild: discord.Guild) -> tuple[InventoryCapture, SnapshotRecord]:
        config = await self._service.store.get_live_signal_config(guild.id)
        inventory = await capture_inventory(
            guild,
            required_permissions=phase_two_permissions(),
            configured_channel_id=None,
            live_channel_id=config.channel_id if config else None,
            streaming_role_id=config.role_id if config else None,
            presence_intent=self._presence_intent,
            twitch_credentials=self._twitch_credentials,
            twitch_available=self._twitch_available,
        )
        snapshot = await self._snapshots.capture(guild.id, inventory, datetime.now(UTC))
        return inventory, snapshot

    async def finish(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        actor_id: int,
        embed: discord.Embed,
        detail: dict[str, str | bool | int],
        view: discord.ui.View | None = None,
    ) -> None:
        if interaction.guild_id is None:
            raise RuntimeError("authorized interaction lost guild context")
        await self._service.record_action(
            interaction.guild_id,
            action=action,
            status="succeeded",
            actor_id=actor_id,
            detail=detail,
        )
        if view is None:
            await interaction.edit_original_response(embed=embed)
        else:
            await interaction.edit_original_response(embed=embed, view=view)

    @app_commands.command(name="status", description="Fetch Krubit's Phase 1 status")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        context = await self.authorize(interaction, "fetch_status")
        if context is None:
            return
        guild, actor_id = context
        snapshot = await self._service.status(guild.id, actor_id=actor_id)
        card = Card(
            kind="fetched",
            title="🦴 Fetched: Krubit Status",
            description="Krubit's Phase 1 operational status.",
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
        await interaction.edit_original_response(embed=render_card(card))

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

    @app_commands.command(name="server-health", description="Fetch factual server health")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def server_health(self, interaction: discord.Interaction) -> None:
        context = await self.authorize(interaction, "fetch_server_health")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self.capture(guild)
        report = self._health.server_health(
            snapshot, now=datetime.now(UTC), database_healthy=True, gateway_ready=True
        )
        await self.finish(
            interaction,
            action="fetch_server_health",
            actor_id=actor_id,
            embed=render_health_card(report, title="Fetched: Server Health"),
            detail={"snapshot_version": snapshot.version},
        )

    @app_commands.command(name="changes", description="Fetch latest configuration changes")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def changes(self, interaction: discord.Interaction) -> None:
        context = await self.authorize(interaction, "fetch_changes")
        if context is None:
            return
        guild, actor_id = context
        previous = await self._snapshots.latest(guild.id)
        _, current = await self.capture(guild)
        diff = (
            compare_inventory(current.content, current.content)
            if previous is None or previous.snapshot_id == current.snapshot_id
            else compare_inventory(previous.content, current.content)
        )
        await self.finish(
            interaction,
            action="fetch_changes",
            actor_id=actor_id,
            embed=render_diff_card(diff, title="Fetched: Server Changes"),
            detail={"change_count": len(diff.items)},
        )

    @app_commands.command(name="permissions", description="Fetch Krubit's Discord permissions")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def permissions(self, interaction: discord.Interaction) -> None:
        context = await self.authorize(interaction, "fetch_permissions")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self.capture(guild)
        report = self._health.permission_health(snapshot)
        await self.finish(
            interaction,
            action="fetch_permissions",
            actor_id=actor_id,
            embed=render_health_card(report, title="Fetched: Permissions"),
            detail={"finding_count": len(report.findings)},
        )

    @app_commands.command(name="integrations", description="Fetch integration visibility")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def integrations(self, interaction: discord.Interaction) -> None:
        context = await self.authorize(interaction, "fetch_integrations")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self.capture(guild)
        report = self._health.integration_health(snapshot)
        await self.finish(
            interaction,
            action="fetch_integrations",
            actor_id=actor_id,
            embed=render_health_card(report, title="Fetched: Integrations"),
            detail={"finding_count": len(report.findings)},
        )


class BackupCommands(app_commands.Group):
    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(name="backup", description="Fetch configuration backup results")
        self._parent = parent

    @app_commands.command(name="status", description="Fetch latest snapshot status")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_backup_status")
        if context is None:
            return
        guild, actor_id = context
        snapshot = await self._parent.snapshots.latest(guild.id)
        card = Card(
            kind="fetched",
            title="Fetched: Backup Status",
            description="Latest read-only server configuration snapshot.",
            fields=(
                CardField("Version", str(snapshot.version) if snapshot else "None", True),
                CardField("Integrity", snapshot.content_hash[:12] if snapshot else "None", True),
                CardField(
                    "Captured", snapshot.captured_at.isoformat() if snapshot else "Never", False
                ),
            ),
        )
        await self._parent.finish(
            interaction,
            action="fetch_backup_status",
            actor_id=actor_id,
            embed=render_card(card),
            detail={"snapshot_present": snapshot is not None},
        )

    @app_commands.command(name="create", description="Capture a configuration snapshot")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_backup_create")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self._parent.capture(guild)
        card = Card(
            kind="fetched",
            title="Fetched: Backup Created",
            description="Configuration snapshot captured without message or member content.",
            fields=(
                CardField("Version", str(snapshot.version), True),
                CardField("Integrity", snapshot.content_hash[:12], True),
            ),
        )
        await self._parent.finish(
            interaction,
            action="fetch_backup_create",
            actor_id=actor_id,
            embed=render_card(card),
            detail={"snapshot_version": snapshot.version},
        )

    @app_commands.command(name="preview", description="Preview restoring the latest snapshot")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_backup_preview")
        if context is None:
            return
        guild, actor_id = context
        target = await self._parent.snapshots.latest(guild.id)
        if target is None:
            card = Card("fetched", "Fetched: Restore Preview", "No snapshot exists yet.")
            await self._parent.finish(
                interaction,
                action="fetch_backup_preview",
                actor_id=actor_id,
                embed=render_card(card),
                detail={"snapshot_present": False},
            )
            return
        inventory = await capture_inventory(
            guild, required_permissions=phase_two_permissions(), configured_channel_id=None
        )
        diff = await self._parent.snapshots.preview_restore(
            guild.id, target.snapshot_id, inventory
        )
        await self._parent.finish(
            interaction,
            action="fetch_backup_preview",
            actor_id=actor_id,
            embed=render_diff_card(diff, title="Fetched: Restore Preview"),
            detail={"change_count": len(diff.items), "mutated": False},
        )


class KrubitBot(discord.Client):
    """Discord transport; community judgment remains outside this class."""

    def __init__(
        self,
        settings: Settings,
        service: FoundationService,
        *,
        connector: BaseConnector | None = None,
        twitch: TwitchClient | None = None,
    ) -> None:
        super().__init__(
            intents=phase_two_intents(),
            application_id=settings.application_id,
            connector=connector,
        )
        self.tree = app_commands.CommandTree(self)
        self._service = service
        self._boot_id = uuid4().hex
        self._settings = settings
        self._daily_summaries = DailySummaryService(service.store)
        live_twitch: TwitchClient = twitch or UnavailableTwitchClient()
        live_runtime_enabled = settings.live_signals_enabled and twitch is not None
        self._live_runtime_enabled = live_runtime_enabled
        live_signals = (
            LiveSignalService(service.store, live_twitch) if live_runtime_enabled else None
        )
        self._live_runtime = LiveSignalRuntime(
            service.store,
            live_signals,
            receipts=service,
        )
        self.tree.add_command(
            FetchCommands(
                service,
                live_service=live_signals,
                reconcile_callback=self._live_runtime.reconcile_guild,
                presence_intent=self.intents.presences,
                twitch_credentials=(
                    settings.twitch_client_id is not None
                    and settings.twitch_client_secret is not None
                ),
                twitch_available=live_runtime_enabled,
            )
        )

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.daily_health_summary.start()
        if self._live_runtime_enabled:
            self.live_signal_reconciliation.start()

    async def close(self) -> None:
        self.daily_health_summary.cancel()
        self.live_signal_reconciliation.cancel()
        await super().close()

    async def run_daily_summary_for_guild(
        self, guild: discord.Guild, summary_date: date
    ) -> DailySummaryResult:
        if not await self._service.store.guild_is_enabled(guild.id):
            return DailySummaryResult(guild.id, summary_date, "guild_disabled", None)
        claimed = await self._daily_summaries.claim(guild.id, summary_date)
        if not claimed:
            return DailySummaryResult(guild.id, summary_date, "already_claimed", None)
        channel_id = self._settings.staff_channel_id
        if channel_id is None:
            return await self._record_daily_summary_outcome(
                guild.id,
                summary_date,
                status="delivery_disabled",
                channel_id=None,
                receipt_status="delivery_disabled",
                detail={"reason": "staff_channel_not_configured"},
            )
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await self._record_daily_summary_outcome(
                guild.id,
                summary_date,
                status="channel_missing",
                channel_id=channel_id,
                receipt_status="failed",
                detail={"channel_id": channel_id, "reason": "staff_channel_missing"},
            )
        permissions = channel.permissions_for(guild.me)
        if not all(
            (
                permissions.view_channel,
                permissions.send_messages,
                permissions.embed_links,
            )
        ):
            return await self._record_daily_summary_outcome(
                guild.id,
                summary_date,
                status="permission_missing",
                channel_id=channel_id,
                receipt_status="failed",
                detail={"channel_id": channel_id, "reason": "staff_channel_not_writable"},
            )
        try:
            config = await self._service.store.get_live_signal_config(guild.id)
            inventory = await capture_inventory(
                guild,
                required_permissions=phase_two_permissions(),
                configured_channel_id=channel_id,
                live_channel_id=config.channel_id if config else None,
                streaming_role_id=config.role_id if config else None,
                presence_intent=self.intents.presences,
                twitch_credentials=(
                    self._settings.twitch_client_id is not None
                    and self._settings.twitch_client_secret is not None
                ),
                twitch_available=self._live_runtime_enabled,
            )
            snapshot = await SnapshotService(self._service.store).capture(
                guild.id, inventory, datetime.now(UTC)
            )
            report = HealthService().server_health(
                snapshot,
                now=datetime.now(UTC),
                database_healthy=True,
                gateway_ready=self.is_ready(),
            )
            await channel.send(
                embed=render_health_card(report, title="Krubit Daily Server Health")
            )
        except Exception as exc:
            return await self._record_daily_summary_outcome(
                guild.id,
                summary_date,
                status="failed",
                channel_id=channel_id,
                receipt_status="failed",
                detail={
                    "channel_id": channel_id,
                    "error_type": type(exc).__name__,
                    "reason": "discord_delivery_failed",
                },
            )
        return await self._record_daily_summary_outcome(
            guild.id,
            summary_date,
            status="sent",
            channel_id=channel_id,
            receipt_status="succeeded",
            detail={"channel_id": channel_id, "snapshot_version": snapshot.version},
        )

    async def _record_daily_summary_outcome(
        self,
        guild_id: int,
        summary_date: date,
        *,
        status: str,
        channel_id: int | None,
        receipt_status: str,
        detail: dict[str, str | int],
    ) -> DailySummaryResult:
        result = await self._daily_summaries.record_outcome(
            guild_id, summary_date, status=status, channel_id=channel_id
        )
        await self._service.record_action(
            guild_id,
            action="daily_health_summary",
            status=receipt_status,
            actor_id=None,
            detail=detail,
        )
        return result

    @tasks.loop(time=time(hour=12, tzinfo=UTC))
    async def daily_health_summary(self) -> None:
        today = datetime.now(UTC).date()
        for guild in self.guilds:
            await self.run_daily_summary_for_guild(guild, today)

    @daily_health_summary.before_loop
    async def before_daily_health_summary(self) -> None:
        await self.wait_until_ready()

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
        await self._live_runtime.bootstrap_guild(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._record_guild_connection(guild)
        await self._live_runtime.bootstrap_guild(guild)

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        await self._live_runtime.handle_presence(before, after)

    @tasks.loop(seconds=60)
    async def live_signal_reconciliation(self) -> None:
        await self._live_runtime.reconcile_all(self.guilds)

    @live_signal_reconciliation.before_loop
    async def before_live_signal_reconciliation(self) -> None:
        await self.wait_until_ready()

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
        await self._live_runtime.handle_member_leave(member)

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
