"""Tests for the pure activation/retention/trend calculation functions.

Covers `time_to_activation`, `cohort_membership` (including a known-fixture
reproduction and explicit window-boundary tests), and `participation_trend`. All
inputs and outputs are the frozen value objects from `krubit.domain.activity_ledger`
— no I/O, no mocks, no clock reads.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from krubit.domain.activity_ledger import (
    AttendanceAction,
    CohortWindow,
    EventAttendanceEvent,
    JoinEvent,
    LedgerEvent,
    LedgerEventKind,
    MessageEvent,
    ReactionEvent,
    RoleChangeAction,
    RoleChangeEvent,
)
from krubit.services.activation_retention import (
    cohort_membership,
    participation_trend,
    time_to_activation,
)

AWARE_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_GUILD_ID = 1


def ledger_event(
    *, kind: LedgerEventKind, occurred_at: datetime, member_id: int = 1
) -> LedgerEvent:
    """Build a minimal `LedgerEvent` variant of the given `kind` for test fixtures."""
    if kind is LedgerEventKind.MESSAGE:
        return MessageEvent(
            guild_id=_GUILD_ID, member_id=member_id, occurred_at=occurred_at, channel_id=100
        )
    if kind is LedgerEventKind.REACTION:
        return ReactionEvent(
            guild_id=_GUILD_ID,
            member_id=member_id,
            occurred_at=occurred_at,
            channel_id=100,
            emoji="🎉",
        )
    if kind is LedgerEventKind.EVENT_ATTENDANCE:
        return EventAttendanceEvent(
            guild_id=_GUILD_ID,
            member_id=member_id,
            occurred_at=occurred_at,
            scheduled_event_id=500,
            action=AttendanceAction.ADD,
        )
    if kind is LedgerEventKind.ROLE_CHANGE:
        return RoleChangeEvent(
            guild_id=_GUILD_ID,
            member_id=member_id,
            occurred_at=occurred_at,
            role_id=200,
            action=RoleChangeAction.GRANTED,
        )
    raise ValueError(f"ledger_event() fixture helper does not support kind={kind!r}")


# ---------------------------------------------------------------------------
# time_to_activation
# ---------------------------------------------------------------------------


def test_time_to_activation_finds_first_meaningful_action_after_join() -> None:
    join_at = AWARE_NOW
    events = (
        ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=join_at + timedelta(hours=3)),
        ledger_event(kind=LedgerEventKind.REACTION, occurred_at=join_at + timedelta(hours=1)),
    )
    result = time_to_activation(join_at, events)
    assert result.activated is True
    assert result.time_to_activation == timedelta(hours=1)
    assert result.activating_kind is LedgerEventKind.REACTION


def test_time_to_activation_with_no_meaningful_events_reports_not_activated() -> None:
    result = time_to_activation(AWARE_NOW, ())
    assert result.activated is False
    assert result.time_to_activation is None


def test_time_to_activation_ignores_non_meaningful_kinds() -> None:
    join_at = AWARE_NOW
    events = (
        ledger_event(kind=LedgerEventKind.ROLE_CHANGE, occurred_at=join_at + timedelta(minutes=1)),
    )
    result = time_to_activation(join_at, events)
    assert result.activated is False


def test_time_to_activation_ignores_events_strictly_before_join() -> None:
    join_at = AWARE_NOW
    events = (
        ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=join_at - timedelta(minutes=1)),
    )
    result = time_to_activation(join_at, events)
    assert result.activated is False


def test_time_to_activation_includes_event_exactly_at_join_instant() -> None:
    join_at = AWARE_NOW
    events = (ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=join_at),)
    result = time_to_activation(join_at, events)
    assert result.activated is True
    assert result.time_to_activation == timedelta(0)


def test_time_to_activation_rejects_naive_join_at() -> None:
    with pytest.raises(ValueError):
        time_to_activation(datetime(2026, 8, 5, 12, 0), ())


# ---------------------------------------------------------------------------
# cohort_membership: known fixture
# ---------------------------------------------------------------------------

_JOIN_DATE = date(2026, 1, 1)


def _join_at(day_offset: int = 0, hour: int = 12) -> datetime:
    base = datetime(_JOIN_DATE.year, _JOIN_DATE.month, _JOIN_DATE.day, hour, tzinfo=UTC)
    return base + timedelta(days=day_offset)


KNOWN_JOIN_FIXTURE: tuple[JoinEvent, ...] = tuple(
    JoinEvent(guild_id=_GUILD_ID, member_id=member_id, occurred_at=_join_at())
    for member_id in range(1, 11)
)

KNOWN_EVENT_FIXTURE: tuple[LedgerEvent, ...] = (
    # Members 1-6: at least one meaningful event inside the 7-day window -> retained.
    ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=0), member_id=1),
    ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=3), member_id=2),
    ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=7), member_id=3),
    ledger_event(kind=LedgerEventKind.REACTION, occurred_at=_join_at(day_offset=1), member_id=4),
    ledger_event(
        kind=LedgerEventKind.EVENT_ATTENDANCE, occurred_at=_join_at(day_offset=5), member_id=5
    ),
    ledger_event(
        kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=7, hour=23), member_id=6
    ),
    # Member 7: event exists but one day past the window end -> not retained.
    ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=8), member_id=7),
    # Member 8: no events at all -> not retained.
    # Member 9: no events of their own -> not retained. This extra event belongs to
    # member 1 (already retained above) and proves it is never misattributed to 9.
    ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=1), member_id=1),
    # Member 10: an event inside the window but not a meaningful kind -> not retained.
    ledger_event(
        kind=LedgerEventKind.ROLE_CHANGE, occurred_at=_join_at(day_offset=1), member_id=10
    ),
)


def test_cohort_membership_reproduces_a_known_fixture() -> None:
    result = cohort_membership(
        joins=KNOWN_JOIN_FIXTURE, events=KNOWN_EVENT_FIXTURE, window=CohortWindow.SEVEN_DAY
    )
    assert result.retained_count == 6
    assert result.cohort_size == 10
    assert result.retention_rate == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# cohort_membership: explicit boundary tests
# ---------------------------------------------------------------------------


def test_cohort_membership_counts_event_on_join_day_before_join_time_of_day() -> None:
    join = JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=_join_at(hour=12))
    # Event on the same calendar date but an earlier time of day than the join instant.
    event = ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(hour=1), member_id=1)
    result = cohort_membership(joins=(join,), events=(event,), window=CohortWindow.SEVEN_DAY)
    assert result.retained_count == 1


def test_cohort_membership_counts_event_exactly_on_window_end_day() -> None:
    join = JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=_join_at())
    event = ledger_event(
        kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=7), member_id=1
    )
    result = cohort_membership(joins=(join,), events=(event,), window=CohortWindow.SEVEN_DAY)
    assert result.retained_count == 1


def test_cohort_membership_excludes_event_one_day_past_window_end() -> None:
    join = JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=_join_at())
    event = ledger_event(
        kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=8), member_id=1
    )
    result = cohort_membership(joins=(join,), events=(event,), window=CohortWindow.SEVEN_DAY)
    assert result.retained_count == 0


def test_cohort_membership_thirty_day_window_boundary() -> None:
    join = JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=_join_at())
    on_boundary = ledger_event(
        kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=30), member_id=1
    )
    result = cohort_membership(joins=(join,), events=(on_boundary,), window=CohortWindow.THIRTY_DAY)
    assert result.retained_count == 1

    past_boundary = ledger_event(
        kind=LedgerEventKind.MESSAGE, occurred_at=_join_at(day_offset=31), member_id=1
    )
    result = cohort_membership(
        joins=(join,), events=(past_boundary,), window=CohortWindow.THIRTY_DAY
    )
    assert result.retained_count == 0


def test_cohort_membership_empty_cohort_has_zero_rate() -> None:
    result = cohort_membership(joins=(), events=(), window=CohortWindow.SEVEN_DAY)
    assert result.cohort_size == 0
    assert result.retained_count == 0
    assert result.retention_rate == 0.0


def test_cohort_membership_rejects_non_tuple_joins() -> None:
    with pytest.raises(ValueError):
        cohort_membership(joins=[], events=(), window=CohortWindow.SEVEN_DAY)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# participation_trend
# ---------------------------------------------------------------------------

_TREND_D0 = date(2026, 2, 1)


def _trend_day(offset: int, hour: int = 9) -> datetime:
    base = datetime(_TREND_D0.year, _TREND_D0.month, _TREND_D0.day, hour, tzinfo=UTC)
    return base + timedelta(days=offset)


_TREND_EVENTS: tuple[LedgerEvent, ...] = (
    MessageEvent(guild_id=_GUILD_ID, member_id=5, occurred_at=_trend_day(0), channel_id=100),
    MessageEvent(guild_id=_GUILD_ID, member_id=5, occurred_at=_trend_day(2), channel_id=101),
    MessageEvent(guild_id=_GUILD_ID, member_id=5, occurred_at=_trend_day(14), channel_id=100),
    MessageEvent(guild_id=_GUILD_ID, member_id=5, occurred_at=_trend_day(15), channel_id=100),
    ReactionEvent(
        guild_id=_GUILD_ID, member_id=5, occurred_at=_trend_day(15), channel_id=102, emoji="🎉"
    ),
    EventAttendanceEvent(
        guild_id=_GUILD_ID,
        member_id=5,
        occurred_at=_trend_day(15),
        scheduled_event_id=55,
        action=AttendanceAction.ADD,
    ),
)


def test_participation_trend_computes_active_days_diversity_and_returning() -> None:
    trend = participation_trend(_TREND_EVENTS, CohortWindow.SEVEN_DAY, timedelta(days=5))
    # Window anchors to the latest active day (offset 15) and covers offsets 9-15.
    # Only offsets 14 and 15 fall in that range.
    assert trend.active_day_count == 2
    assert trend.channel_diversity == 2  # channels 100 and 102 within the window
    assert trend.event_diversity == 1  # scheduled_event_id 55
    # The gap between offset 2 and offset 14 (12 days) exceeds the 5-day threshold,
    # and offset 14 falls inside the window -> returning.
    assert trend.returning is True


def test_participation_trend_returning_false_when_no_gap_exceeds_threshold() -> None:
    trend = participation_trend(_TREND_EVENTS, CohortWindow.SEVEN_DAY, timedelta(days=20))
    assert trend.returning is False


def test_participation_trend_empty_events_is_all_zero() -> None:
    trend = participation_trend((), CohortWindow.SEVEN_DAY, timedelta(days=3))
    assert trend.active_day_count == 0
    assert trend.returning is False
    assert trend.channel_diversity == 0
    assert trend.event_diversity == 0


def test_participation_trend_ignores_non_meaningful_events() -> None:
    events = (
        RoleChangeEvent(
            guild_id=_GUILD_ID,
            member_id=5,
            occurred_at=_trend_day(0),
            role_id=1,
            action=RoleChangeAction.GRANTED,
        ),
    )
    trend = participation_trend(events, CohortWindow.SEVEN_DAY, timedelta(days=3))
    assert trend.active_day_count == 0


def test_participation_trend_rejects_non_positive_inactivity_threshold() -> None:
    with pytest.raises(ValueError):
        participation_trend((), CohortWindow.SEVEN_DAY, timedelta(0))
    with pytest.raises(ValueError):
        participation_trend((), CohortWindow.SEVEN_DAY, timedelta(days=-1))
