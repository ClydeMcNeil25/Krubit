"""Thin discord.py adapter for Krubit's Phase 0 service."""

from __future__ import annotations

import io
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import discord
from aiohttp import BaseConnector
from discord import app_commands
from discord.ext import tasks

from krubit.config import Settings
from krubit.discord.activity_commands import ActivityActorContext, ActivityCommandService
from krubit.discord.activity_runtime import ActivityRuntime
from krubit.discord.cards import render_card, render_diff_card, render_health_card
from krubit.discord.content_commands import (
    ActorContext,
    ContentCommandService,
    CreatorCommands,
    NotificationCommands,
    _present_result,
)
from krubit.discord.content_runtime import ConnectorScheduler, ContentRuntime
from krubit.discord.events import guild_event
from krubit.discord.install import phase_three_intents, phase_two_intents, phase_two_permissions
from krubit.discord.inventory import InventoryCapture, capture_inventory
from krubit.discord.live_commands import LiveCommands, ReconcileCallback
from krubit.discord.live_runtime import LiveSignalRuntime
from krubit.discord.membership_announcements import MembershipAnnouncementRuntime
from krubit.discord.watchdog_commands import WatchdogActorContext, WatchdogCommandService
from krubit.discord.watchdog_runtime import WatchdogRuntime
from krubit.domain.companion import SnapshotRecord
from krubit.domain.creator_signals import ContentPlan, Platform
from krubit.domain.models import Card, CardField, GuildEvent, JSONValue
from krubit.integrations.base import Connector
from krubit.integrations.twitch import TwitchClient, UnavailableTwitchClient
from krubit.services.daily_summary import DailySummaryResult, DailySummaryService
from krubit.services.foundation import (
    AuthorizationError,
    FoundationService,
    GuildDisabledError,
)
from krubit.services.health import ActivityLedgerHealthFacts, HealthService, WatchdogHealthFacts
from krubit.services.incident_evidence import correlate_automod_action
from krubit.services.live_signals import LiveSignalService
from krubit.services.snapshots import SnapshotService, compare_inventory
from krubit.services.watch_window import WATCH_WINDOW_DURATION, WatchWindowService

_logger = logging.getLogger(__name__)

# Fallback used by `FetchCommands._inactivity_threshold` when
# `Settings.activity_ledger_inactivity_threshold_days` is unset (its default,
# per Task 7's design -- see that field's `config.py` comment). Matches
# `krubit.services.milestones._INACTIVITY_THRESHOLD`'s own 14-day figure, the
# only other named default for this exact semantic threshold already
# established in this codebase, rather than inventing a second unrelated
# default.
_DEFAULT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS = 14


