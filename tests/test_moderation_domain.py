from __future__ import annotations

import pytest

from krubit.domain.moderation import (
    IllegalTransitionError,
    ModerationStatus,
    transition,
)

ALL_STATUSES = list(ModerationStatus)

LEGAL_PAIRS = {
    (ModerationStatus.RECORDED, ModerationStatus.APPROVAL_REQUIRED),
    (ModerationStatus.RECORDED, ModerationStatus.DUPLICATE),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.APPROVED),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.REJECTED),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.EXPIRED),
    (ModerationStatus.APPROVED, ModerationStatus.EXECUTED),
    (ModerationStatus.APPROVED, ModerationStatus.EXECUTION_FAILED),
    (ModerationStatus.REJECTED, ModerationStatus.CLOSED),
    (ModerationStatus.EXECUTED, ModerationStatus.CLOSED),
    (ModerationStatus.EXECUTION_FAILED, ModerationStatus.APPROVAL_REQUIRED),
    (ModerationStatus.EXECUTION_FAILED, ModerationStatus.CLOSED),
    (ModerationStatus.EXPIRED, ModerationStatus.CLOSED),
}


def test_status_enum_has_exactly_nine_required_values():
    assert {status.value for status in ModerationStatus} == {
        "recorded",
        "approval_required",
        "approved",
        "rejected",
        "executed",
        "execution_failed",
        "duplicate",
        "expired",
        "closed",
    }


@pytest.mark.parametrize("current,target", sorted(LEGAL_PAIRS, key=lambda p: (p[0], p[1])))
def test_legal_transitions_succeed(current, target):
    assert transition(current, target) is target


@pytest.mark.parametrize(
    "current,target",
    [
        (current, target)
        for current in ALL_STATUSES
        for target in ALL_STATUSES
        if (current, target) not in LEGAL_PAIRS
    ],
)
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


from datetime import UTC, datetime

from krubit.domain.moderation import (
    ApprovalDecision,
    AppealStatus,
    ModerationCase,
)


def _case(**overrides):
    fields = dict(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        offense_number=1,
        recommended_action="24h timeout",
        executed_action=None,
        action_expiration=None,
        status=ModerationStatus.RECORDED,
        review_deadline=None,
        reviewer_id=None,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )
    fields.update(overrides)
    return ModerationCase(**fields)


def test_valid_case_constructs():
    case = _case()
    assert case.status is ModerationStatus.RECORDED


def test_offense_number_must_be_at_least_one():
    with pytest.raises(ValueError, match="offense_number"):
        _case(offense_number=0)


def test_reviewer_id_and_decision_must_be_paired():
    with pytest.raises(ValueError, match="reviewer"):
        _case(reviewer_id=999, reviewer_decision=None)
    with pytest.raises(ValueError, match="reviewer"):
        _case(reviewer_id=None, reviewer_decision=ApprovalDecision.APPROVED)


def test_reviewer_pair_together_is_valid():
    case = _case(
        status=ModerationStatus.APPROVED,
        reviewer_id=999,
        reviewer_decision=ApprovalDecision.APPROVED,
    )
    assert case.reviewer_id == 999


def test_close_timestamp_requires_closed_status():
    with pytest.raises(ValueError, match="close_timestamp"):
        _case(close_timestamp=datetime(2026, 1, 2, tzinfo=UTC))


def test_close_timestamp_allowed_when_closed():
    case = _case(
        status=ModerationStatus.CLOSED,
        close_timestamp=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert case.status is ModerationStatus.CLOSED


def test_executed_action_only_allowed_when_status_executed():
    with pytest.raises(ValueError, match="executed_action"):
        _case(status=ModerationStatus.EXECUTION_FAILED, executed_action="24h timeout")


def test_executed_action_allowed_when_executed():
    case = _case(status=ModerationStatus.EXECUTED, executed_action="24h timeout")
    assert case.executed_action == "24h timeout"


def test_naive_report_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        _case(report_timestamp=datetime(2026, 1, 1))
