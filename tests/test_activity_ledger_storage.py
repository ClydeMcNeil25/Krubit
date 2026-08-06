"""Integration tests for guild-scoped activity ledger (Phase 4) persistence.

Exercises `SQLiteStore`'s activity ledger methods against a real on-disk SQLite
database (never mocked), matching the `test_watchdog_storage.py` convention. Per the
plan's own verification step, guild-isolation and deletion-completeness get dedicated
coverage, not just happy paths: `delete_member_ledger_data` is the single most
important behavior this module tests, since an incomplete deletion is a privacy bug,
not just a failing assertion.
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
    Milestone,
    MilestoneKind,
    ModerationReceiptEvent,
    ReactionEvent,
    RetentionPolicy,
    RoleChangeAction,
    RoleChangeEvent,
    VoiceSessionEvent,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def ledger_event(
    *,
    guild_id: int = 111,
    member_id: int = 222,
    occurred_at: datetime = NOW,
) -> LedgerEvent:
    return MessageEvent(
        guild_id=guild_id,
        member_id=member_id,
        occurred_at=occurred_at,
        channel_id=333,
    )


def milestone(
    *,
    guild_id: int = 111,
    member_id: int = 222,
    kind: MilestoneKind = MilestoneKind.MESSAGE_COUNT,
    reached_at: datetime = NOW,
    detail: str = "reached 10 messages",
) -> Milestone:
    return Milestone(
        guild_id=guild_id,
        member_id=member_id,
        kind=kind,
        reached_at=reached_at,
        detail=detail,
    )


def exclusion_entry(
    *,
    guild_id: int = 111,
    channel_id: int = 444,
    excluded_by: int = 555,
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


def retention_policy(
    *,
    guild_id: int = 111,
    max_age_days: int = 90,
    updated_by: int = 555,
    updated_at: datetime = NOW,
) -> RetentionPolicy:
    return RetentionPolicy(
        guild_id=guild_id,
        max_age_days=max_age_days,
        updated_by=updated_by,
        updated_at=updated_at,
    )


# -- Ledger events ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_events_are_guild_scoped(store: SQLiteStore) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.record_ledger_event(ledger_event(guild_id=999, member_id=222))
    assert len(await store.list_ledger_events(111, member_id=222)) == 1
    assert len(await store.list_ledger_events(999, member_id=222)) == 1


@pytest.mark.asyncio
async def test_list_ledger_events_round_trips_every_event_kind(store: SQLiteStore) -> None:
    events: tuple[LedgerEvent, ...] = (
        JoinEvent(guild_id=111, member_id=222, occurred_at=NOW),
        MessageEvent(guild_id=111, member_id=222, occurred_at=NOW, channel_id=1, thread_id=2),
        ReactionEvent(guild_id=111, member_id=222, occurred_at=NOW, channel_id=1, emoji="🎉"),
        VoiceSessionEvent(
            guild_id=111, member_id=222, occurred_at=NOW, left_at=LATER, channel_id=1
        ),
        EventAttendanceEvent(
            guild_id=111,
            member_id=222,
            occurred_at=NOW,
            scheduled_event_id=9,
            action=AttendanceAction.ADD,
        ),
        RoleChangeEvent(
            guild_id=111,
            member_id=222,
            occurred_at=NOW,
            role_id=7,
            action=RoleChangeAction.GRANTED,
        ),
        ModerationReceiptEvent(
            guild_id=111, member_id=222, occurred_at=NOW, receipt_id="receipt-1"
        ),
    )
    for event in events:
        await store.record_ledger_event(event)

    stored = await store.list_ledger_events(111, member_id=222)
    assert set(stored) == set(events)


@pytest.mark.asyncio
async def test_list_ledger_events_orders_most_recent_first(store: SQLiteStore) -> None:
    earlier = ledger_event(occurred_at=NOW)
    later = ledger_event(occurred_at=LATER)
    await store.record_ledger_event(earlier)
    await store.record_ledger_event(later)

    stored = await store.list_ledger_events(111, member_id=222)
    assert stored[0].occurred_at == LATER
    assert stored[1].occurred_at == NOW


# -- Milestones -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_milestones_are_guild_scoped(store: SQLiteStore) -> None:
    await store.save_milestone(milestone(guild_id=111, member_id=222))
    await store.save_milestone(milestone(guild_id=999, member_id=222))
    assert len(await store.list_milestones(111, member_id=222)) == 1
    assert len(await store.list_milestones(999, member_id=222)) == 1


@pytest.mark.asyncio
async def test_save_milestone_is_idempotent_for_same_identity(store: SQLiteStore) -> None:
    await store.save_milestone(milestone(detail="reached 10 messages"))
    await store.save_milestone(milestone(detail="reached 10 messages, updated wording"))
    stored = await store.list_milestones(111, member_id=222)
    assert len(stored) == 1
    assert stored[0].detail == "reached 10 messages, updated wording"


# -- Channel exclusions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusion_entries_are_guild_scoped(store: SQLiteStore) -> None:
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=444))
    await store.save_exclusion_entry(exclusion_entry(guild_id=999, channel_id=444))
    assert len(await store.list_exclusion_entries(111)) == 1
    assert len(await store.list_exclusion_entries(999)) == 1


@pytest.mark.asyncio
async def test_save_exclusion_entry_upserts_by_channel(store: SQLiteStore) -> None:
    await store.save_exclusion_entry(exclusion_entry(reason="first reason"))
    await store.save_exclusion_entry(exclusion_entry(reason="updated reason"))
    stored = await store.list_exclusion_entries(111)
    assert len(stored) == 1
    assert stored[0].reason == "updated reason"


# -- Retention policy -----------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_policy_is_guild_scoped(store: SQLiteStore) -> None:
    await store.save_retention_policy(retention_policy(guild_id=111, max_age_days=30))
    await store.save_retention_policy(retention_policy(guild_id=999, max_age_days=90))
    first = await store.get_retention_policy(111)
    second = await store.get_retention_policy(999)
    assert first is not None and first.max_age_days == 30
    assert second is not None and second.max_age_days == 90


@pytest.mark.asyncio
async def test_get_retention_policy_returns_none_when_unset(store: SQLiteStore) -> None:
    assert await store.get_retention_policy(111) is None


@pytest.mark.asyncio
async def test_save_retention_policy_replaces_prior_value(store: SQLiteStore) -> None:
    await store.save_retention_policy(retention_policy(max_age_days=30))
    await store.save_retention_policy(retention_policy(max_age_days=60))
    stored = await store.get_retention_policy(111)
    assert stored is not None
    assert stored.max_age_days == 60


# -- Activity receipts ------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_activity_receipt_persists_without_error(store: SQLiteStore) -> None:
    await store.record_activity_receipt(
        guild_id=111,
        receipt_id="receipt-1",
        member_id=222,
        action="member_data_deleted",
        detail={"table_count": 2},
        created_at=NOW,
    )
    # No dedicated reader is part of this task's interface; this only proves the
    # write succeeds against the real schema (constraint/type errors would raise).


# -- Deletion completeness --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_member_ledger_data_removes_events_and_milestones(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.save_milestone(milestone(guild_id=111, member_id=222))
    await store.delete_member_ledger_data(111, 222)
    assert await store.list_ledger_events(111, member_id=222) == ()
    assert await store.list_milestones(111, member_id=222) == ()


@pytest.mark.asyncio
async def test_delete_member_ledger_data_seeds_and_clears_every_member_scoped_table(
    store: SQLiteStore,
) -> None:
    """The deletion-completeness property: seed every table `delete_member_ledger_data`
    is documented to touch for one member, then confirm every one is empty afterward
    (not just the first table checked).
    """
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=222, occurred_at=LATER, channel_id=1)
    )
    await store.save_milestone(
        milestone(guild_id=111, member_id=222, kind=MilestoneKind.MESSAGE_COUNT)
    )
    await store.save_milestone(
        milestone(
            guild_id=111,
            member_id=222,
            kind=MilestoneKind.JOIN_ANNIVERSARY,
            reached_at=LATER,
            detail="1 year anniversary",
        )
    )

    await store.delete_member_ledger_data(111, 222)

    assert await store.list_ledger_events(111, member_id=222) == ()
    assert await store.list_milestones(111, member_id=222) == ()


@pytest.mark.asyncio
async def test_delete_member_ledger_data_does_not_affect_other_members_or_guilds(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=333))
    await store.record_ledger_event(ledger_event(guild_id=999, member_id=222))
    await store.save_milestone(milestone(guild_id=111, member_id=222))
    await store.save_milestone(milestone(guild_id=111, member_id=333))
    await store.save_milestone(milestone(guild_id=999, member_id=222))

    await store.delete_member_ledger_data(111, 222)

    assert await store.list_ledger_events(111, member_id=222) == ()
    assert await store.list_milestones(111, member_id=222) == ()
    assert len(await store.list_ledger_events(111, member_id=333)) == 1
    assert len(await store.list_milestones(111, member_id=333)) == 1
    assert len(await store.list_ledger_events(999, member_id=222)) == 1
    assert len(await store.list_milestones(999, member_id=222)) == 1


@pytest.mark.asyncio
async def test_delete_member_ledger_data_does_not_touch_guild_level_privacy_config(
    store: SQLiteStore,
) -> None:
    """Channel exclusions and the retention policy are guild-level configuration, not
    member-scoped ledger data, so deleting one member's data must leave them intact.
    """
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.save_exclusion_entry(exclusion_entry(guild_id=111))
    await store.save_retention_policy(retention_policy(guild_id=111))

    await store.delete_member_ledger_data(111, 222)

    assert len(await store.list_exclusion_entries(111)) == 1
    assert (await store.get_retention_policy(111)) is not None


@pytest.mark.asyncio
async def test_delete_member_ledger_data_is_idempotent(store: SQLiteStore) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.delete_member_ledger_data(111, 222)
    # Deleting again (nothing left to delete) must not raise.
    await store.delete_member_ledger_data(111, 222)
    assert await store.list_ledger_events(111, member_id=222) == ()
