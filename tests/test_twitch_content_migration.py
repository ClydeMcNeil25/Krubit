"""Idempotent Phase 2A Twitch -> unified content ledger migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import CreatorAccount, Platform, creator_account_id
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


@pytest.mark.asyncio
async def test_migration_never_crashes_boot_on_an_owner_conflict(store: SQLiteStore) -> None:
    """A legitimate admin action (e.g. `/fetch creator transfer`) can leave the
    deterministic Twitch identity registered to a different owner than the Phase 2A
    session recorded. `migrate_twitch_content` must skip that one session, never raise,
    so `krubit run`'s boot sequence can never be crashed by it."""
    account_id = creator_account_id(Platform.TWITCH, "krucialstudios")
    await store.save_creator_account(
        CreatorAccount(
            guild_id=111,
            account_id=account_id,
            owner_member_id=999,  # different from the session's member_id (222)
            platform=Platform.TWITCH,
            handle="krucialstudios",
            canonical_url="https://twitch.tv/krucialstudios",
            external_id="krucialstudios",
            paused=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")

    linked = await migrate_twitch_content(store, guild_id=111)

    assert linked == 0
    assert await store.get_content_delivery(111, Platform.TWITCH, "stream-1") is None
    account = await store.get_creator_account(111, account_id)
    assert account is not None
    assert account.owner_member_id == 999  # untouched


@pytest.mark.asyncio
async def test_migrate_all_twitch_content_survives_one_guilds_owner_conflict(
    store: SQLiteStore,
) -> None:
    """A single guild's bad migration entry must not stop `migrate_all_twitch_content`
    from processing every other guild."""
    conflicted_account_id = creator_account_id(Platform.TWITCH, "krucialstudios")
    await store.save_creator_account(
        CreatorAccount(
            guild_id=111,
            account_id=conflicted_account_id,
            owner_member_id=999,
            platform=Platform.TWITCH,
            handle="krucialstudios",
            canonical_url="https://twitch.tv/krucialstudios",
            external_id="krucialstudios",
            paused=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1", guild_id=111)
    await seed_phase_2a_live_session(store, message_id=2002, stream_id="stream-2", guild_id=222)

    linked = await migrate_all_twitch_content(store)

    assert linked == 1
    assert await store.get_content_delivery(111, Platform.TWITCH, "stream-1") is None
    second = await store.get_content_delivery(222, Platform.TWITCH, "stream-2")
    assert second is not None and second.discord_message_id == 2002


@pytest.mark.asyncio
async def test_migration_never_re_pauses_an_operator_resumed_account(store: SQLiteStore) -> None:
    """Every `krubit run` boot replays this migration. It must never revert an
    operator's `/fetch creator resume` on the migrated Twitch account."""
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")
    account_id = creator_account_id(Platform.TWITCH, "krucialstudios")
    account = await store.get_creator_account(111, account_id)
    assert account is not None
    assert account.paused is True  # migration's own default

    # Operator resumes the account (mirrors CreatorRegistry.resume_account's effect).
    resumed = await store.save_creator_account(
        CreatorAccount(
            guild_id=account.guild_id,
            account_id=account.account_id,
            owner_member_id=account.owner_member_id,
            platform=account.platform,
            handle=account.handle,
            canonical_url=account.canonical_url,
            external_id=account.external_id,
            paused=False,
            created_at=account.created_at,
            updated_at=NOW,
        )
    )
    assert resumed.paused is False

    # A later boot replays the migration for the same (already-linked) session.
    await migrate_twitch_content(store, guild_id=111)

    after_reboot = await store.get_creator_account(111, account_id)
    assert after_reboot is not None
    assert after_reboot.paused is False
