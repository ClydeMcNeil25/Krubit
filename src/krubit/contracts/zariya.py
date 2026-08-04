"""Outbound evidence contract for Zariya's companion integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from krubit.domain.models import JSONValue
from krubit.security.redaction import redact

ZARIYA_SIGNAL_SCHEMA = "krubit.zariya-signal.v1"


class SignalContractError(ValueError):
    """Raised when a Zariya signal violates the Phase 0 contract."""


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise SignalContractError(f"{key} is required")
    return value


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SignalContractError("occurred_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SignalContractError("occurred_at must include a timezone")
    return parsed.astimezone(UTC)


def _evidence(value: object) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise SignalContractError("evidence must be an object")
    redacted = redact(value)  # type: ignore[arg-type]
    if not isinstance(redacted, dict):
        raise SignalContractError("evidence must remain an object")
    return redacted


@dataclass(frozen=True, slots=True)
class ZariyaSignal:
    signal_id: str
    guild_id: int
    kind: str
    severity: str
    occurred_at: datetime
    source_event_id: str
    summary: str
    evidence: Mapping[str, JSONValue]
    action_request: None = None

    @classmethod
    def create_test(
        cls, *, guild_id: int, source_event_id: str, occurred_at: datetime
    ) -> ZariyaSignal:
        if guild_id <= 0:
            raise SignalContractError("guild_id must be positive")
        if not source_event_id.strip():
            raise SignalContractError("source_event_id is required")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise SignalContractError("occurred_at must include a timezone")
        return cls(
            signal_id=f"signal:{source_event_id}",
            guild_id=guild_id,
            kind="foundation_test",
            severity="info",
            occurred_at=occurred_at.astimezone(UTC),
            source_event_id=source_event_id,
            summary="Krubit Phase 0 signal path is healthy.",
            evidence={"phase": 0, "member_data": False},
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ZariyaSignal:
        if payload.get("schema_version") != ZARIYA_SIGNAL_SCHEMA:
            raise SignalContractError(f"schema_version must be {ZARIYA_SIGNAL_SCHEMA}")
        if payload.get("action_request") is not None:
            raise SignalContractError("action_request must be null in Phase 0")
        raw_guild_id = _required_text(payload, "guild_id")
        if not raw_guild_id.isdigit() or int(raw_guild_id) <= 0:
            raise SignalContractError("guild_id must be positive numeric text")
        kind = _required_text(payload, "kind")
        severity = _required_text(payload, "severity")
        if kind != "foundation_test" or severity != "info":
            raise SignalContractError("Phase 0 only accepts info foundation_test signals")
        return cls(
            signal_id=_required_text(payload, "signal_id"),
            guild_id=int(raw_guild_id),
            kind=kind,
            severity=severity,
            occurred_at=_timestamp(payload.get("occurred_at")),
            source_event_id=_required_text(payload, "source_event_id"),
            summary=_required_text(payload, "summary"),
            evidence=_evidence(payload.get("evidence")),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": ZARIYA_SIGNAL_SCHEMA,
            "signal_id": self.signal_id,
            "guild_id": str(self.guild_id),
            "kind": self.kind,
            "severity": self.severity,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "source_event_id": self.source_event_id,
            "summary": self.summary,
            "evidence": _evidence(dict(self.evidence)),
            "action_request": None,
        }

