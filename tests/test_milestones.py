"""Tests for milestone evaluation and recognition-candidate shortlisting.

Covers `evaluate_milestones` (named, explainable milestone rules — message-count
tiers and join anniversaries) and `recognition_candidates` (a factual shortlist:
never a numeric score, never generated recognition text — see the design doc's
Recognition-candidate view and the rollout doc's Non-Negotiable Boundaries). All
inputs/outputs are the frozen value objects from `krubit.domain.activity_ledger` —
no I/O, no mocks, no clock reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.activity_ledger import (
    AttendanceAction,
    CohortWindow,
    EventAttendanceEvent,
    JoinEvent,
    LedgerEvent,
    LedgerEventKind,
    MessageEvent,
    MilestoneKind,
    ReactionEvent,
)
from krubit.services.milestones import evaluate_milestones, recognition_candidates

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_GUILD_ID = 111
_MEMBER_ID = 1


def ledger_event(
    *,
    kind: LedgerEventKind,
    occurred_at: datetime | None = None,
    member_id: int = _MEMBER_ID,
    channel_id: int = 100,
) -> LedgerEvent:
    """Build a minimal `LedgerEvent` variant of the given `kind` for test fixtures."""
    ts = occurred_at if occurred_at is not None else NOW
    if kind is LedgerEventKind.MESSAGE:
        return MessageEvent(
            guild_id=_GUILD_ID, member_id=member_id, occurred_at=ts, channel_id=channel_id
        )
    if kind is LedgerEventKind.REACTION:
        return ReactionEvent(
            guild_id=_GUILD_ID,
            member_id=member_id,
            occurred_at=ts,
            channel_id=channel_id,
            emoji="🎉",
        )
    if kind is LedgerEventKind.EVENT_ATTENDANCE:
        return EventAttendanceEvent(
            guild_id=_GUILD_ID,
            member_id=member_id,
            occurred_at=ts,
            scheduled_event_id=500,
            action=AttendanceAction.ADD,
        )
    raise ValueError(f"ledger_event() fixture helper does not support kind={kind!r}")


def _messages(
    count: int, *, start: datetime, member_id: int = _MEMBER_ID
) -> tuple[LedgerEvent, ...]:
    return tuple(
        ledger_event(
            kind=LedgerEventKind.MESSAGE,
            occurred_at=start + timedelta(minutes=i),
            member_id=member_id,
        )
        for i in range(count)
    )


# ---------------------------------------------------------------------------
# evaluate_milestones
# ---------------------------------------------------------------------------


def test_message_count_milestone_fires_at_configured_threshold() -> None:
    events = tuple(ledger_event(kind=LedgerEventKind.MESSAGE) for _ in range(100))
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    assert any(m.kind is MilestoneKind.MESSAGE_COUNT for m in milestones)
    assert any("100" in m.detail for m in milestones)


def test_message_count_milestone_does_not_fire_below_threshold() -> None:
    events = tuple(ledger_event(kind=LedgerEventKind.MESSAGE) for _ in range(99))
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    assert not any("100" in m.detail for m in milestones if m.kind is MilestoneKind.MESSAGE_COUNT)


def test_message_count_milestones_fire_for_every_threshold_crossed() -> None:
    events = tuple(ledger_event(kind=LedgerEventKind.MESSAGE) for _ in range(1000))
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    count_milestones = [m for m in milestones if m.kind is MilestoneKind.MESSAGE_COUNT]
    details = {m.detail for m in count_milestones}
    assert any("1" in d for d in details)
    assert any("500" in d for d in details)
    assert any("1000" in d for d in details)
    # Each threshold fires exactly once.
    assert len(count_milestones) == len({m.detail for m in count_milestones})


def test_message_count_milestone_reached_at_is_the_crossing_message_timestamp() -> None:
    start = NOW - timedelta(days=1)
    events = _messages(100, start=start)
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    hundred = next(
        m for m in milestones if m.kind is MilestoneKind.MESSAGE_COUNT and "100" in m.detail
    )
    assert hundred.reached_at == events[99].occurred_at


def test_join_anniversary_milestone_fires_after_a_full_year() -> None:
    join_at = NOW - timedelta(days=400)
    events = (JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at),)
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    assert any(m.kind is MilestoneKind.JOIN_ANNIVERSARY for m in milestones)


def test_join_anniversary_milestone_does_not_fire_before_a_full_year() -> None:
    join_at = NOW - timedelta(days=300)
    events = (JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at),)
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    assert not any(m.kind is MilestoneKind.JOIN_ANNIVERSARY for m in milestones)


def test_join_anniversary_milestones_fire_for_each_year_elapsed() -> None:
    join_at = NOW - timedelta(days=800)  # a bit over 2 years
    events = (JoinEvent(guild_id=_GUILD_ID, member_id=1, occurred_at=join_at),)
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    anniversaries = [m for m in milestones if m.kind is MilestoneKind.JOIN_ANNIVERSARY]
    assert len(anniversaries) == 2
    details = {m.detail for m in anniversaries}
    assert any("1" in d for d in details)
    assert any("2" in d for d in details)


def test_evaluate_milestones_only_considers_the_requested_member() -> None:
    events = tuple(
        ledger_event(kind=LedgerEventKind.MESSAGE, member_id=2) for _ in range(100)
    ) + tuple(ledger_event(kind=LedgerEventKind.MESSAGE, member_id=1) for _ in range(5))
    milestones = evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=events, now=NOW)
    # Member 1 only has 5 messages, so the 100-message tier must not fire for them
    # even though member 2 (excluded) crossed it.
    assert not any("100" in m.detail for m in milestones if m.kind is MilestoneKind.MESSAGE_COUNT)


def test_evaluate_milestones_rejects_non_tuple_events() -> None:
    with pytest.raises(ValueError, match="tuple"):
        evaluate_milestones(member_id=1, guild_id=_GUILD_ID, events=[], now=NOW)  # type: ignore[arg-type]


def test_evaluate_milestones_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone"):
        evaluate_milestones(
            member_id=1, guild_id=_GUILD_ID, events=(), now=datetime(2026, 8, 6, 12, 0)
        )


# ---------------------------------------------------------------------------
# recognition_candidates
# ---------------------------------------------------------------------------


def _diverse_channel_events(member_id: int, *, start: datetime) -> tuple[LedgerEvent, ...]:
    return tuple(
        ledger_event(
            kind=LedgerEventKind.MESSAGE,
            occurred_at=start + timedelta(hours=i),
            member_id=member_id,
            channel_id=100 + i,
        )
        for i in range(4)
    )


FIXTURE_EVENTS: tuple[LedgerEvent, ...] = (
    tuple(
        ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=NOW - timedelta(days=1))
        for _ in range(100)
    )
    + _diverse_channel_events(2, start=NOW - timedelta(days=2))
)


def test_recognition_candidates_are_facts_not_a_score() -> None:
    candidates = recognition_candidates(
        _GUILD_ID, events=FIXTURE_EVENTS, window=CohortWindow.THIRTY_DAY, now=NOW
    )
    assert candidates
    assert all(c.reasons for c in candidates)  # every candidate names its reasons


def test_recognition_candidate_reasons_cite_concrete_facts() -> None:
    candidates = recognition_candidates(
        _GUILD_ID, events=FIXTURE_EVENTS, window=CohortWindow.THIRTY_DAY, now=NOW
    )
    member_one = next(c for c in candidates if c.member_id == 1)
    assert any("message_count" in reason or "100" in reason for reason in member_one.reasons)


def test_recognition_candidates_excludes_members_with_no_notable_facts() -> None:
    # Reactions in a single channel: no message-count/join-anniversary milestone,
    # channel/event diversity below the notability threshold, no "returning" gap.
    quiet_events = tuple(
        ledger_event(
            kind=LedgerEventKind.REACTION,
            occurred_at=NOW - timedelta(days=1),
            member_id=3,
        )
        for _ in range(2)
    )
    candidates = recognition_candidates(
        _GUILD_ID, events=quiet_events, window=CohortWindow.THIRTY_DAY, now=NOW
    )
    assert not any(c.member_id == 3 for c in candidates)


def test_recognition_candidates_only_include_events_from_the_requested_guild() -> None:
    other_guild_events = tuple(
        MessageEvent(guild_id=999, member_id=9, occurred_at=NOW - timedelta(days=1), channel_id=1)
        for _ in range(100)
    )
    candidates = recognition_candidates(
        _GUILD_ID, events=other_guild_events, window=CohortWindow.THIRTY_DAY, now=NOW
    )
    assert candidates == ()


def test_recognition_candidates_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone"):
        recognition_candidates(
            _GUILD_ID, events=(), window=CohortWindow.THIRTY_DAY, now=datetime(2026, 8, 6)
        )


def test_recognition_candidates_returning_is_reachable_with_seven_day_window() -> None:
    """Reproduces and closes Important #3's dead-branch finding: with
    `_INACTIVITY_THRESHOLD` fixed at 14 days, `recognition_candidates(...,
    window=CohortWindow.SEVEN_DAY, ...)` could never surface a `returning=true`
    reason before the fetch-window-widening fix -- a bare 7-day-wide pre-filter
    cannot represent a gap longer than 6 days, so no 14-day gap could ever be seen,
    no matter how real. This member has a genuine ~18-day gap that resumes inside
    the trailing 7-day window, and must surface a "returning" reason."""
    member_id = 7
    events = (
        ledger_event(
            kind=LedgerEventKind.REACTION,
            occurred_at=NOW - timedelta(days=19),
            member_id=member_id,
        ),
        ledger_event(
            kind=LedgerEventKind.REACTION,
            occurred_at=NOW - timedelta(days=1),
            member_id=member_id,
        ),
    )
    candidates = recognition_candidates(
        _GUILD_ID, events=events, window=CohortWindow.SEVEN_DAY, now=NOW
    )
    candidate = next(c for c in candidates if c.member_id == member_id)
    assert any("returning" in reason for reason in candidate.reasons)
