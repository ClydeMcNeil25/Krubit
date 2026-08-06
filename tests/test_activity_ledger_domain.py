"""Tests for the activity ledger domain value objects (frozen dataclasses and enums).

Covers construction, immutability, and `__post_init__` validation for every type in
`krubit.domain.activity_ledger`. No I/O, no Discord objects, no calculation logic
(see `test_activation_retention_calculation.py` for that) — pure value object tests
only.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.activity_ledger import (
    MEANINGFUL_EVENT_KINDS,
    ActivationResult,
    AttendanceAction,
    CohortResult,
    CohortWindow,
    EventAttendanceEvent,
    ExclusionEntry,
    JoinEvent,
    LedgerEventKind,
    MessageEvent,
    Milestone,
    MilestoneEvent,
    MilestoneKind,
    ModerationReceiptEvent,
    OnboardingEvent,
    ParticipationTrend,
    ReactionEvent,
    RetentionPolicy,
    RoleChangeAction,
    RoleChangeEvent,
    VoiceSessionEvent,
    cohort_window_days,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_NAIVE = datetime(2026, 8, 5, 12, 0)


def test_ledger_event_kind_enumerates_every_kind() -> None:
    assert [kind.value for kind in LedgerEventKind] == [
        "join",
        "onboarding",
        "message",
        "reaction",
        "voice_session",
        "event_attendance",
        "role_change",
        "milestone",
        "moderation_receipt",
    ]


def test_meaningful_event_kinds_is_exactly_message_reaction_voice_event() -> None:
    assert frozenset(
        {
            LedgerEventKind.MESSAGE,
            LedgerEventKind.REACTION,
            LedgerEventKind.VOICE_SESSION,
            LedgerEventKind.EVENT_ATTENDANCE,
        }
    ) == MEANINGFUL_EVENT_KINDS


def test_meaningful_event_kinds_excludes_join_onboarding_role_milestone_receipt() -> None:
    excluded = {
        LedgerEventKind.JOIN,
        LedgerEventKind.ONBOARDING,
        LedgerEventKind.ROLE_CHANGE,
        LedgerEventKind.MILESTONE,
        LedgerEventKind.MODERATION_RECEIPT,
    }
    assert MEANINGFUL_EVENT_KINDS.isdisjoint(excluded)


def test_cohort_window_days_maps_seven_and_thirty() -> None:
    assert cohort_window_days(CohortWindow.SEVEN_DAY) == 7
    assert cohort_window_days(CohortWindow.THIRTY_DAY) == 30


def test_cohort_window_days_rejects_non_cohort_window() -> None:
    with pytest.raises(ValueError):
        cohort_window_days("seven_day")  # type: ignore[arg-type]


def test_join_event_kind_property() -> None:
    event = JoinEvent(guild_id=1, member_id=2, occurred_at=_NOW)
    assert event.kind is LedgerEventKind.JOIN


def test_join_event_is_frozen() -> None:
    event = JoinEvent(guild_id=1, member_id=2, occurred_at=_NOW)
    with pytest.raises(FrozenInstanceError):
        event.member_id = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"guild_id": 0},
        {"guild_id": -1},
        {"member_id": 0},
        {"occurred_at": _NAIVE},
    ],
)
def test_join_event_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {"guild_id": 1, "member_id": 2, "occurred_at": _NOW}
    merged = {**fields, **overrides}
    with pytest.raises(ValueError):
        JoinEvent(**merged)  # type: ignore[arg-type]


def test_onboarding_event_kind_property() -> None:
    event = OnboardingEvent(guild_id=1, member_id=2, occurred_at=_NOW)
    assert event.kind is LedgerEventKind.ONBOARDING


def test_message_event_kind_and_defaults() -> None:
    event = MessageEvent(guild_id=1, member_id=2, occurred_at=_NOW, channel_id=3)
    assert event.kind is LedgerEventKind.MESSAGE
    assert event.thread_id is None


def test_message_event_rejects_non_positive_channel_id() -> None:
    with pytest.raises(ValueError):
        MessageEvent(guild_id=1, member_id=2, occurred_at=_NOW, channel_id=0)


def test_message_event_rejects_non_positive_thread_id() -> None:
    with pytest.raises(ValueError):
        MessageEvent(guild_id=1, member_id=2, occurred_at=_NOW, channel_id=3, thread_id=0)


def test_reaction_event_kind_and_validation() -> None:
    event = ReactionEvent(guild_id=1, member_id=2, occurred_at=_NOW, channel_id=3, emoji="🎉")
    assert event.kind is LedgerEventKind.REACTION


def test_reaction_event_rejects_blank_emoji() -> None:
    with pytest.raises(ValueError):
        ReactionEvent(guild_id=1, member_id=2, occurred_at=_NOW, channel_id=3, emoji="  ")


def test_voice_session_event_computes_duration() -> None:
    event = VoiceSessionEvent(
        guild_id=1,
        member_id=2,
        occurred_at=_NOW,
        left_at=_NOW + timedelta(minutes=30),
        channel_id=3,
    )
    assert event.kind is LedgerEventKind.VOICE_SESSION
    assert event.duration == timedelta(minutes=30)


def test_voice_session_event_rejects_left_before_occurred() -> None:
    with pytest.raises(ValueError):
        VoiceSessionEvent(
            guild_id=1,
            member_id=2,
            occurred_at=_NOW,
            left_at=_NOW - timedelta(minutes=1),
            channel_id=3,
        )


def test_voice_session_event_allows_zero_duration() -> None:
    event = VoiceSessionEvent(guild_id=1, member_id=2, occurred_at=_NOW, left_at=_NOW, channel_id=3)
    assert event.duration == timedelta(0)


def test_event_attendance_event_kind_and_validation() -> None:
    event = EventAttendanceEvent(
        guild_id=1,
        member_id=2,
        occurred_at=_NOW,
        scheduled_event_id=5,
        action=AttendanceAction.ADD,
    )
    assert event.kind is LedgerEventKind.EVENT_ATTENDANCE


def test_event_attendance_event_rejects_non_action() -> None:
    with pytest.raises(ValueError):
        EventAttendanceEvent(
            guild_id=1,
            member_id=2,
            occurred_at=_NOW,
            scheduled_event_id=5,
            action="add",  # type: ignore[arg-type]
        )


def test_role_change_event_kind_and_validation() -> None:
    event = RoleChangeEvent(
        guild_id=1, member_id=2, occurred_at=_NOW, role_id=7, action=RoleChangeAction.GRANTED
    )
    assert event.kind is LedgerEventKind.ROLE_CHANGE


def test_milestone_event_kind_and_validation() -> None:
    event = MilestoneEvent(
        guild_id=1,
        member_id=2,
        occurred_at=_NOW,
        milestone_kind=MilestoneKind.MESSAGE_COUNT,
        detail="reached 10 messages",
    )
    assert event.kind is LedgerEventKind.MILESTONE


def test_milestone_event_rejects_blank_detail() -> None:
    with pytest.raises(ValueError):
        MilestoneEvent(
            guild_id=1,
            member_id=2,
            occurred_at=_NOW,
            milestone_kind=MilestoneKind.MESSAGE_COUNT,
            detail="   ",
        )


def test_moderation_receipt_event_kind_and_validation() -> None:
    event = ModerationReceiptEvent(guild_id=1, member_id=2, occurred_at=_NOW, receipt_id="rcpt-1")
    assert event.kind is LedgerEventKind.MODERATION_RECEIPT


def test_moderation_receipt_event_rejects_blank_receipt_id() -> None:
    with pytest.raises(ValueError):
        ModerationReceiptEvent(guild_id=1, member_id=2, occurred_at=_NOW, receipt_id="")


def test_milestone_value_object_validation() -> None:
    milestone = Milestone(
        guild_id=1,
        member_id=2,
        kind=MilestoneKind.JOIN_ANNIVERSARY,
        reached_at=_NOW,
        detail="one year in the guild",
    )
    assert milestone.kind is MilestoneKind.JOIN_ANNIVERSARY
    with pytest.raises(ValueError):
        Milestone(guild_id=1, member_id=2, kind="join_anniversary", reached_at=_NOW, detail="x")  # type: ignore[arg-type]


def test_activation_result_activated_requires_time_and_kind() -> None:
    with pytest.raises(ValueError):
        ActivationResult(activated=True, time_to_activation=None, activating_kind=None)
    with pytest.raises(ValueError):
        ActivationResult(
            activated=True, time_to_activation=timedelta(hours=1), activating_kind=None
        )


def test_activation_result_activated_rejects_negative_time() -> None:
    with pytest.raises(ValueError):
        ActivationResult(
            activated=True,
            time_to_activation=timedelta(hours=-1),
            activating_kind=LedgerEventKind.MESSAGE,
        )


def test_activation_result_activated_requires_meaningful_kind() -> None:
    with pytest.raises(ValueError):
        ActivationResult(
            activated=True,
            time_to_activation=timedelta(hours=1),
            activating_kind=LedgerEventKind.JOIN,
        )


def test_activation_result_not_activated_requires_none_fields() -> None:
    with pytest.raises(ValueError):
        ActivationResult(
            activated=False,
            time_to_activation=timedelta(hours=1),
            activating_kind=None,
        )
    with pytest.raises(ValueError):
        ActivationResult(
            activated=False,
            time_to_activation=None,
            activating_kind=LedgerEventKind.MESSAGE,
        )


def test_activation_result_valid_construction() -> None:
    result = ActivationResult(
        activated=True,
        time_to_activation=timedelta(hours=2),
        activating_kind=LedgerEventKind.MESSAGE,
    )
    assert result.activated is True
    not_activated = ActivationResult(activated=False, time_to_activation=None, activating_kind=None)
    assert not_activated.activated is False


def test_cohort_result_validates_counts_and_rate() -> None:
    with pytest.raises(ValueError):
        CohortResult(
            window=CohortWindow.SEVEN_DAY, cohort_size=5, retained_count=6, retention_rate=1.2
        )
    with pytest.raises(ValueError):
        CohortResult(
            window=CohortWindow.SEVEN_DAY, cohort_size=5, retained_count=-1, retention_rate=0.0
        )
    with pytest.raises(ValueError):
        CohortResult(
            window=CohortWindow.SEVEN_DAY, cohort_size=5, retained_count=2, retention_rate=0.9
        )


def test_cohort_result_valid_construction() -> None:
    result = CohortResult(
        window=CohortWindow.SEVEN_DAY, cohort_size=10, retained_count=6, retention_rate=0.6
    )
    assert result.retention_rate == pytest.approx(0.6)


def test_cohort_result_zero_cohort_size_requires_zero_rate() -> None:
    result = CohortResult(
        window=CohortWindow.SEVEN_DAY, cohort_size=0, retained_count=0, retention_rate=0.0
    )
    assert result.retention_rate == 0.0
    with pytest.raises(ValueError):
        CohortResult(
            window=CohortWindow.SEVEN_DAY, cohort_size=0, retained_count=0, retention_rate=0.1
        )


def test_participation_trend_validates_non_negative_counts() -> None:
    with pytest.raises(ValueError):
        ParticipationTrend(
            window=CohortWindow.SEVEN_DAY,
            active_day_count=-1,
            returning=False,
            channel_diversity=0,
            event_diversity=0,
        )
    with pytest.raises(ValueError):
        ParticipationTrend(
            window=CohortWindow.SEVEN_DAY,
            active_day_count=0,
            returning=False,
            channel_diversity=-1,
            event_diversity=0,
        )


def test_exclusion_entry_validation() -> None:
    entry = ExclusionEntry(
        guild_id=1, channel_id=2, excluded_by=3, reason="private staff channel", excluded_at=_NOW
    )
    assert entry.channel_id == 2
    with pytest.raises(ValueError):
        ExclusionEntry(guild_id=1, channel_id=2, excluded_by=3, reason="", excluded_at=_NOW)


def test_retention_policy_validation() -> None:
    policy = RetentionPolicy(guild_id=1, max_age_days=90, updated_by=2, updated_at=_NOW)
    assert policy.max_age_days == 90
    with pytest.raises(ValueError):
        RetentionPolicy(guild_id=1, max_age_days=0, updated_by=2, updated_at=_NOW)
    with pytest.raises(ValueError):
        RetentionPolicy(guild_id=1, max_age_days=-5, updated_by=2, updated_at=_NOW)
