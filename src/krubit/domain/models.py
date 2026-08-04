"""Framework-independent values used by Krubit's foundation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


def _require_id(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_guild_id(guild_id: int) -> None:
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")


def _require_aware(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")


@dataclass(frozen=True, slots=True)
class GuildEvent:
    event_id: str
    guild_id: int
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _require_id("event_id", self.event_id)
        _require_guild_id(self.guild_id)
        _require_id("event_type", self.event_type)
        _require_aware(self.occurred_at)


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    receipt_id: str
    guild_id: int
    action: str
    status: str
    actor_id: int | None
    detail: Mapping[str, JSONValue]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id("receipt_id", self.receipt_id)
        _require_guild_id(self.guild_id)
        _require_id("action", self.action)
        _require_id("status", self.status)
        _require_aware(self.created_at)


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    guild_id: int
    enabled: bool
    event_count: int
    receipt_count: int
    database_healthy: bool

    def __post_init__(self) -> None:
        _require_guild_id(self.guild_id)
        if self.event_count < 0 or self.receipt_count < 0:
            raise ValueError("status counts must not be negative")


@dataclass(frozen=True, slots=True)
class CardField:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True, slots=True)
class Card:
    kind: str
    title: str
    description: str
    fields: tuple[CardField, ...] = ()
    color: int = 0x8B5CF6
