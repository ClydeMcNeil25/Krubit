from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.scheduled_events import (
    ScheduledEventOutcome,
    ScheduledEventSynchronizer,
    ScheduledStreamEvent,
)
from krubit.domain.creator_signals import (
    ContentState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.storage.sqlite import ScheduledEventMapping, SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111


class FakeScheduledEvent:
    def __init__(
        self,
        event_id: int,
        *,
        name: str,
        description: str,
        start_time: datetime,
        end_time: datetime | None,
        location: str | None,
        status: discord.EventStatus,
    ) -> None:
        self.id = event_id
        self.name = name
        self.description = description
        self.start_time = start_time
        self.end_time = end_time
        self.location = location
        self.status = status
        self.edits: list[dict[str, object]] = []
        self.edit_failure: BaseException | None = None

    async def edit(self, **kwargs: object) -> None:
        if self.edit_failure is not None:
            raise self.edit_failure
        self.edits.append(kwargs)
        if kwargs.get("name") is not discord.utils.MISSING and "name" in kwargs:
            self.name = cast(str, kwargs["name"])
        if kwargs.get("description") is not discord.utils.MISSING and "description" in kwargs:
            self.description = cast(str, kwargs["description"])
        if kwargs.get("start_time") is not discord.utils.MISSING and "start_time" in kwargs:
            self.start_time = cast(datetime, kwargs["start_time"])
        if kwargs.get("end_time") is not discord.utils.MISSING and "end_time" in kwargs:
            self.end_time = cast(datetime, kwargs["end_time"])
        if kwargs.get("location") is not discord.utils.MISSING and "location" in kwargs:
            self.location = cast(str, kwargs["location"])
        if kwargs.get("status") is not discord.utils.MISSING and "status" in kwargs:
            self.status = cast(discord.EventStatus, kwargs["status"])


class FakeGuild:
    def __init__(
        self, guild_id: int = GUILD_ID, *, permissions: discord.Permissions | None = None
    ) -> None:
        self.id = guild_id
        self.events: dict[int, FakeScheduledEvent] = {}
        self.created_events = 0
        self.create_kwargs: list[dict[str, object]] = []
        self.create_failure: BaseException | None = None
        self._next_id = 9000
        granted = (
            permissions
            if permissions is not None
            else discord.Permissions(create_events=True, manage_events=True)
        )
        self.me = SimpleNamespace(guild_permissions=granted)

    async def create_scheduled_event(self, **kwargs: object) -> FakeScheduledEvent:
        if self.create_failure is not None:
            raise self.create_failure
        self._next_id += 1
        self.create_kwargs.append(kwargs)
        event = FakeScheduledEvent(
            self._next_id,
            name=cast(str, kwargs["name"]),
            description=cast(str, kwargs.get("description", "")),
            start_time=cast(datetime, kwargs["start_time"]),
            end_time=cast("datetime | None", kwargs.get("end_time")),
            location=cast("str | None", kwargs.get("location")),
            status=discord.EventStatus.scheduled,
        )
        self.events[event.id] = event
        self.created_events += 1
        return event

    def get_scheduled_event(self, event_id: int) -> FakeScheduledEvent | None:
        return self.events.get(event_id)


def as_guild(guild: FakeGuild) -> discord.Guild:
    return cast(discord.Guild, guild)


def account() -> CreatorAccount:
    return CreatorAccount(
        guild_id=GUILD_ID,
        account_id=creator_account_id(Platform.YOUTUBE, "yt-creator"),
        owner_member_id=222,
        platform=Platform.YOUTUBE,
        handle="Krucial Studios",
        canonical_url="https://www.youtube.com/@krucialstudios",
        external_id="yt-creator",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def scheduled_youtube_event(*, external_id: str) -> ScheduledStreamEvent:
    return ScheduledStreamEvent(
        guild_id=GUILD_ID,
        account_id=account().account_id,
        platform=Platform.YOUTUBE,
        external_id=external_id,
        state=ContentState.SCHEDULED,
        title="Weekly Krucial Stream",
        description="Join the weekly stream for news and Q&A.",
        canonical_url=f"https://www.youtube.com/watch?v={external_id}",
        scheduled_start_at=NOW + timedelta(hours=1),
        scheduled_end_at=NOW + timedelta(hours=3),
    )


def delayed_youtube_event(*, external_id: str) -> ScheduledStreamEvent:
    base = scheduled_youtube_event(external_id=external_id)
    return replace(
        base,
        state=ContentState.DELAYED,
        scheduled_start_at=base.scheduled_start_at + timedelta(minutes=30),
    )


def live_youtube_event(*, external_id: str) -> ScheduledStreamEvent:
    return replace(scheduled_youtube_event(external_id=external_id), state=ContentState.LIVE)


def ended_youtube_event(*, external_id: str) -> ScheduledStreamEvent:
    return replace(scheduled_youtube_event(external_id=external_id), state=ContentState.ENDED)


def cancelled_event(*, external_id: str = "yt-1") -> ScheduledStreamEvent:
    return replace(scheduled_youtube_event(external_id=external_id), state=ContentState.CANCELLED)


def mapping(
    *,
    external_id: str = "yt-1",
    owned_by_krubit: bool,
    discord_status: str = "scheduled",
    discord_event_id: int | None = 555,
) -> ScheduledEventMapping:
    return ScheduledEventMapping(
        guild_id=GUILD_ID,
        account_id=account().account_id,
        platform=Platform.YOUTUBE,
        external_id=external_id,
        discord_event_id=discord_event_id,
        discord_status=discord_status,
        owned_by_krubit=owned_by_krubit,
        content_hash="seed",
        created_at=NOW,
        updated_at=NOW,
    )


EnvFixture = tuple[ScheduledEventSynchronizer, SQLiteStore, FakeGuild]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[EnvFixture]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(GUILD_ID, True)
    await store.save_creator_account(account())
    guild = FakeGuild()
    sync = ScheduledEventSynchronizer(store, as_guild(guild), now=lambda: NOW)
    try:
        yield sync, store, guild
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_updates_exact_owned_event_and_never_name_matches(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env

    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    delayed = await sync.apply(delayed_youtube_event(external_id="yt-1"))

    assert isinstance(created, ScheduledEventMapping)
    assert isinstance(delayed, ScheduledEventMapping)
    assert created.discord_event_id == delayed.discord_event_id
    assert guild.created_events == 1


@pytest.mark.asyncio
async def test_sync_refuses_mapping_without_krubit_ownership_receipt(
    env: EnvFixture,
) -> None:
    sync, store, guild = env
    await store.save_scheduled_event_mapping(mapping(owned_by_krubit=False))

    outcome = await sync.apply(cancelled_event())

    assert outcome is ScheduledEventOutcome.SKIPPED_NOT_OWNED
    assert guild.created_events == 0
    assert all(event.edits == [] for event in guild.events.values())


@pytest.mark.asyncio
async def test_sync_creates_with_bounded_description_explicit_start_end_and_location(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    event = scheduled_youtube_event(external_id="yt-1")

    result = await sync.apply(event)

    assert isinstance(result, ScheduledEventMapping)
    kwargs = guild.create_kwargs[0]
    assert kwargs["name"] == event.title
    assert kwargs["description"] == event.description
    assert kwargs["location"] == event.canonical_url
    assert kwargs["start_time"] == event.scheduled_start_at
    assert kwargs["end_time"] == event.scheduled_end_at
    assert kwargs["entity_type"] is discord.EntityType.external


@pytest.mark.asyncio
async def test_sync_transitions_scheduled_through_active_to_completed(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None

    live = await sync.apply(live_youtube_event(external_id="yt-1"))
    ended = await sync.apply(ended_youtube_event(external_id="yt-1"))

    assert isinstance(live, ScheduledEventMapping)
    assert isinstance(ended, ScheduledEventMapping)
    assert live.discord_status == "active"
    assert ended.discord_status == "completed"
    assert guild.events[event_id].status == discord.EventStatus.completed
    assert guild.created_events == 1


@pytest.mark.asyncio
async def test_sync_transitions_scheduled_to_cancelled(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None

    cancelled = await sync.apply(cancelled_event(external_id="yt-1"))

    assert isinstance(cancelled, ScheduledEventMapping)
    assert cancelled.discord_status == "cancelled"
    assert guild.events[event_id].status == discord.EventStatus.canceled


@pytest.mark.asyncio
async def test_sync_rejects_direct_scheduled_to_completed_transition(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None

    outcome = await sync.apply(ended_youtube_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_INVALID_TRANSITION
    assert guild.events[event_id].status == discord.EventStatus.scheduled
    assert guild.events[event_id].edits == []


@pytest.mark.asyncio
async def test_sync_rejects_active_to_cancelled_transition(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None
    live = await sync.apply(live_youtube_event(external_id="yt-1"))
    assert isinstance(live, ScheduledEventMapping)
    edits_before = len(guild.events[event_id].edits)

    outcome = await sync.apply(cancelled_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_INVALID_TRANSITION
    assert guild.events[event_id].status == discord.EventStatus.active
    assert len(guild.events[event_id].edits) == edits_before


@pytest.mark.asyncio
async def test_sync_is_a_noop_when_nothing_about_the_event_changed(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    event = scheduled_youtube_event(external_id="yt-1")
    created = await sync.apply(event)
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None

    repeated = await sync.apply(event)

    assert isinstance(repeated, ScheduledEventMapping)
    assert repeated.discord_event_id == created.discord_event_id
    assert guild.events[event_id].edits == []
    assert guild.created_events == 1


@pytest.mark.asyncio
async def test_sync_skips_when_bot_lacks_scheduled_event_permissions(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(GUILD_ID, True)
    await store.save_creator_account(account())
    guild = FakeGuild(permissions=discord.Permissions.none())
    sync = ScheduledEventSynchronizer(store, as_guild(guild), now=lambda: NOW)
    try:
        outcome = await sync.apply(scheduled_youtube_event(external_id="yt-1"))

        assert outcome is ScheduledEventOutcome.SKIPPED_MISSING_PERMISSIONS
        assert guild.created_events == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_permission_loss_blocks_further_mutation_of_an_owned_event(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None
    guild.me.guild_permissions = discord.Permissions.none()

    outcome = await sync.apply(live_youtube_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_MISSING_PERMISSIONS
    assert guild.events[event_id].status == discord.EventStatus.scheduled
    assert guild.events[event_id].edits == []


@pytest.mark.asyncio
async def test_sync_skips_unregistered_creator_accounts(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    event = replace(scheduled_youtube_event(external_id="yt-1"), account_id="unregistered-account")

    outcome = await sync.apply(event)

    assert outcome is ScheduledEventOutcome.SKIPPED_NOT_REGISTERED
    assert guild.created_events == 0


@pytest.mark.asyncio
async def test_sync_skips_unsupported_platforms(
    env: EnvFixture,
) -> None:
    sync, _store, guild = env
    event = replace(
        scheduled_youtube_event(external_id="post-1"),
        platform=Platform.X,
        canonical_url="https://x.com/krucialstudios/status/post-1",
    )

    outcome = await sync.apply(event)

    assert outcome is ScheduledEventOutcome.SKIPPED_NOT_SUPPORTED
    assert guild.created_events == 0


@pytest.mark.asyncio
async def test_sync_skips_when_guild_is_disabled(
    env: EnvFixture,
) -> None:
    sync, store, guild = env
    await store.set_guild_enabled(GUILD_ID, False)

    outcome = await sync.apply(scheduled_youtube_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_NOT_ENABLED
    assert guild.created_events == 0


@pytest.mark.asyncio
async def test_restart_recovery_finds_mapping_by_exact_id_never_by_name(
    env: EnvFixture,
) -> None:
    sync, store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None
    # Simulate the live Discord object's name having drifted since the process
    # restarted (a moderator edit, or a fresh gateway cache) so a name-based search
    # would fail or match the wrong event; only the stored exact ID is trustworthy.
    guild.events[event_id].name = "Totally Unrelated Renamed Event"

    restarted_sync = ScheduledEventSynchronizer(store, as_guild(guild), now=lambda: NOW)
    live = await restarted_sync.apply(live_youtube_event(external_id="yt-1"))

    assert isinstance(live, ScheduledEventMapping)
    assert live.discord_event_id == created.discord_event_id
    assert guild.events[event_id].status == discord.EventStatus.active
    assert guild.created_events == 1


@pytest.mark.asyncio
async def test_create_failure_records_a_durable_failure_receipt(
    env: EnvFixture,
) -> None:
    sync, store, guild = env
    guild.create_failure = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom", headers={})), "boom"
    )

    outcome = await sync.apply(scheduled_youtube_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_DISCORD_ERROR
    assert guild.created_events == 0
    assert (
        await store.get_scheduled_event_mapping(GUILD_ID, Platform.YOUTUBE, "yt-1") is None
    )
    receipts = await store.list_content_receipts(GUILD_ID)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.action == "scheduled_event_create_failed"
    assert receipt.account_id == account().account_id
    assert receipt.platform is Platform.YOUTUBE
    assert receipt.external_id == "yt-1"
    assert receipt.detail["desired_status"] == "scheduled"
    assert "boom" in str(receipt.detail["error"])


@pytest.mark.asyncio
async def test_update_failure_records_a_durable_failure_receipt_and_keeps_prior_mapping(
    env: EnvFixture,
) -> None:
    sync, store, guild = env
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    assert isinstance(created, ScheduledEventMapping)
    event_id = created.discord_event_id
    assert event_id is not None
    guild.events[event_id].edit_failure = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom", headers={})), "boom"
    )

    outcome = await sync.apply(live_youtube_event(external_id="yt-1"))

    assert outcome is ScheduledEventOutcome.SKIPPED_DISCORD_ERROR
    # The prior mapping (still "scheduled", still owned) must remain untouched.
    unchanged = await store.get_scheduled_event_mapping(GUILD_ID, Platform.YOUTUBE, "yt-1")
    assert unchanged is not None
    assert unchanged.discord_status == "scheduled"
    assert unchanged.discord_event_id == event_id
    receipts = await store.list_content_receipts(GUILD_ID)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.action == "scheduled_event_update_failed"
    assert receipt.external_id == "yt-1"
    assert receipt.detail["desired_status"] == "active"
    assert receipt.detail["discord_event_id"] == str(event_id)
    assert "boom" in str(receipt.detail["error"])
