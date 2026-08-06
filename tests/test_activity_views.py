"""Tests for the newcomer, inactive, returning-member, and community-pulse views.

Exercises `krubit.services.activity_views` against a real on-disk SQLite database
(never mocked), matching the `test_activity_ledger_storage.py` convention. Every
view is guild-scoped; `inactive_view` must genuinely exclude members who left,
using Phase 1's existing `guild_events` "member_left" tracking (see
`KrubitBot.on_member_remove` in `discord/bot.py`) rather than a new mechanism —
that is the single most important behavior this module tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.discord.events import guild_event
from krubit.domain.activity_ledger import (
    CohortWindow,
    JoinEvent,
    MessageEvent,
    ReactionEvent,
)
from krubit.services.activity_views import (
    community_pulse,
    inactive_view,
    newcomer_view,
    returning_member_view,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_GUILD_ID = 111


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


async def seed_member(
    store: SQLiteStore,
    *,
    guild_id: int,
    member_id: int,
    left: bool,
    last_active_days_ago: int | None,
    joined_days_ago: int | None = None,
    channel_id: int = 900,
) -> None:
    """Seed one member's ledger history: a join event, and (unless
    `last_active_days_ago` is None) a message event that many days before `NOW`.
    If `left`, also records the exact Phase 1 `member_left` `guild_events` row
    `KrubitBot.on_member_remove` writes on a real guild departure.
    """
    offset = (
        joined_days_ago
        if joined_days_ago is not None
        else ((last_active_days_ago + 10) if last_active_days_ago is not None else 90)
    )
    joined_at = NOW - timedelta(days=offset)
    await store.record_ledger_event(
        JoinEvent(guild_id=guild_id, member_id=member_id, occurred_at=joined_at)
    )
    if last_active_days_ago is not None:
        await store.record_ledger_event(
            MessageEvent(
                guild_id=guild_id,
                member_id=member_id,
                occurred_at=NOW - timedelta(days=last_active_days_ago),
                channel_id=channel_id,
            )
        )
    if left:
        await store.accept_event(
            guild_event("member_left", guild_id, member_id, NOW, {"bot": False}, None)
        )


# ---------------------------------------------------------------------------
# newcomer_view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newcomer_view_includes_members_who_joined_within_the_window(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=2))
    )
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    assert [m.member_id for m in view] == [1]


@pytest.mark.asyncio
async def test_newcomer_view_excludes_members_who_joined_before_the_window(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=30))
    )
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    assert view == ()


@pytest.mark.asyncio
async def test_newcomer_view_reports_activation_status(store: SQLiteStore) -> None:
    join_at = NOW - timedelta(days=2)
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at))
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=join_at + timedelta(hours=1), channel_id=1
        )
    )
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=2, occurred_at=join_at))
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    by_member = {entry.member_id: entry for entry in view}
    assert by_member[1].activated is True
    assert by_member[1].time_to_activation == timedelta(hours=1)
    assert by_member[2].activated is False
    assert by_member[2].time_to_activation is None


@pytest.mark.asyncio
async def test_newcomer_view_reports_most_recent_meaningful_action(
    store: SQLiteStore,
) -> None:
    join_at = NOW - timedelta(days=2)
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at))
    first = join_at + timedelta(hours=1)
    last = join_at + timedelta(hours=5)
    await store.record_ledger_event(
        MessageEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=first, channel_id=1)
    )
    await store.record_ledger_event(
        MessageEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=last, channel_id=1)
    )
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    assert view[0].last_meaningful_action_at == last


@pytest.mark.asyncio
async def test_newcomer_view_is_guild_scoped(store: SQLiteStore) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=1))
    )
    await store.record_ledger_event(
        JoinEvent(guild_id=999, member_id=2, occurred_at=NOW - timedelta(days=1))
    )
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    assert [m.member_id for m in view] == [1]


@pytest.mark.asyncio
async def test_newcomer_view_does_not_leak_one_newcomers_events_into_anothers_entry(
    store: SQLiteStore,
) -> None:
    join_at = NOW - timedelta(days=2)
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at))
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=2, occurred_at=join_at))
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=join_at + timedelta(hours=1), channel_id=1
        )
    )
    view = await newcomer_view(store, _GUILD_ID, recent_window=timedelta(days=7), now=NOW)
    by_member = {entry.member_id: entry for entry in view}
    assert by_member[2].last_meaningful_action_at is None
    assert by_member[2].activated is False


# ---------------------------------------------------------------------------
# inactive_view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inactive_view_excludes_members_who_left(store: SQLiteStore) -> None:
    await seed_member(store, guild_id=111, member_id=1, left=True, last_active_days_ago=60)
    await seed_member(store, guild_id=111, member_id=2, left=False, last_active_days_ago=60)
    view = await inactive_view(store, 111, inactivity_threshold=timedelta(days=30), now=NOW)
    assert [m.member_id for m in view] == [2]


@pytest.mark.asyncio
async def test_inactive_view_excludes_members_active_within_the_threshold(
    store: SQLiteStore,
) -> None:
    await seed_member(store, guild_id=_GUILD_ID, member_id=1, left=False, last_active_days_ago=5)
    view = await inactive_view(store, _GUILD_ID, inactivity_threshold=timedelta(days=30), now=NOW)
    assert view == ()


@pytest.mark.asyncio
async def test_inactive_view_includes_members_with_no_meaningful_action_at_all(
    store: SQLiteStore,
) -> None:
    await seed_member(store, guild_id=_GUILD_ID, member_id=1, left=False, last_active_days_ago=None)
    view = await inactive_view(store, _GUILD_ID, inactivity_threshold=timedelta(days=30), now=NOW)
    assert [m.member_id for m in view] == [1]


@pytest.mark.asyncio
async def test_inactive_view_is_guild_scoped(store: SQLiteStore) -> None:
    await seed_member(store, guild_id=_GUILD_ID, member_id=1, left=False, last_active_days_ago=60)
    await seed_member(store, guild_id=999, member_id=2, left=False, last_active_days_ago=60)
    view = await inactive_view(store, _GUILD_ID, inactivity_threshold=timedelta(days=30), now=NOW)
    assert [m.member_id for m in view] == [1]


# ---------------------------------------------------------------------------
# returning_member_view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_member_view_includes_members_who_resumed_after_a_gap(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=60))
    )
    # Both the pre-gap and post-gap activity must fall within the fixed 30-day
    # trend window (`_RETURNING_TREND_WINDOW`) `returning_member_view` uses, since
    # `participation_trend`'s caller contract requires pre-filtering `events` to a
    # real trailing window ending at `now` before the function ever sees them.
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=20), channel_id=1
        )
    )
    # Long silent gap (19 days, > the 14-day threshold), then activity resumes.
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=1), channel_id=1
        )
    )
    view = await returning_member_view(
        store, _GUILD_ID, inactivity_threshold=timedelta(days=14), now=NOW
    )
    assert [m.member_id for m in view] == [1]


@pytest.mark.asyncio
async def test_returning_member_view_excludes_members_with_steady_activity(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=20))
    )
    for day in range(20):
        await store.record_ledger_event(
            MessageEvent(
                guild_id=_GUILD_ID,
                member_id=1,
                occurred_at=NOW - timedelta(days=day),
                channel_id=1,
            )
        )
    view = await returning_member_view(
        store, _GUILD_ID, inactivity_threshold=timedelta(days=14), now=NOW
    )
    assert view == ()


@pytest.mark.asyncio
async def test_returning_member_view_is_guild_scoped(store: SQLiteStore) -> None:
    for guild_id in (_GUILD_ID, 999):
        await store.record_ledger_event(
            JoinEvent(guild_id=guild_id, member_id=1, occurred_at=NOW - timedelta(days=60))
        )
        await store.record_ledger_event(
            MessageEvent(
                guild_id=guild_id, member_id=1, occurred_at=NOW - timedelta(days=20), channel_id=1
            )
        )
        await store.record_ledger_event(
            MessageEvent(
                guild_id=guild_id, member_id=1, occurred_at=NOW - timedelta(days=1), channel_id=1
            )
        )
    view = await returning_member_view(
        store, _GUILD_ID, inactivity_threshold=timedelta(days=14), now=NOW
    )
    assert len(view) == 1


# ---------------------------------------------------------------------------
# community_pulse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_community_pulse_counts_active_members_in_the_window(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=5))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=1), channel_id=1
        )
    )
    # Member 2 only has activity long outside the 7-day window.
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=2, occurred_at=NOW - timedelta(days=60))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=2, occurred_at=NOW - timedelta(days=60), channel_id=1
        )
    )
    pulse = await community_pulse(store, _GUILD_ID, window=CohortWindow.SEVEN_DAY, now=NOW)
    assert pulse.active_member_count == 1


@pytest.mark.asyncio
async def test_community_pulse_computes_cohort_retention(store: SQLiteStore) -> None:
    join_at = NOW - timedelta(days=5)
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at))
    await store.record_ledger_event(
        MessageEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at, channel_id=1)
    )
    await store.record_ledger_event(JoinEvent(guild_id=_GUILD_ID, member_id=2, occurred_at=join_at))
    pulse = await community_pulse(store, _GUILD_ID, window=CohortWindow.SEVEN_DAY, now=NOW)
    assert pulse.cohort.cohort_size == 2
    assert pulse.cohort.retained_count == 1


@pytest.mark.asyncio
async def test_community_pulse_counts_channel_and_event_contribution(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=5))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=1), channel_id=1
        )
    )
    await store.record_ledger_event(
        ReactionEvent(
            guild_id=_GUILD_ID,
            member_id=1,
            occurred_at=NOW - timedelta(days=1),
            channel_id=2,
            emoji="🎉",
        )
    )
    pulse = await community_pulse(store, _GUILD_ID, window=CohortWindow.SEVEN_DAY, now=NOW)
    assert pulse.channel_contribution_count == 2


@pytest.mark.asyncio
async def test_community_pulse_is_guild_scoped(store: SQLiteStore) -> None:
    await store.record_ledger_event(
        JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=5))
    )
    await store.record_ledger_event(
        MessageEvent(
            guild_id=_GUILD_ID, member_id=1, occurred_at=NOW - timedelta(days=1), channel_id=1
        )
    )
    await store.record_ledger_event(
        JoinEvent(guild_id=999, member_id=2, occurred_at=NOW - timedelta(days=5))
    )
    await store.record_ledger_event(
        MessageEvent(guild_id=999, member_id=2, occurred_at=NOW - timedelta(days=1), channel_id=1)
    )
    pulse = await community_pulse(store, _GUILD_ID, window=CohortWindow.SEVEN_DAY, now=NOW)
    assert pulse.active_member_count == 1
