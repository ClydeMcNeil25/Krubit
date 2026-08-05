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
    ContentCursor,
    ContentDelivery,
    ContentEvent,
    ContentKind,
    ContentState,
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


def content_cursor_from_row(row: aiosqlite.Row | None) -> ContentCursor | None:
    if row is None:
        return None
    baselined_at = row["baselined_at"]
    cursor_value = row["cursor_value"]
    return ContentCursor(
        guild_id=int(row["guild_id"]),
        account_id=str(row["account_id"]),
        platform=Platform(str(row["platform"])),
        value=str(cursor_value) if cursor_value is not None else None,
        baselined_at=(
            datetime.fromisoformat(str(baselined_at)) if baselined_at is not None else None
        ),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def content_event_from_row(row: aiosqlite.Row | None) -> ContentEvent | None:
    if row is None:
        return None
    title = row["title"]
    published_at = row["published_at"]
    return ContentEvent(
        guild_id=int(row["guild_id"]),
        account_id=str(row["account_id"]),
        platform=Platform(str(row["platform"])),
        external_id=str(row["external_id"]),
        content_kind=ContentKind(str(row["content_kind"])),
        state=ContentState(str(row["state"])),
        canonical_url=str(row["canonical_url"]),
        title=str(title) if title is not None else None,
        published_at=(
            datetime.fromisoformat(str(published_at)) if published_at is not None else None
        ),
        first_observed_at=datetime.fromisoformat(str(row["first_observed_at"])),
        last_observed_at=datetime.fromisoformat(str(row["last_observed_at"])),
    )


def content_delivery_from_row(row: aiosqlite.Row | None) -> ContentDelivery | None:
    if row is None:
        return None
    channel_id = row["discord_channel_id"]
    message_id = row["discord_message_id"]
    return ContentDelivery(
        guild_id=int(row["guild_id"]),
        platform=Platform(str(row["platform"])),
        external_id=str(row["external_id"]),
        transition_seq=int(row["transition_seq"]),
        account_id=str(row["account_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        discord_channel_id=int(channel_id) if channel_id is not None else None,
        discord_message_id=int(message_id) if message_id is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
