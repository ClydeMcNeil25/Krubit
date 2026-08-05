"""Row-decoding helpers for the watchdog (Phase 3) storage tables.

Isolates the column-to-value-object mapping for `entry_sniff_assessments`,
`watch_windows`, `incidents`, and `guild_allow_block_lists` rows so `SQLiteStore`
stays focused on queries and transactions, matching the `storage/creator_rows.py`
precedent. Storage-only view types with no corresponding domain value object (for
example `SniffReceipt`) stay decoded in `sqlite.py`, matching the existing
`LiveSignalDelivery`/`CreatorRegistryReceipt` convention there.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

import aiosqlite

from krubit.domain.watchdog import (
    AllowBlockEntry,
    EntrySniffAssessment,
    Incident,
    IncidentKind,
    RiskBand,
    RiskSignal,
    WatchWindow,
    WatchWindowCloseReason,
)


def _signals_from_json(raw: str) -> tuple[RiskSignal, ...]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("stored signals_json must decode to a list")
    signals: list[RiskSignal] = []
    for raw_entry in cast(list[object], payload):
        if not isinstance(raw_entry, dict):
            raise ValueError("stored signal entry must be an object")
        entry = cast(dict[str, object], raw_entry)
        signals.append(
            RiskSignal(
                name=str(entry["name"]),
                weight=int(cast(int, entry["weight"])),
                detail=str(entry["detail"]),
                confidence=float(cast(float, entry["confidence"])),
            )
        )
    return tuple(signals)


def entry_sniff_assessment_from_row(row: aiosqlite.Row | None) -> EntrySniffAssessment | None:
    if row is None:
        return None
    return EntrySniffAssessment(
        guild_id=int(row["guild_id"]),
        member_id=int(row["member_id"]),
        joined_at=datetime.fromisoformat(str(row["joined_at"])),
        band=RiskBand(str(row["band"])),
        signals=_signals_from_json(str(row["signals_json"])),
        explanation=str(row["explanation"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def watch_window_from_row(row: aiosqlite.Row | None) -> WatchWindow | None:
    if row is None:
        return None
    closed_at = row["closed_at"]
    close_reason = row["close_reason"]
    return WatchWindow(
        guild_id=int(row["guild_id"]),
        member_id=int(row["member_id"]),
        opened_at=datetime.fromisoformat(str(row["opened_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        band=RiskBand(str(row["band"])),
        closed_at=datetime.fromisoformat(str(closed_at)) if closed_at is not None else None,
        close_reason=(
            WatchWindowCloseReason(str(close_reason)) if close_reason is not None else None
        ),
    )


def incident_from_row(row: aiosqlite.Row | None) -> Incident | None:
    if row is None:
        return None
    acknowledged_by = row["acknowledged_by"]
    return Incident(
        guild_id=int(row["guild_id"]),
        incident_id=str(row["incident_id"]),
        kind=IncidentKind(str(row["kind"])),
        band=RiskBand(str(row["band"])),
        opened_at=datetime.fromisoformat(str(row["opened_at"])),
        evidence_packet_id=str(row["evidence_packet_id"]),
        recommended_action=str(row["recommended_action"]),
        acknowledged_by=int(acknowledged_by) if acknowledged_by is not None else None,
    )


def allow_block_entry_from_row(row: aiosqlite.Row | None) -> AllowBlockEntry | None:
    if row is None:
        return None
    return AllowBlockEntry(
        guild_id=int(row["guild_id"]),
        discord_user_id=int(row["discord_user_id"]),
        list_kind=str(row["list_kind"]),
        reason=str(row["reason"]),
        set_by=int(row["set_by"]),
        set_at=datetime.fromisoformat(str(row["set_at"])),
    )
