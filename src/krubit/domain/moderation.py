"""Moderation case lifecycle: pure value objects and a legal-transition table.

This module tracks *decisions made about* evidence that already exists as a
Phase 3 `krubit.domain.watchdog.Incident` — it does not create evidence and
does not execute Discord actions. See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from krubit.domain.watchdog import _require_aware, _require_positive_id, _require_text


class ModerationStatus(StrEnum):
    RECORDED = "recorded"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    CLOSED = "closed"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class AppealStatus(StrEnum):
    NONE = "none"
    SUBMITTED = "submitted"
    UPHELD = "upheld"
    OVERTURNED = "overturned"


class IllegalTransitionError(ValueError):
    """Raised when a well-formed request asks for a status transition that
    isn't legal from the case's current status."""


_LEGAL_TRANSITIONS: dict[ModerationStatus, frozenset[ModerationStatus]] = {
    ModerationStatus.RECORDED: frozenset(
        {ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.DUPLICATE}
    ),
    ModerationStatus.APPROVAL_REQUIRED: frozenset(
        {ModerationStatus.APPROVED, ModerationStatus.REJECTED, ModerationStatus.EXPIRED}
    ),
    ModerationStatus.APPROVED: frozenset(
        {ModerationStatus.EXECUTED, ModerationStatus.EXECUTION_FAILED}
    ),
    ModerationStatus.REJECTED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.EXECUTED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.EXECUTION_FAILED: frozenset(
        {ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.CLOSED}
    ),
    ModerationStatus.DUPLICATE: frozenset(),
    ModerationStatus.EXPIRED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.CLOSED: frozenset(),
}


def transition(current: ModerationStatus, target: ModerationStatus) -> ModerationStatus:
    """Return `target` if `current -> target` is a legal lifecycle transition.

    Raises `IllegalTransitionError` otherwise. Pure function: no I/O, no clock
    reads, matching the sibling `krubit.domain.watchdog` module's convention.
    """
    if target not in _LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(f"{current} -> {target} is not a legal transition")
    return target


_MAX_ID_LENGTH = 64
_MAX_ACTION_LENGTH = 500


@dataclass(frozen=True, slots=True)
class ModerationCase:
    """Tracks the lifecycle decision made about an existing Phase 3 Incident.

    References `incident_id` as a plain string only — this slice does not
    join against a live `krubit.domain.watchdog.Incident` row.
    """

    case_id: str
    incident_id: str
    guild_id: int
    member_id: int
    report_timestamp: datetime
    offense_number: int
    recommended_action: str
    executed_action: str | None
    action_expiration: datetime | None
    status: ModerationStatus
    review_deadline: datetime | None
    reviewer_id: int | None
    reviewer_decision: ApprovalDecision | None
    appeal_status: AppealStatus
    close_timestamp: datetime | None

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id, limit=_MAX_ID_LENGTH)
        _require_text("incident_id", self.incident_id, limit=_MAX_ID_LENGTH)
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("report_timestamp", self.report_timestamp)
        if self.offense_number < 1:
            raise ValueError("offense_number must be at least 1")
        _require_text("recommended_action", self.recommended_action, limit=_MAX_ACTION_LENGTH)
        if self.executed_action is not None:
            _require_text("executed_action", self.executed_action, limit=_MAX_ACTION_LENGTH)
            if self.status is not ModerationStatus.EXECUTED:
                raise ValueError("executed_action may only be set when status is executed")
        if self.action_expiration is not None:
            _require_aware("action_expiration", self.action_expiration)
        if type(self.status) is not ModerationStatus:
            raise ValueError("status must be a ModerationStatus")
        if self.review_deadline is not None:
            _require_aware("review_deadline", self.review_deadline)
        if (self.reviewer_id is None) != (self.reviewer_decision is None):
            raise ValueError("reviewer_id and reviewer_decision must be set together")
        if self.reviewer_id is not None:
            _require_positive_id("reviewer_id", self.reviewer_id)
        if type(self.appeal_status) is not AppealStatus:
            raise ValueError("appeal_status must be an AppealStatus")
        if self.close_timestamp is not None:
            _require_aware("close_timestamp", self.close_timestamp)
            if self.status is not ModerationStatus.CLOSED:
                raise ValueError("close_timestamp may only be set when status is closed")