def _receipt_detail(detail: dict[str, JSONValue]) -> dict[str, str | bool | int]:
    """Narrow a `WatchdogCommandService` `CommandResult.detail` (`dict[str,
    JSONValue]`) down to `record_action`'s stricter `dict[str, str | bool | int]`.
    Every detail dict this command surface produces already only ever holds
    str/bool/int values; anything else is dropped rather than raising, matching
    this module's existing tolerant `int(count) if isinstance(count, (int, float))
    else 0`-style coercions elsewhere in `FetchCommands`.
    """
    return {key: value for key, value in detail.items() if isinstance(value, (str, bool, int))}


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
        content_runtime: ContentRuntime | None = None,
        watchdog_facts: WatchdogHealthFacts | None = None,
        activity_ledger_facts: ActivityLedgerHealthFacts | None = None,
        activity_ledger_inactivity_threshold_days: int | None = None,
    ) -> None:
        super().__init__(name="fetch", description="Ask Krubit to fetch a system result")
        self._service = service
        self._snapshots = SnapshotService(service.store)
        self._health = HealthService()
        self._live_service = live_service
        self._presence_intent = presence_intent
        self._twitch_credentials = twitch_credentials
        self._twitch_available = twitch_available
        # See `WatchdogHealthFacts`'s own docstring for why this is a process-level
        # fact bundle passed in at construction rather than derived from a captured
        # `SnapshotRecord` -- it mirrors `presence_intent`/`twitch_credentials`/
        # `twitch_available` immediately above. `None` (the default, and what both
        # test-only `FetchCommands(...)` construction sites in this repository still
        # use) means "no watchdog facts were supplied," which `HealthService`
        # interprets as "report nothing about Watchdog" rather than "Watchdog is
        # disabled" -- so pre-Phase-3 callers see no behavior change at all.
        self._watchdog_facts = watchdog_facts
        # See `ActivityLedgerHealthFacts`'s own docstring -- mirrors `_watchdog_facts`
        # immediately above exactly: `None` (the default, and what every pre-Phase-4
        # `FetchCommands(...)` construction site still uses) means "no activity-ledger
        # facts were supplied," so pre-Phase-4 callers see no behavior change at all.
        self._activity_ledger_facts = activity_ledger_facts
        # Share the same `ContentRuntime` instance `KrubitBot` polls into, rather than
        # letting `ContentCommandService` default-construct its own: that default
        # always has `social_delivery_enabled=True`, which would let `/fetch
        # notifications retry` bypass the `KRUBIT_SOCIAL_DELIVERY_ENABLED=false` shadow-
        # mode gate that every scheduler-driven delivery already honors.
        self._content_commands = ContentCommandService(
            service.store, runtime=content_runtime or ContentRuntime(service.store)
        )
        self._watchdog_commands = WatchdogCommandService(service.store)
        self._activity_commands = ActivityCommandService(service.store)
        # See the module-level `_DEFAULT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS`
        # comment: `Settings.activity_ledger_inactivity_threshold_days` is `None`
        # by default (Task 7's deliberate "per-query parameter, never seeded"
        # design), so a documented fallback is resolved here, once, rather than
        # each command re-deriving its own default.
        self._inactivity_threshold = timedelta(
            days=activity_ledger_inactivity_threshold_days
            if activity_ledger_inactivity_threshold_days is not None
            else _DEFAULT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS
        )
        self.add_command(BackupCommands(self))
        self.add_command(AdminCommands(self))
        self.add_command(SniffCommands(self))
        self.add_command(LiveCommands(self, live_service, reconcile_callback))
        self.add_command(CreatorCommands(self, self._content_commands))
        self.add_command(NotificationCommands(self, self._content_commands))

    @property
    def snapshots(self) -> SnapshotService:
        return self._snapshots

    @property
    def inactivity_threshold(self) -> timedelta:
        """The resolved `/fetch inactive`/`/fetch activity` inactivity threshold --
        `Settings.activity_ledger_inactivity_threshold_days` when configured, else
        the documented `_DEFAULT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS`
        fallback. Exposed as a public property (rather than requiring a test to
        reach into `_inactivity_threshold`) so this resolution is verifiable
        without `reportPrivateUsage` friction."""
        return self._inactivity_threshold

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

    async def authorize_public(
        self, interaction: discord.Interaction, action: str
    ) -> tuple[discord.Guild, int] | None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return None
        user = interaction.user
        try:
            await self._service.authorize_member(interaction.guild_id, action=action)
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

    @app_commands.command(name="latest", description="Fetch the latest observed creator content")
    @app_commands.guild_only()
    async def latest(self, interaction: discord.Interaction) -> None:
        context = await self.authorize_public(interaction, "fetch_latest")
        if context is None:
            return
        guild, actor_id = context
        user = interaction.user
        is_admin = isinstance(user, discord.Member) and (
            user.guild_permissions.manage_guild or user.guild_permissions.administrator
        )
        actor = ActorContext(
            guild_id=guild.id, member_id=actor_id, is_admin=is_admin, has_creator_role=False
        )
        result = await self._content_commands.latest(actor=actor)
        assert result.card is not None
        item_count = result.detail.get("item_count", 0)
        await self.finish(
            interaction,
            action="fetch_latest",
            actor_id=actor_id,
            embed=render_card(result.card),
            detail={"item_count": int(item_count) if isinstance(item_count, (int, float)) else 0},
        )

    @app_commands.command(name="schedule", description="Fetch Scheduled Event status")
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction) -> None:
        context = await self.authorize_public(interaction, "fetch_schedule")
        if context is None:
            return
        guild, actor_id = context
        user = interaction.user
        is_admin = isinstance(user, discord.Member) and (
            user.guild_permissions.manage_guild or user.guild_permissions.administrator
        )
        actor = ActorContext(
            guild_id=guild.id, member_id=actor_id, is_admin=is_admin, has_creator_role=False
        )
        result = await self._content_commands.schedule_status(actor=actor)
        assert result.card is not None
        count = result.detail.get("count", 0)
        await self.finish(
            interaction,
            action="fetch_schedule",
            actor_id=actor_id,
            embed=render_card(result.card),
            detail={"count": int(count) if isinstance(count, (int, float)) else 0},
        )

    # -- Phase 4 Activity Ledger: staff-or-self reads -----------------------------
    #
    # `activity` and `milestones` accept a self-view: they deliberately do NOT use
    # `self.authorize` (which unconditionally requires Manage Guild), matching
    # `content_commands.py`'s `_actor_context` precedent for staff-or-self
    # authority instead. The Discord-layer `member` parameter defaults to the
    # caller when omitted, but that default is never trusted as the actual
    # authority check -- `ActivityCommandService.activity`/`milestones` each
    # re-derive `self_view` from `target.member_id == actor.member_id`
    # themselves and deny a non-staff caller who explicitly names someone else.

    async def _activity_actor(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, ActivityActorContext] | None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return None
        user = interaction.user
        if not isinstance(user, discord.Member):
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return None
        is_staff = user.guild_permissions.manage_guild or user.guild_permissions.administrator
        return interaction.guild, ActivityActorContext(
            guild_id=interaction.guild_id, member_id=user.id, is_staff=is_staff
        )

    @app_commands.command(
        name="activity", description="Fetch a member's participation trend, or your own"
    )
    @app_commands.guild_only()
    async def activity(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        resolved = await self._activity_actor(interaction)
        if resolved is None:
            return
        guild, actor = resolved
        target = (
            ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
            if member is not None
            else actor
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._activity_commands.activity(
            actor=actor, target=target, inactivity_threshold=self._inactivity_threshold
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="milestones", description="Fetch a member's milestones, or your own"
    )
    @app_commands.guild_only()
    async def milestones(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        resolved = await self._activity_actor(interaction)
        if resolved is None:
            return
        guild, actor = resolved
        target = (
            ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
            if member is not None
            else actor
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._activity_commands.milestones(actor=actor, target=target)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="member-export", description="Export a member's activity-ledger data, or your own"
    )
    @app_commands.guild_only()
    async def member_export(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        resolved = await self._activity_actor(interaction)
        if resolved is None:
            return
        guild, actor = resolved
        target = (
            ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
            if member is not None
            else actor
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result, payload = await self._activity_commands.export_member(actor=actor, target=target)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        if payload is None:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        file = discord.File(io.BytesIO(payload), filename=f"krubit-export-{target.member_id}.json")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)


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
        diff = await self._parent.snapshots.preview_restore(guild.id, target.snapshot_id, inventory)
        await self._parent.finish(
            interaction,
            action="fetch_backup_preview",
            actor_id=actor_id,
            embed=render_diff_card(diff, title="Fetched: Restore Preview"),
            detail={"change_count": len(diff.items), "mutated": False},
        )


class AdminCommands(app_commands.Group):
    """`/fetch admin` -- general-operational admin commands (status, test
    card, server health, config changes, permissions, integrations) plus
    Phase 4 activity-ledger maintenance commands (member profile, newcomers,
    inactive, returning members, recognition candidates, member deletion,
    channel-exclusion configuration), consolidated into a single admin-gated
    group to free slots against Discord's 25-child-per-group cap on
    `FetchCommands` itself. `member-export` is deliberately NOT here -- it is
    staff-or-self, not admin-only, so it stays a flat `FetchCommands`
    command."""

    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(name="admin", description="Administrative and activity-ledger commands")
        self._parent = parent

    @app_commands.command(name="status", description="Fetch Krubit's Phase 1 status")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_status")
        if context is None:
            return
        guild, actor_id = context
        snapshot = await self._parent._service.status(guild.id, actor_id=actor_id)
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
            card = await self._parent._service.test_card(
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
        context = await self._parent.authorize(interaction, "fetch_server_health")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self._parent.capture(guild)
        report = self._parent._health.server_health(
            snapshot,
            now=datetime.now(UTC),
            database_healthy=True,
            gateway_ready=True,
            watchdog=self._parent._watchdog_facts,
            activity_ledger=self._parent._activity_ledger_facts,
        )
        await self._parent.finish(
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
        context = await self._parent.authorize(interaction, "fetch_changes")
        if context is None:
            return
        guild, actor_id = context
        previous = await self._parent.snapshots.latest(guild.id)
        _, current = await self._parent.capture(guild)
        diff = (
            compare_inventory(current.content, current.content)
            if previous is None or previous.snapshot_id == current.snapshot_id
            else compare_inventory(previous.content, current.content)
        )
        await self._parent.finish(
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
        context = await self._parent.authorize(interaction, "fetch_permissions")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self._parent.capture(guild)
        report = self._parent._health.permission_health(snapshot)
        await self._parent.finish(
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
        context = await self._parent.authorize(interaction, "fetch_integrations")
        if context is None:
            return
        guild, actor_id = context
        _, snapshot = await self._parent.capture(guild)
        connector_authorizations = (
            await self._parent._service.store.list_connector_authorization_status(guild.id)
        )
        report = self._parent._health.integration_health(
            snapshot,
            watchdog=self._parent._watchdog_facts,
            activity_ledger=self._parent._activity_ledger_facts,
            connector_authorizations=connector_authorizations,
        )
        await self._parent.finish(
            interaction,
            action="fetch_integrations",
            actor_id=actor_id,
            embed=render_health_card(report, title="Fetched: Integrations"),
            detail={"finding_count": len(report.findings)},
        )

    @app_commands.command(name="member", description="Fetch a member's detailed activity profile")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def member(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_member")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        target = ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
        result = await self._parent._activity_commands.member(actor=actor, target=target)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_member",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="newcomers", description="Fetch guild-wide newcomer activation status"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def newcomers(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_newcomers")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.newcomers(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_newcomers",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="inactive", description="Fetch guild-wide inactive members"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def inactive(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_inactive")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.inactive(
            actor=actor, inactivity_threshold=self._parent._inactivity_threshold
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_inactive",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(name="returning", description="Fetch members returning from inactivity")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def returning(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_activity_admin_returning")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.returning(
            actor=actor, inactivity_threshold=self._parent._inactivity_threshold
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_activity_admin_returning",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="recognition-candidates", description="Fetch a factual recognition shortlist"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def recognition_candidates(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(
            interaction, "fetch_activity_admin_recognition_candidates"
        )
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.recognition_candidates(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_activity_admin_recognition_candidates",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="member-delete", description="Permanently delete a member's activity-ledger data"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def member_delete(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_activity_admin_member_delete")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        target = ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
        result = await self._parent._activity_commands.delete_member(actor=actor, target=target)
        await _present_result(
            interaction,
            result,
            confirm=lambda: self._parent._activity_commands.delete_member(
                actor=actor, target=target, confirm=True
            ),
        )

    @app_commands.command(
        name="exclude-channel", description="Exclude a channel from activity-ledger tracking"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def exclude_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_activity_admin_exclude_channel")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.exclude_channel(
            actor=actor, channel_id=channel.id, reason=reason
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_activity_admin_exclude_channel",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="exclusions", description="Fetch guild-wide activity-ledger channel exclusions"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def exclusions(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_activity_admin_exclusions")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.exclusions(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_activity_admin_exclusions",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="retention", description="Fetch guild-wide cohort retention (7-day and 30-day)"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def retention(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_retention")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.retention(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_retention",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="community-pulse", description="Fetch a guild-wide factual activity summary"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def community_pulse(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_community_pulse")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._activity_commands.community_pulse(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_community_pulse",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="leaderboard", description="Fetch the meaningful-action leaderboard for a year"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, 2020, datetime.now(UTC).year] | None = None,
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_admin_leaderboard")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        resolved_year = year if year is not None else datetime.now(UTC).year
        result = await self._parent._activity_commands.leaderboard(
            actor=actor, year=resolved_year
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_admin_leaderboard",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )


class SniffCommands(app_commands.Group):
    """`/fetch sniff` -- Watchdog risk-assessment and incident-evidence
    commands, consolidated from five flat `/fetch` commands to free slots
    against Discord's 25-child-per-group cap. The member-assessment command
    is named `member` here (not `sniff`) since a group cannot contain a
    same-named subcommand."""

    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(name="sniff", description="Watchdog risk assessment and incident evidence")
        self._parent = parent

    @app_commands.command(
        name="member", description="Fetch a member's current or most recent Entry Sniff assessment"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_member")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        target = WatchdogActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
        result = await self._parent._watchdog_commands.sniff(actor=actor, target=target)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_member",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="report",
        description="Fetch a guild-wide Watchdog sniff report: high-band joins and open windows",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def report(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_report")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.sniff_report(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_report",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="incident", description="Fetch one Watchdog incident's full evidence packet"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def incident(self, interaction: discord.Interaction, incident_id: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_incident")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.incident(
            actor=actor, incident_id=incident_id
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_incident",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="evidence", description="Fetch one Watchdog incident's raw, redacted evidence export"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def evidence(self, interaction: discord.Interaction, incident_id: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_evidence")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.evidence(
            actor=actor, incident_id=incident_id
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_evidence",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="watchlist", description="Fetch this guild's open watch windows and allow/block lists"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def watchlist(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_watchlist")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.watchlist(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_watchlist",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
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
        content_connectors: Mapping[Platform, Connector] | None = None,
        request_message_content_intent: bool | None = None,
    ) -> None:
        # `phase_three_intents()`'s new `message_content`/`messages` flags are only
        # requested when `watchdog_enabled` is explicitly on: requesting a privileged
        # intent unconditionally would require every operator to enable Message
        # Content in the Discord Developer Portal before the bot could connect at
        # all, even for a deployment that never opts into Watchdog -- breaking the
        # "safe by default" promise `watchdog_enabled=False` is supposed to keep.
        #
        # `request_message_content_intent` lets a caller override that default
        # decision independently of `settings.watchdog_enabled`. This exists for
        # exactly one caller: `krubit.__main__._run_bot`'s fallback retry after a
        # `discord.PrivilegedIntentsRequired` failure -- see that function's
        # docstring for why. Passing `False` here still leaves every other Watchdog
        # behavior (`watchdog_enabled`-gated join-signal detection, sweeps, the four
        # guild-scoped detectors) fully functional; only the message-content-
        # dependent signals become honestly unavailable, matching the design doc's
        # "degrades to join-signal-only detection ... rather than failing to start."
        should_request_message_content = (
            settings.watchdog_enabled
            if request_message_content_intent is None
            else request_message_content_intent
        )
        selected_intents = (
            phase_three_intents() if should_request_message_content else phase_two_intents()
        )
        # Phase 4 Activity Ledger's three intents (`guild_reactions`/`voice_states`/
        # `guild_scheduled_events`) are all non-privileged, but are still only
        # requested when `activity_ledger_enabled` is explicitly on -- matching every
        # other phase's "request a feature's intents only when that feature's flag
        # is on" convention (see `phase_three_intents()`'s own docstring for
        # `message_content`). Added on top of whichever intent set was already
        # selected above rather than via `phase_four_intents()` directly, since that
        # function unconditionally inherits `phase_three_intents()`'s privileged
        # `message_content` -- which must stay gated on `watchdog_enabled` alone.
        if settings.activity_ledger_enabled:
            selected_intents.guild_reactions = True
            selected_intents.voice_states = True
            selected_intents.guild_scheduled_events = True
        super().__init__(
            intents=selected_intents,
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
        self._content_runtime = ContentRuntime(
            service.store, social_delivery_enabled=settings.social_delivery_enabled
        )
        self._content_connectors = (
            dict(content_connectors or {}) if settings.creator_signals_enabled else {}
        )
        self._content_scheduler_enabled = bool(self._content_connectors)
        self._content_scheduler = ConnectorScheduler(
            service.store,
            self._content_connectors,
            guild_ids=lambda: tuple(guild.id for guild in self.guilds),
            on_plans=self._deliver_content_plans,
        )
        watch_window_duration = (
            timedelta(hours=settings.watchdog_watch_window_hours)
            if settings.watchdog_watch_window_hours is not None
            else WATCH_WINDOW_DURATION
        )
        self._watchdog_runtime = WatchdogRuntime(
            service.store,
            watchdog_enabled=settings.watchdog_enabled,
            watchdog_notifications_enabled=settings.watchdog_notifications_enabled,
            staff_channel_id=settings.staff_channel_id,
            guild_lookup=self.get_guild,
            guild_ids=lambda: tuple(guild.id for guild in self.guilds),
            message_content_available=self.intents.message_content,
            watch_window=WatchWindowService(service.store, duration=watch_window_duration),
        )
        self._activity_runtime = ActivityRuntime(
            service.store,
            activity_ledger_enabled=settings.activity_ledger_enabled,
            guild_ids=lambda: tuple(guild.id for guild in self.guilds),
            default_retention_days=settings.activity_ledger_retention_days,
            default_retention_policy_owner_id=settings.application_id,
            excluded_channel_ids=settings.activity_ledger_excluded_channel_ids,
        )
        self._membership_announcements = MembershipAnnouncementRuntime(service.store)
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
                content_runtime=self._content_runtime,
                watchdog_facts=WatchdogHealthFacts(
                    enabled=settings.watchdog_enabled,
                    notifications_enabled=settings.watchdog_notifications_enabled,
                    message_content_available=self.intents.message_content,
                ),
                activity_ledger_facts=ActivityLedgerHealthFacts(
                    enabled=settings.activity_ledger_enabled,
                    retention_configured=settings.activity_ledger_retention_days is not None,
                    exclusions_configured=bool(
                        settings.activity_ledger_excluded_channel_ids
                    ),
                ),
                activity_ledger_inactivity_threshold_days=(
                    settings.activity_ledger_inactivity_threshold_days
                ),
            )
        )

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.daily_health_summary.start()
        if self._live_runtime_enabled:
            self.live_signal_reconciliation.start()
        if self._content_scheduler_enabled:
            self.content_scheduler_cycle.start()
        if self._settings.watchdog_enabled:
            self.watchdog_sweep_cycle.start()
        if self._settings.activity_ledger_enabled:
            self.activity_ledger_sweep_cycle.start()

    async def close(self) -> None:
        self.daily_health_summary.cancel()
        self.live_signal_reconciliation.cancel()
        self.content_scheduler_cycle.cancel()
        self.watchdog_sweep_cycle.cancel()
        self.activity_ledger_sweep_cycle.cancel()
        await super().close()

    async def _deliver_content_plans(self, guild_id: int, plans: tuple[ContentPlan, ...]) -> None:
        """Apply one connector poll's freshly claimed plans to their real Discord guild.

        A guild the scheduler polled for but that is not (or no longer) in this
        process's cache — for example between a restart and the next `GUILD_CREATE`
        — simply leaves the claimed deliveries `pending`; `recover_pending` sweeps
        them once `on_guild_available` runs, so nothing already claimed is lost.
        """
        guild = self.get_guild(guild_id)
        if guild is not None:
            await self._content_runtime.apply_plans(guild, plans)

    @tasks.loop(seconds=60)
    async def content_scheduler_cycle(self) -> None:
        """Run one polling cycle without ever letting the loop itself stop.

        `ConnectorScheduler.run_cycle` already isolates every per-guild and per-job
        failure internally, but `discord.ext.tasks.Loop` only auto-retries a narrow set
        of reconnect-worthy exceptions (`OSError`/gateway errors) — any other unhandled
        exception escaping this coroutine stops the loop *permanently* until the
        process restarts, silently killing polling for every guild and platform. This
        `try`/`except` is the actual guarantee that one cycle's unexpected failure never
        does that; `content_scheduler_cycle.error` below only logs it.
        """
        try:
            await self._content_scheduler.run_cycle()
        except Exception:
            _logger.exception("content_scheduler_cycle: unhandled exception during run_cycle")

    @content_scheduler_cycle.before_loop
    async def before_content_scheduler_cycle(self) -> None:
        await self.wait_until_ready()

    @content_scheduler_cycle.error
    async def on_content_scheduler_cycle_error(self, exception: BaseException) -> None:
        _logger.exception(
            "content_scheduler_cycle: exception escaped the loop body", exc_info=exception
        )

    @tasks.loop(seconds=60)
    async def watchdog_sweep_cycle(self) -> None:
        """Run one Watchdog sweep cycle without ever letting the loop itself stop.

        `WatchdogRuntime.sweep_cycle` already isolates every per-guild and per-detector
        failure internally (mirroring `ConnectorScheduler.run_cycle`'s own discipline),
        but this outer `try`/`except` is the same second-layer guarantee
        `content_scheduler_cycle` relies on: `discord.ext.tasks.Loop` only auto-retries
        a narrow set of reconnect-worthy exceptions, so anything else escaping this
        coroutine would otherwise stop watch-window expiry and raid/spam-wave/webhook-
        abuse/permission-risk detection permanently until a process restart. Sixty
        seconds matches `content_scheduler_cycle`'s own cadence and is comfortably
        finer-grained than every detector's own correlation window (the shortest,
        `SpamWaveDetector`'s 5 minutes), so a window is never open materially longer
        than its configured duration and a fired detector is never left unnotified for
        more than one cycle.
        """
        try:
            await self._watchdog_runtime.sweep_cycle(datetime.now(UTC))
        except Exception:
            _logger.exception("watchdog_sweep_cycle: unhandled exception during sweep_cycle")

    @watchdog_sweep_cycle.before_loop
    async def before_watchdog_sweep_cycle(self) -> None:
        await self.wait_until_ready()

    @watchdog_sweep_cycle.error
    async def on_watchdog_sweep_cycle_error(self, exception: BaseException) -> None:
        _logger.exception(
            "watchdog_sweep_cycle: exception escaped the loop body", exc_info=exception
        )

    @tasks.loop(seconds=60)
    async def activity_ledger_sweep_cycle(self) -> None:
        """Run one activity-ledger retention sweep cycle without ever letting the
        loop itself stop -- the same second-layer guarantee `watchdog_sweep_cycle`/
        `content_scheduler_cycle` both rely on: `ActivityRuntime.sweep_cycle` already
        isolates every per-guild failure internally, but `discord.ext.tasks.Loop`
        only auto-retries a narrow set of reconnect-worthy exceptions, so anything
        else escaping this coroutine would otherwise stop retention pruning
        permanently until a process restart.
        """
        try:
            await self._activity_runtime.sweep_cycle(datetime.now(UTC))
        except Exception:
            _logger.exception(
                "activity_ledger_sweep_cycle: unhandled exception during sweep_cycle"
            )

    @activity_ledger_sweep_cycle.before_loop
    async def before_activity_ledger_sweep_cycle(self) -> None:
        await self.wait_until_ready()

    @activity_ledger_sweep_cycle.error
    async def on_activity_ledger_sweep_cycle_error(self, exception: BaseException) -> None:
        _logger.exception(
            "activity_ledger_sweep_cycle: exception escaped the loop body", exc_info=exception
        )

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
                watchdog=WatchdogHealthFacts(
                    enabled=self._settings.watchdog_enabled,
                    notifications_enabled=self._settings.watchdog_notifications_enabled,
                    message_content_available=self.intents.message_content,
                ),
                activity_ledger=ActivityLedgerHealthFacts(
                    enabled=self._settings.activity_ledger_enabled,
                    retention_configured=self._settings.activity_ledger_retention_days is not None,
                    exclusions_configured=bool(
                        self._settings.activity_ledger_excluded_channel_ids
                    ),
                ),
            )
            await channel.send(embed=render_health_card(report, title="Krubit Daily Server Health"))
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
        await self._content_runtime.recover_pending(guild)
        await self._watchdog_runtime.bootstrap_guild(guild.id, datetime.now(UTC))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._record_guild_connection(guild)
        await self._live_runtime.bootstrap_guild(guild)
        await self._content_runtime.recover_pending(guild)
        await self._watchdog_runtime.bootstrap_guild(guild.id, datetime.now(UTC))

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
        await self._watchdog_runtime.on_member_join(member, datetime.now(UTC))
        await self._activity_runtime.on_member_join(member, datetime.now(UTC))
        await self._membership_announcements.on_member_join(member)

    async def on_message(self, message: discord.Message) -> None:
        await self._watchdog_runtime.on_message(message, datetime.now(UTC))
        await self._activity_runtime.on_message(message, datetime.now(UTC))

    async def on_member_remove(self, member: discord.Member) -> None:
        await self._ingest_change(
            "member_left", member.guild.id, member.id, {"bot": member.bot}, None
        )
        await self._live_runtime.handle_member_leave(member)
        await self._activity_runtime.on_member_remove(member, datetime.now(UTC))
        await self._membership_announcements.on_member_remove(member)

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
        await self._activity_runtime.on_member_update(before, after, datetime.now(UTC))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._activity_runtime.on_reaction_add(payload, datetime.now(UTC))

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._activity_runtime.on_reaction_remove(payload, datetime.now(UTC))

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await self._activity_runtime.on_voice_state_update(
            member, before, after, datetime.now(UTC)
        )

    async def on_scheduled_event_user_add(
        self, event: discord.ScheduledEvent, user: discord.User | discord.Member
    ) -> None:
        await self._activity_runtime.on_scheduled_event_user_add(event, user, datetime.now(UTC))

    async def on_scheduled_event_user_remove(
        self, event: discord.ScheduledEvent, user: discord.User | discord.Member
    ) -> None:
        await self._activity_runtime.on_scheduled_event_user_remove(
            event, user, datetime.now(UTC)
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
        # Feed the raw callback into `WebhookAbuseDetector`'s in-memory same-channel
        # cache independently of the durable `_ingest_change` write above -- see
        # `WatchdogRuntime`'s module docstring for why both this and `on_message`'s
        # `SpamWaveDetector.record_message` call must exist (Task 5's review flagged
        # wiring only one of the two as a silent, recurring detection gap).
        await self._watchdog_runtime.on_webhooks_update(channel, datetime.now(UTC))

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
        # Correlate, never enforce: `correlate_automod_action` only reads the event
        # Discord already fired and acted on above; it makes no Discord API call and
        # takes no action of its own. Recording it as a sniff receipt is evidence-only
        # bookkeeping (redacted at storage, matching every other receipt write in this
        # class) — it does not feed a live risk-band re-evaluation or open/escalate an
        # incident here, since no Phase 3 watchdog service is wired into `KrubitBot`
        # yet (that wiring is later work; see `krubit.services.incident_evidence`'s
        # module docstring).
        #
        # `matched_keyword`/`matched_content` are message content, so
        # `correlate_automod_action` only includes them for a member with an actively
        # open watch window, matching the plan's global message-content boundary —
        # this lookup is what supplies that answer (the function itself stays
        # side-effect-free).
        #
        # Gated on `watchdog_enabled`, matching every other Watchdog data-producing
        # path in `WatchdogRuntime` (each of whose methods checks this flag as their
        # first statement): with the flag off, this handler must still record the
        # Phase-1-style `automod_action_executed` change above (that ingestion predates
        # Watchdog and is not Watchdog-specific), but it must not read
        # `list_open_watch_windows` or write a Watchdog `sniff_receipt`, or a disabled
        # deployment would still see Watchdog storage reads/writes on every AutoMod
        # action, contradicting the ops doc's "disabling the flag means no Watchdog
        # activity" claim.
        if self._settings.watchdog_enabled:
            open_windows = await self._service.store.list_open_watch_windows(execution.guild_id)
            member_has_open_watch_window = any(
                window.member_id == execution.user_id for window in open_windows
            )
            signal = correlate_automod_action(
                execution,
                datetime.now(UTC),
                member_has_open_watch_window=member_has_open_watch_window,
            )
            if signal is not None:
                await self._service.store.record_sniff_receipt(
                    guild_id=execution.guild_id,
                    receipt_id=f"automod-correlation:{uuid4().hex}",
                    member_id=execution.user_id,
                    action="automod_action_correlated",
                    detail={
                        "signal_name": signal.name,
                        "detail": signal.detail,
                        "weight": signal.weight,
                        "confidence": signal.confidence,
                    },
                    created_at=datetime.now(UTC),
                )
