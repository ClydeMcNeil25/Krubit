"""Typed request/response contract for the Krubit moderation adapter.

Mirrors krubit.contracts.zariya's pattern (schema constant, from_dict/to_dict,
a ValueError subclass for malformed payloads). See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md. This module
defines shape only: no storage, no Discord execution, no dedup lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from krubit.domain.models import JSONValue
from krubit.domain.moderation import (
    AppealStatus,
    ApprovalDecision,
    IllegalTransitionError,
    ModerationStatus,
)

__all__ = [
    "IllegalTransitionError",
    "ModerationContractError",
    "RecordIncidentRequest",
    "RecordIncidentResponse",
    "SubmitActionRecommendationRequest",
    "SubmitActionRecommendationResponse",
    "RequestHumanApprovalRequest",
    "RequestHumanApprovalResponse",
]


class ModerationContractError(ValueError):
    """Raised when a moderation adapter payload is malformed or incomplete."""


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ModerationContractError(f"{key} is required")
    return value


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModerationContractError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModerationContractError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


_MAX_ID_LENGTH = 64
_MAX_ACTION_LENGTH = 500


def _status(payload: Mapping[str, object]) -> ModerationStatus:
    raw = _required_text(payload, "status")
    try:
        return ModerationStatus(raw)
    except ValueError as exc:
        raise ModerationContractError(f"unknown status: {raw!r}") from exc


def _duplicate(payload: Mapping[str, object]) -> bool:
    value = payload.get("duplicate")
    if not isinstance(value, bool):
        raise ModerationContractError("duplicate must be a boolean")
    return value


def _receipt_state(payload: Mapping[str, object]) -> str | None:
    value = payload.get("receipt_state")
    if value is None:
        return None
    return str(value)


@dataclass(frozen=True, slots=True)
class RecordIncidentRequest:
    incident_id: str
    guild_id: int
    member_id: int
    report_timestamp: datetime
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RecordIncidentRequest:
        raw_guild_id = _required_text(payload, "guild_id")
        raw_member_id = _required_text(payload, "member_id")
        if not raw_guild_id.isdigit() or int(raw_guild_id) <= 0:
            raise ModerationContractError("guild_id must be positive numeric text")
        if not raw_member_id.isdigit() or int(raw_member_id) <= 0:
            raise ModerationContractError("member_id must be positive numeric text")
        return cls(
            incident_id=_required_text(payload, "incident_id"),
            guild_id=int(raw_guild_id),
            member_id=int(raw_member_id),
            report_timestamp=_timestamp(payload.get("report_timestamp")),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "incident_id": self.incident_id,
            "guild_id": str(self.guild_id),
            "member_id": str(self.member_id),
            "report_timestamp": _iso(self.report_timestamp),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RecordIncidentResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RecordIncidentResponse:
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class SubmitActionRecommendationRequest:
    case_id: str
    recommended_action: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SubmitActionRecommendationRequest:
        return cls(
            case_id=_required_text(payload, "case_id"),
            recommended_action=_required_text(payload, "recommended_action"),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "recommended_action": self.recommended_action,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class SubmitActionRecommendationResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SubmitActionRecommendationResponse:
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class RequestHumanApprovalRequest:
    case_id: str
    review_deadline: datetime
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RequestHumanApprovalRequest:
        return cls(
            case_id=_required_text(payload, "case_id"),
            review_deadline=_timestamp(payload.get("review_deadline")),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "review_deadline": _iso(self.review_deadline),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RequestHumanApprovalResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RequestHumanApprovalResponse:
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }
