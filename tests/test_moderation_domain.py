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
