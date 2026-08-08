"""Tests for `krubit.discord.activity_commands.ActivityCommandService` — Task 8's
`/fetch member|activity|newcomers|inactive|milestones|retention|community-pulse`
command surface.

Matches `tests/test_watchdog_commands.py`'s convention: every test calls the
framework-independent service directly (never a `discord.Interaction`), against a
real on-disk `SQLiteStore` (never mocked), so authority and self-view properties
are exercised end to end through real storage rather than a fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from krubit.discord.activity_commands import ActivityActorContext, ActivityCommandService
from krubit.discord.content_commands import CommandStatus
from krubit.domain.activity_ledger import (
    JoinEvent,
    MessageEvent,
    Milestone,
    MilestoneKind,
    ModerationReceiptEvent,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

GUILD_ID = 111
STAFF_ID = 999
REGULAR_ID = 222
TARGET_ID = 333

_DEFAULT_INACTIVITY_THRESHOLD = timedelta(days=14)


def staff_member() -> ActivityActorContext:
    return ActivityActorContext(guild_id=GUILD_ID, member_id=STAFF_ID, is_staff=True)


def regular_member() -> ActivityActorContext:
    return ActivityActorContext(guild_id=GUILD_ID, member_id=REGULAR_ID, is_staff=False)


def other_member() -> ActivityActorContext:
    return ActivityActorContext(guild_id=GUILD_ID, member_id=TARGET_ID, is_staff=False)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def commands(store: SQLiteStore) -> ActivityCommandService:
    return ActivityCommandService(store, now=lambda: NOW)


async def _seed_member(
    store: SQLiteStore,
    *,
    member_id: int,
    joined_days_ago: int,
    last_active_days_ago: int | None,
    channel_id: int = 900,
) -> None:
    await store.record_ledger_event(
        JoinEvent(
            guild_id=GUILD_ID,
            member_id=member_id,
            occurred_at=NOW - timedelta(days=joined_days_ago),
        )
    )
    if last_active_days_ago is not None:
        await store.record_ledger_event(
            MessageEvent(
                guild_id=GUILD_ID,
                member_id=member_id,
                occurred_at=NOW - timedelta(days=last_active_days_ago),
                channel_id=channel_id,
            )
        )


# ---------------------------------------------------------------------------
# member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_another_members_profile(
    commands: ActivityCommandService,
) -> None:
    result = await commands.member(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_staff_can_fetch_a_members_profile(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=40, last_active_days_ago=2)
    await store.save_milestone(
        Milestone(
            guild_id=GUILD_ID,
            member_id=TARGET_ID,
            kind=MilestoneKind.MESSAGE_COUNT,
            reached_at=NOW - timedelta(days=2),
            detail="message_count_1",
        )
    )
    await store.record_ledger_event(
        ModerationReceiptEvent(
            guild_id=GUILD_ID,
            member_id=TARGET_ID,
            occurred_at=NOW - timedelta(days=1),
            receipt_id="incident:raid:test-1",
        )
    )
    result = await commands.member(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert result.detail["milestone_count"] == 1
    assert result.detail["activated"] is True
    assert "incident:raid:test-1" in result.card.description


@pytest.mark.asyncio
async def test_member_profile_reports_no_activation_with_no_meaningful_events(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=5, last_active_days_ago=None)
    result = await commands.member(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["activated"] is False


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_view_their_own_activity_self_view(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=REGULAR_ID, joined_days_ago=20, last_active_days_ago=1)
    result = await commands.activity(
        actor=regular_member(),
        target=regular_member(),
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "other member" not in result.card.description


@pytest.mark.asyncio
async def test_self_view_omits_the_staff_views_activated_field(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=REGULAR_ID, joined_days_ago=20, last_active_days_ago=1)
    self_result = await commands.activity(
        actor=regular_member(),
        target=regular_member(),
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert self_result.card is not None
    self_field_names = {field.name for field in self_result.card.fields}
    assert "Activated" not in self_field_names

    staff_result = await commands.activity(
        actor=staff_member(),
        target=regular_member(),
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert staff_result.card is not None
    staff_field_names = {field.name for field in staff_result.card.fields}
    assert "Activated" in staff_field_names


@pytest.mark.asyncio
async def test_regular_member_cannot_view_another_members_activity(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=20, last_active_days_ago=1)
    result = await commands.activity(
        actor=regular_member(),
        target=other_member(),
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_activity_self_view_cannot_be_manipulated_via_target_argument(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    """A non-staff caller whose `target` argument names another member is
    denied, even though a Discord-layer UI would normally default an omitted
    `member` argument to the caller's own ID -- proving the re-validation
    happens in the service, not merely in a UI default."""
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=20, last_active_days_ago=1)
    manipulated_target = ActivityActorContext(
        guild_id=GUILD_ID, member_id=TARGET_ID, is_staff=True
    )
    result = await commands.activity(
        actor=regular_member(),
        target=manipulated_target,
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_staff_can_view_another_members_activity(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=20, last_active_days_ago=1)
    result = await commands.activity(
        actor=staff_member(),
        target=other_member(),
        inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD,
    )
    assert result.status is CommandStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_activity_returning_field_is_reachable_when_threshold_at_least_window(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    """Reproduces and closes Important #3: `/fetch activity` uses a fixed 30-day
    trend window, but the inactivity threshold is operator-settable
    (`KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS`). Before the fetch-window
    widening fix, any operator setting the threshold to 30 or more made the
    "Returning" field permanently render "No", silently, with no warning, no
    matter how real a member's resumed-activity gap actually was. This member has
    a genuine ~39-day gap that resumes inside the trailing 30-day window."""
    await store.record_ledger_event(
        JoinEvent(guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=80))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=40),
            channel_id=900,
        )
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=1),
            channel_id=900,
        )
    )
    result = await commands.activity(
        actor=staff_member(),
        target=other_member(),
        inactivity_threshold=timedelta(days=30),
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    returning_field = next(f for f in result.card.fields if f.name == "Returning")
    assert returning_field.value == "Yes"


# ---------------------------------------------------------------------------
# newcomers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newcomers_denied_for_non_staff(commands: ActivityCommandService) -> None:
    result = await commands.newcomers(actor=regular_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_newcomers_lists_recent_joins_for_staff(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=5, last_active_days_ago=1)
    result = await commands.newcomers(actor=staff_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["count"] == 1


# ---------------------------------------------------------------------------
# inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inactive_denied_for_non_staff(commands: ActivityCommandService) -> None:
    result = await commands.inactive(
        actor=regular_member(), inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_inactive_genuinely_uses_the_supplied_threshold(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    """A member last active 10 days ago is inactive under a 5-day threshold but
    not under a 20-day threshold -- proving `inactivity_threshold` is a real,
    load-bearing call argument, not ignored."""
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=60, last_active_days_ago=10)

    short_threshold_result = await commands.inactive(
        actor=staff_member(), inactivity_threshold=timedelta(days=5)
    )
    assert short_threshold_result.detail["count"] == 1

    long_threshold_result = await commands.inactive(
        actor=staff_member(), inactivity_threshold=timedelta(days=20)
    )
    assert long_threshold_result.detail["count"] == 0


# ---------------------------------------------------------------------------
# milestones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_milestones_self_accessible(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await store.save_milestone(
        Milestone(
            guild_id=GUILD_ID,
            member_id=REGULAR_ID,
            kind=MilestoneKind.MESSAGE_COUNT,
            reached_at=NOW - timedelta(days=1),
            detail="message_count_1",
        )
    )
    result = await commands.milestones(actor=regular_member(), target=regular_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["count"] == 1


@pytest.mark.asyncio
async def test_milestones_denied_for_non_staff_viewing_another_member(
    commands: ActivityCommandService,
) -> None:
    result = await commands.milestones(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_milestones_staff_can_view_another_members_milestones(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await store.save_milestone(
        Milestone(
            guild_id=GUILD_ID,
            member_id=TARGET_ID,
            kind=MilestoneKind.JOIN_ANNIVERSARY,
            reached_at=NOW - timedelta(days=1),
            detail="join_anniversary_year_1",
        )
    )
    result = await commands.milestones(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["count"] == 1


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_denied_for_non_staff(commands: ActivityCommandService) -> None:
    result = await commands.retention(actor=regular_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_retention_reports_both_windows_for_staff(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=10, last_active_days_ago=9)
    result = await commands.retention(actor=staff_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert "seven_day_retention_pct" in result.detail
    assert "thirty_day_retention_pct" in result.detail


# ---------------------------------------------------------------------------
# community-pulse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_community_pulse_denied_for_non_staff(commands: ActivityCommandService) -> None:
    result = await commands.community_pulse(actor=regular_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_community_pulse_succeeds_for_staff(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=5, last_active_days_ago=1)
    result = await commands.community_pulse(actor=staff_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["active_member_count"] == 1


# ---------------------------------------------------------------------------
# returning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_denies_non_staff(commands: ActivityCommandService) -> None:
    result = await commands.returning(
        actor=regular_member(), inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_returning_renders_real_entries(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    # Matches tests/test_activity_views.py's own returning_member_view fixture
    # shape: join, active period, a gap longer than the threshold, then active
    # again -- all within the fixed 30-day trend window.
    await store.record_ledger_event(
        JoinEvent(guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=60))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=20),
            channel_id=900,
        )
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=1),
            channel_id=900,
        )
    )
    result = await commands.returning(
        actor=staff_member(), inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert f"<@{TARGET_ID}>" in result.card.description


@pytest.mark.asyncio
async def test_returning_truncates_past_entry_cap(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    # Seed more than the _MAX_LIST_ENTRIES (40) cap of members who each show a
    # genuine returning gap-then-resume within the trend window.
    for member_id in range(1, 45):
        await store.record_ledger_event(
            JoinEvent(guild_id=GUILD_ID, member_id=member_id, occurred_at=NOW - timedelta(days=60))
        )
        await store.record_ledger_event(
            MessageEvent(
                guild_id=GUILD_ID, member_id=member_id, occurred_at=NOW - timedelta(days=20),
                channel_id=900,
            )
        )
        await store.record_ledger_event(
            MessageEvent(
                guild_id=GUILD_ID, member_id=member_id, occurred_at=NOW - timedelta(days=1),
                channel_id=900,
            )
        )
    result = await commands.returning(
        actor=staff_member(), inactivity_threshold=_DEFAULT_INACTIVITY_THRESHOLD
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "...and" in result.card.description
    assert result.detail["count"] == 44


# ---------------------------------------------------------------------------
# recognition-candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recognition_candidates_denies_non_staff(
    commands: ActivityCommandService,
) -> None:
    result = await commands.recognition_candidates(actor=regular_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_recognition_candidates_renders_reasons(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    # Matches tests/test_milestones.py's FIXTURE_EVENTS seeding: 100 messages in
    # a single day is well past the message-count notability threshold.
    for i in range(100):
        await store.record_ledger_event(
            MessageEvent(
                guild_id=GUILD_ID,
                member_id=TARGET_ID,
                occurred_at=NOW - timedelta(days=1, minutes=-i),
                channel_id=900,
            )
        )
    result = await commands.recognition_candidates(actor=staff_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert f"<@{TARGET_ID}>" in result.card.description


# ---------------------------------------------------------------------------
# member-delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_member_denies_non_staff(
    commands: ActivityCommandService,
) -> None:
    result = await commands.delete_member(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_delete_member_first_call_requires_confirmation(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=5))
    )
    result = await commands.delete_member(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.CONFIRMATION_REQUIRED
    # nothing deleted yet
    events = await store.list_ledger_events(GUILD_ID, member_id=TARGET_ID)
    assert events != ()


@pytest.mark.asyncio
async def test_delete_member_confirm_true_deletes_and_returns_minimal_receipt(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=5))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=GUILD_ID, member_id=TARGET_ID, occurred_at=NOW - timedelta(days=1),
            channel_id=900,
        )
    )
    result = await commands.delete_member(actor=staff_member(), target=other_member(), confirm=True)
    assert result.status is CommandStatus.SUCCEEDED
    events = await store.list_ledger_events(GUILD_ID, member_id=TARGET_ID)
    assert events == ()
    assert "receipt_id" in result.detail
    assert "table" not in str(result.detail).lower()
    assert "row" not in str(result.detail).lower()


@pytest.mark.asyncio
async def test_delete_member_confirm_true_is_idempotent(
    commands: ActivityCommandService,
) -> None:
    first = await commands.delete_member(actor=staff_member(), target=other_member(), confirm=True)
    second = await commands.delete_member(actor=staff_member(), target=other_member(), confirm=True)
    assert first.status is CommandStatus.SUCCEEDED
    assert second.status is CommandStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# member-export
# ---------------------------------------------------------------------------


async def _receipt_count(db_path: Path, *, guild_id: int, member_id: int) -> int:
    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM activity_receipts WHERE guild_id = ? AND member_id = ?",
            (guild_id, member_id),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


@pytest.mark.asyncio
async def test_export_member_denies_non_staff_non_self(
    commands: ActivityCommandService,
) -> None:
    result, payload = await commands.export_member(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED
    assert payload is None


@pytest.mark.asyncio
async def test_export_member_self_view_writes_no_receipt(
    tmp_path: Path, store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=REGULAR_ID, joined_days_ago=10, last_active_days_ago=1)
    result, payload = await commands.export_member(actor=regular_member(), target=regular_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert payload is not None
    decoded = json.loads(payload)
    assert decoded["member_id"] == REGULAR_ID
    db_path = tmp_path / "krubit.db"
    assert await _receipt_count(db_path, guild_id=GUILD_ID, member_id=REGULAR_ID) == 0


@pytest.mark.asyncio
async def test_export_member_staff_on_behalf_writes_audit_receipt(
    tmp_path: Path, store: SQLiteStore, commands: ActivityCommandService
) -> None:
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=10, last_active_days_ago=1)
    result, payload = await commands.export_member(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert payload is not None
    db_path = tmp_path / "krubit.db"
    assert await _receipt_count(db_path, guild_id=GUILD_ID, member_id=TARGET_ID) == 1
    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute(
            "SELECT action FROM activity_receipts WHERE guild_id = ? AND member_id = ?",
            (GUILD_ID, TARGET_ID),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "member_data_exported"


@pytest.mark.asyncio
async def test_export_member_json_never_includes_another_members_data(
    store: SQLiteStore, commands: ActivityCommandService
) -> None:
    other_target_id = 444
    await _seed_member(store, member_id=TARGET_ID, joined_days_ago=10, last_active_days_ago=1)
    await _seed_member(store, member_id=other_target_id, joined_days_ago=10, last_active_days_ago=1)
    result, payload = await commands.export_member(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert payload is not None
    decoded = json.loads(payload)
    serialized = json.dumps(decoded)
    assert str(other_target_id) not in serialized
