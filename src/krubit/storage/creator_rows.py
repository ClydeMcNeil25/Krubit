"""Row-decoding helpers for the creator registry tables.

Isolates the column-to-value-object mapping for `creator_profiles`, `creator_accounts`,
and `creator_routes` rows so `SQLiteStore` stays focused on queries and transactions.
Storage-only view types (for example audit receipts) that have no corresponding domain
value object stay decoded in `sqlite.py`, matching the existing `LiveSignalDelivery`
convention there.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from krubit.domain.creator_signals import (
    ContentKind,
    CreatorAccount,
    CreatorProfile,
    CreatorRoute,
    Platform,
)


def creator_profile_from_row(row: aiosqlite.Row | None) -> CreatorProfile | None:
    if row is None:
        return None
    return CreatorProfile(
        guild_id=int(row["guild_id"]),
        owner_member_id=int(row["owner_member_id"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def creator_account_from_row(row: aiosqlite.Row | None) -> CreatorAccount | None:
    if row is None:
        return None
    return CreatorAccount(
        guild_id=int(row["guild_id"]),
        account_id=str(row["account_id"]),
        owner_member_id=int(row["owner_member_id"]),
        platform=Platform(str(row["platform"])),
        handle=str(row["handle"]),
        canonical_url=str(row["canonical_url"]),
        external_id=str(row["external_id"]),
        paused=bool(row["paused"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def creator_route_from_row(row: aiosqlite.Row | None) -> CreatorRoute | None:
    if row is None:
        return None
    mention_role_id = row["mention_role_id"]
    return CreatorRoute(
        guild_id=int(row["guild_id"]),
        account_id=str(row["account_id"]),
        content_kind=ContentKind(str(row["content_kind"])),
        channel_id=int(row["channel_id"]),
        mention_role_id=int(mention_role_id) if mention_role_id is not None else None,
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
