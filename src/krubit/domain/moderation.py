"""Moderation case lifecycle: pure value objects and a legal-transition table.

This module tracks *decisions made about* evidence that already exists as a
Phase 3 `krubit.domain.watchdog.Incident` — it does not create evidence and
does not execute Discord actions. See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md.
"""

from __future__ import annotations

from enum import StrEnum


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
