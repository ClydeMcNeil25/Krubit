"""Integration tests for `krubit.services.activity_ingestion.ActivityIngestionService`.

Exercises `ingest` against a real on-disk `SQLiteStore` (never mocked), matching
the `test_activity_ledger_storage.py` convention. The centerpiece is
`test_excluded_channel_event_never_reaches_storage`: it asserts zero rows were
actually written, not merely that `ingest` returned `False` — the plan is
explicit that a boolean-only assertion would not prove the exclusion is enforced
*before* storage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.domain.activity_ledger import (
    AttendanceAction,
    EventAttendanceEvent,
    ExclusionEntry,
    JoinEvent,
    LedgerEvent,
    MessageEvent,
    ReactionEvent,
    VoiceSessionEvent,
)
from krubit.services.activity_ingestion import ActivityIngestionService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "krubit.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(db_path)
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def ledger_event(
    *,
    guild_id: int = 111,
    member_id: int = 222,
    channel_id: int = 555,
    occurred_at: datetime = NOW,
) -> LedgerEvent:
    return MessageEvent(
        guild_id=guild_id, member_id=member_id, occurred_at=occurred_at, channel_id=channel_id
    )


def exclusion_entry(
    *,
    guild_id: int = 111,
    channel_id: int = 555,
    excluded_by: int = 999,
    reason: str = "staff lounge, never tracked",
    excluded_at: datetime = NOW,
) -> ExclusionEntry:
    return ExclusionEntry(
        guild_id=guild_id,
        channel_id=channel_id,
        excluded_by=excluded_by,
        reason=reason,
        excluded_at=excluded_at,
    )


@pytest.mark.asyncio
async def test_excluded_channel_event_never_reaches_storage(store: SQLiteStore) -> None:
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=555))
    service = ActivityIngestionService(store)

    stored = await service.ingest(ledger_event(guild_id=111, channel_id=555))

    assert stored is False
    assert await store.list_ledger_events(111, member_id=222) == ()


@pytest.mark.asyncio
async def test_excluded_channel_exclusion_is_guild_scoped(store: SQLiteStore) -> None:
    # Excluding channel 555 in guild 999 must not exclude the same channel ID in
    # guild 111 -- channel IDs are only unique within their own guild.
    await store.save_exclusion_entry(exclusion_entry(guild_id=999, channel_id=555))
    service = ActivityIngestionService(store)

    stored = await service.ingest(ledger_event(guild_id=111, channel_id=555))

    assert stored is True
    assert len(await store.list_ledger_events(111, member_id=222)) == 1


@pytest.mark.asyncio
async def test_non_excluded_channel_event_is_stored(store: SQLiteStore) -> None:
    service = ActivityIngestionService(store)

    stored = await service.ingest(ledger_event(guild_id=111, channel_id=555))

    assert stored is True
    assert len(await store.list_ledger_events(111, member_id=222)) == 1


@pytest.mark.asyncio
async def test_reaction_event_in_excluded_channel_never_reaches_storage(store: SQLiteStore) -> None:
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=555))
    service = ActivityIngestionService(store)
    event = ReactionEvent(guild_id=111, member_id=222, occurred_at=NOW, channel_id=555, emoji="🎉")

    stored = await service.ingest(event)

    assert stored is False
    assert await store.list_ledger_events(111, member_id=222) == ()


@pytest.mark.asyncio
async def test_voice_session_event_in_excluded_channel_never_reaches_storage(
    store: SQLiteStore,
) -> None:
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=555))
    service = ActivityIngestionService(store)
    event = VoiceSessionEvent(
        guild_id=111, member_id=222, occurred_at=NOW, left_at=LATER, channel_id=555
    )

    stored = await service.ingest(event)

    assert stored is False
    assert await store.list_ledger_events(111, member_id=222) == ()


@pytest.mark.asyncio
async def test_channel_less_event_kinds_ignore_channel_exclusions(store: SQLiteStore) -> None:
    # JoinEvent and EventAttendanceEvent name no channel at all, so a channel
    # exclusion has nothing to apply to -- they always proceed to storage.
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=555))
    service = ActivityIngestionService(store)

    join_stored = await service.ingest(JoinEvent(guild_id=111, member_id=222, occurred_at=NOW))
    attendance_stored = await service.ingest(
        EventAttendanceEvent(
            guild_id=111,
            member_id=222,
            occurred_at=NOW,
            scheduled_event_id=777,
            action=AttendanceAction.ADD,
        )
    )

    assert join_stored is True
    assert attendance_stored is True
    assert len(await store.list_ledger_events(111, member_id=222)) == 2
