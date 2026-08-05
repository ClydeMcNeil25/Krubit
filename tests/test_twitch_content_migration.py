"""Idempotent Phase 2A Twitch -> unified content ledger migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import Platform, creator_account_id
from krubit.domain.live_signals import (
    LiveSignalConfig,
    StreamingObservation,
    TwitchLookup,
    TwitchLookupKind,
    TwitchStream,
)
from krubit.services.live_signals import (
    LiveSignalService,
    migrate_all_twitch_content,
    migrate_twitch_content,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeTwitch:
    def __init__(self, results: Sequence[TwitchLookup]) -> None:
        self._results = list(results)

    async def get_stream(self, login: str) -> TwitchLookup:
        return self._results.pop(0)


def observation(guild_id: int = 111) -> StreamingObservation:
    return StreamingObservation(
        guild_id=guild_id,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://twitch.tv/krucialstudios",
        activity_started_at=NOW - timedelta(minutes=2),
        observed_at=NOW,
    )


def stream(stream_id: str) -> TwitchStream:
    return TwitchStream(
        stream_id=stream_id,
        user_login="krucialstudios",
        user_name="Krucial Studios",
        title="Building Krucial Town",
        game_name="Just Chatting",
        started_at=NOW - timedelta(minutes=2),
        thumbnail_url="https://example.test/preview.jpg",
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def seed_phase_2a_live_session(
    store: SQLiteStore, *, message_id: int, stream_id: str, guild_id: int = 111
) -> None:
    """Drive the real Phase 2A pipeline so the seeded history matches production shape."""
    await store.set_live_signal_config(LiveSignalConfig(guild_id, 444, 333, NOW))
    service = LiveSignalService(
        store, FakeTwitch([TwitchLookup(TwitchLookupKind.LIVE, stream(stream_id))])
    )
    plan = await service.observe(observation(guild_id), now=NOW)
    assert plan.delivery_attempt is not None
    await service.record_role_result(
        guild_id, plan.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )
    await service.record_delivery_result(
        guild_id,
        plan.session_key,
        status="succeeded",
        channel_id=444,
        message_id=message_id,
        attempt=plan.delivery_attempt,
    )


@pytest.mark.asyncio
async def test_existing_twitch_delivery_is_linked_without_reannouncement(
    store: SQLiteStore,
) -> None:
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")

    await migrate_twitch_content(store, guild_id=111)

    delivery = await store.get_content_delivery(111, Platform.TWITCH, "stream-1")
    assert delivery is not None
    assert delivery.discord_message_id == 1001
    assert delivery.discord_channel_id == 444
    assert delivery.status == "delivered"
    assert await store.list_pending_content_deliveries(111) == []


@pytest.mark.asyncio
async def test_migration_is_idempotent_when_run_twice(store: SQLiteStore) -> None:
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")

    first_linked = await migrate_twitch_content(store, guild_id=111)
    second_linked = await migrate_twitch_content(store, guild_id=111)

    assert first_linked == 1
    assert second_linked == 1
    delivery = await store.get_content_delivery(111, Platform.TWITCH, "stream-1")
    assert delivery is not None
    assert delivery.discord_message_id == 1001
    assert delivery.attempt == 1
    # The Phase 2A tables are read-only rollback evidence: still present, untouched.
    active = await store.list_active_live_sessions(111)
    assert len(active) == 1
    assert active[0].stream is not None and active[0].stream.stream_id == "stream-1"


@pytest.mark.asyncio
async def test_migration_against_a_guild_with_no_phase_2a_history_links_nothing(
    store: SQLiteStore,
) -> None:
    linked = await migrate_twitch_content(store, guild_id=999)

    assert linked == 0
    assert await store.list_pending_content_deliveries(999) == []


@pytest.mark.asyncio
async def test_claimed_but_undelivered_session_is_not_linked(store: SQLiteStore) -> None:
    """A session whose delivery never actually succeeded must never be invented."""
    await store.set_live_signal_config(LiveSignalConfig(111, 444, 333, NOW))
    service = LiveSignalService(
        store, FakeTwitch([TwitchLookup(TwitchLookupKind.LIVE, stream("stream-2"))])
    )
    plan = await service.observe(observation(), now=NOW)
    assert plan.delivery_attempt is not None
    # No record_delivery_result call: the delivery stays 'claimed', never 'succeeded'.

    linked = await migrate_twitch_content(store, guild_id=111)

    assert linked == 0
    assert await store.get_content_delivery(111, Platform.TWITCH, "stream-2") is None


@pytest.mark.asyncio
async def test_migrate_all_twitch_content_covers_every_configured_guild(store: SQLiteStore) -> None:
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1", guild_id=111)
    await seed_phase_2a_live_session(store, message_id=2002, stream_id="stream-2", guild_id=222)

    linked = await migrate_all_twitch_content(store)

    assert linked == 2
    first = await store.get_content_delivery(111, Platform.TWITCH, "stream-1")
    second = await store.get_content_delivery(222, Platform.TWITCH, "stream-2")
    assert first is not None and first.discord_message_id == 1001
    assert second is not None and second.discord_message_id == 2002


@pytest.mark.asyncio
async def test_live_delivery_is_mirrored_into_the_content_ledger_without_migration(
    store: SQLiteStore,
) -> None:
    """Future (not historical) Twitch deliveries are mirrored automatically."""
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")

    # No explicit migrate_twitch_content call: LiveSignalService itself already
    # linked the identity the moment the delivery succeeded.
    delivery = await store.get_content_delivery(111, Platform.TWITCH, "stream-1")
    assert delivery is not None
    assert delivery.discord_message_id == 1001

    account_id = creator_account_id(Platform.TWITCH, "krucialstudios")
    account = await store.get_creator_account(111, account_id)
    assert account is not None
    assert account.handle == "krucialstudios"
