"""Typed request/response contract for the Krubit moderation adapter.

Mirrors krubit.contracts.zariya's pattern (schema constant, from_dict/to_dict,
a ValueError subclass for malformed payloads). See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md. This module
defines shape only: no storage, no Discord execution, no dedup lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from krubit.domain.moderation import (
    AppealStatus,
    ApprovalDecision,
    IllegalTransitionError,
    ModerationStatus,
)

__all__ = [
    "IllegalTransitionError",
    "ModerationContractError",
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
