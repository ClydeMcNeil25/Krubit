"""Row-decoding helpers for the activity ledger (Phase 4) storage tables.

Isolates the column-to-value-object mapping for `ledger_events`, `milestones`,
`channel_exclusions`, and `retention_policies` rows so `SQLiteStore` stays focused on
queries and transactions, matching the `storage/creator_rows.py` (Phase 2) and
`storage/watchdog_rows.py` (Phase 3) precedent. `activity_receipts` decoding stays in
`sqlite.py` alongside the sibling `SniffReceipt`/`ContentReceipt` storage-only view
types, since it has no corresponding domain value object.

## Schema shape: one polymorphic `ledger_events` table

The design doc leaves "one table per event kind vs. a single polymorphic table with a
`kind` discriminant" as an implementation choice. This module (and the `ledger_events`
table in `sqlite.py`) picks the polymorphic shape, mirroring the existing
`guild_events`/`GuildEvent`/`accept_event` convention (a `kind`/`event_type` column
plus a JSON payload column) rather than inventing nine near-identical tables. Every
`LedgerEvent` union member's kind-specific fields are round-tripped through
`_ledger_event_detail`/`ledger_event_from_row`'s `detail_json` column; the domain layer
still exposes distinct value objects per kind (`JoinEvent`, `MessageEvent`, ...), per
the design doc's requirement — only the storage shape is shared.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

import aiosqlite

from krubit.domain.activity_ledger import (
    AttendanceAction,
    EventAttendanceEvent,
    ExclusionEntry,
    JoinEvent,
    LedgerEvent,
    LedgerEventKind,
    MessageEvent,
    Milestone,
    MilestoneEvent,
    MilestoneKind,
    ModerationReceiptEvent,
    OnboardingEvent,
    ReactionEvent,
    RetentionPolicy,
    RoleChangeAction,
    RoleChangeEvent,
    VoiceSessionEvent,
)
from krubit.domain.models import JSONValue


def _detail_object(raw: str) -> dict[str, JSONValue]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("stored detail_json must decode to an object")
    return {str(key): item for key, item in cast(dict[str, object], payload).items()}  # type: ignore[misc]


def ledger_event_detail(event: LedgerEvent) -> dict[str, JSONValue]:
    """Return the kind-specific fields of `event` as a JSON-compatible mapping.

    `guild_id`, `member_id`, `occurred_at`, and `kind` are stored as their own
    `ledger_events` columns (see `sqlite.py`); this covers only what varies by kind.
    """
    if isinstance(event, JoinEvent):
        return {}
    if isinstance(event, OnboardingEvent):
        return {}
    if isinstance(event, MessageEvent):
        return {"channel_id": event.channel_id, "thread_id": event.thread_id}
    if isinstance(event, ReactionEvent):
        return {"channel_id": event.channel_id, "emoji": event.emoji}
    if isinstance(event, VoiceSessionEvent):
        return {"channel_id": event.channel_id, "left_at": event.left_at.isoformat()}
    if isinstance(event, EventAttendanceEvent):
        return {
            "scheduled_event_id": event.scheduled_event_id,
            "action": event.action.value,
        }
    if isinstance(event, RoleChangeEvent):
        return {"role_id": event.role_id, "action": event.action.value}
    if isinstance(event, MilestoneEvent):
        return {"milestone_kind": event.milestone_kind.value, "detail": event.detail}
    return {"receipt_id": event.receipt_id}


def ledger_event_from_row(row: aiosqlite.Row | None) -> LedgerEvent | None:
    if row is None:
        return None
    guild_id = int(row["guild_id"])
    member_id = int(row["member_id"])
    occurred_at = datetime.fromisoformat(str(row["occurred_at"]))
    kind = LedgerEventKind(str(row["kind"]))
    detail = _detail_object(str(row["detail_json"]))

    if kind is LedgerEventKind.JOIN:
        return JoinEvent(guild_id=guild_id, member_id=member_id, occurred_at=occurred_at)
    if kind is LedgerEventKind.ONBOARDING:
        return OnboardingEvent(guild_id=guild_id, member_id=member_id, occurred_at=occurred_at)
    if kind is LedgerEventKind.MESSAGE:
        thread_id = detail.get("thread_id")
        return MessageEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            channel_id=int(cast(int, detail["channel_id"])),
            thread_id=int(cast(int, thread_id)) if thread_id is not None else None,
        )
    if kind is LedgerEventKind.REACTION:
        return ReactionEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            channel_id=int(cast(int, detail["channel_id"])),
            emoji=str(detail["emoji"]),
        )
    if kind is LedgerEventKind.VOICE_SESSION:
        return VoiceSessionEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            left_at=datetime.fromisoformat(str(detail["left_at"])),
            channel_id=int(cast(int, detail["channel_id"])),
        )
    if kind is LedgerEventKind.EVENT_ATTENDANCE:
        return EventAttendanceEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            scheduled_event_id=int(cast(int, detail["scheduled_event_id"])),
            action=AttendanceAction(str(detail["action"])),
        )
    if kind is LedgerEventKind.ROLE_CHANGE:
        return RoleChangeEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            role_id=int(cast(int, detail["role_id"])),
            action=RoleChangeAction(str(detail["action"])),
        )
    if kind is LedgerEventKind.MILESTONE:
        return MilestoneEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            milestone_kind=MilestoneKind(str(detail["milestone_kind"])),
            detail=str(detail["detail"]),
        )
    if kind is LedgerEventKind.MODERATION_RECEIPT:
        return ModerationReceiptEvent(
            guild_id=guild_id,
            member_id=member_id,
            occurred_at=occurred_at,
            receipt_id=str(detail["receipt_id"]),
        )
    raise ValueError(f"unsupported stored ledger event kind: {kind!r}")


def milestone_from_row(row: aiosqlite.Row | None) -> Milestone | None:
    if row is None:
        return None
    return Milestone(
        guild_id=int(row["guild_id"]),
        member_id=int(row["member_id"]),
        kind=MilestoneKind(str(row["kind"])),
        reached_at=datetime.fromisoformat(str(row["reached_at"])),
        detail=str(row["detail"]),
    )


def exclusion_entry_from_row(row: aiosqlite.Row | None) -> ExclusionEntry | None:
    if row is None:
        return None
    return ExclusionEntry(
        guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]),
        excluded_by=int(row["excluded_by"]),
        reason=str(row["reason"]),
        excluded_at=datetime.fromisoformat(str(row["excluded_at"])),
    )


def retention_policy_from_row(row: aiosqlite.Row | None) -> RetentionPolicy | None:
    if row is None:
        return None
    return RetentionPolicy(
        guild_id=int(row["guild_id"]),
        max_age_days=int(row["max_age_days"]),
        updated_by=int(row["updated_by"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
