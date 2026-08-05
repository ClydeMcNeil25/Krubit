"""Synchronize creator scheduled/live streams with Discord-native Scheduled Events.

This is a distinct concern from `ContentRuntime` (Task 6): it never posts an
announcement message, it mirrors one content item's lifecycle onto a Discord
Scheduled Event object that Krubit itself created and owns. The one safety property
every path here is built around: **Krubit must never mutate a Discord Scheduled Event
it did not create.** `ScheduledEventSynchronizer.apply` always resolves the target
event through the stored `ScheduledEventMapping` keyed by the exact
`(guild_id, platform, external_id)` identity — never by the event's mutable `name` —
and refuses outright the moment a stored mapping is not marked `owned_by_krubit`.

State reconciliation only ever attempts a transition Discord's own Scheduled Event
state machine allows: `scheduled -> active -> completed` or `scheduled -> cancelled`.
Every other requested transition (for example `active -> cancelled`, or a first
observation that starts at `active`/`completed` with no prior `scheduled` mapping) is
skipped without ever calling the Discord API for it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256

import discord

from krubit.domain.creator_signals import ContentState, Platform
from krubit.storage.sqlite import ScheduledEventMapping, SQLiteStore

_MAX_NAME_LENGTH = 100
_MAX_DESCRIPTION_LENGTH = 1000
_MAX_LOCATION_LENGTH = 100
_DEFAULT_DURATION = timedelta(hours=4)

# Only platforms with a genuine live-stream surface can carry a Scheduled Event.
_SUPPORTED_PLATFORMS = frozenset({Platform.TWITCH, Platform.YOUTUBE})

_RELEVANT_STATES = frozenset(
    {
        ContentState.SCHEDULED,
        ContentState.DELAYED,
        ContentState.LIVE,
        ContentState.ENDED,
        ContentState.CANCELLED,
    }
)

_DISCORD_STATUS_FOR_STATE: dict[ContentState, str] = {
    ContentState.SCHEDULED: "scheduled",
    ContentState.DELAYED: "scheduled",
    ContentState.LIVE: "active",
    ContentState.ENDED: "completed",
    ContentState.CANCELLED: "cancelled",
}

# Discord's own Scheduled Event state machine: scheduled -> active -> completed, or
# scheduled -> cancelled. Every status is also trivially "valid" against itself so a
# repeated observation of the same lifecycle phase can still apply a safe field update.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "scheduled": frozenset({"scheduled", "active", "cancelled"}),
    "active": frozenset({"active", "completed"}),
    "completed": frozenset({"completed"}),
    "cancelled": frozenset({"cancelled"}),
}

_EVENT_STATUS_FOR_DISCORD_STATUS: dict[str, discord.EventStatus] = {
    "scheduled": discord.EventStatus.scheduled,
    "active": discord.EventStatus.active,
    "completed": discord.EventStatus.completed,
    "cancelled": discord.EventStatus.canceled,
}

# Statuses for which Discord's API no longer accepts detail-field edits alongside (or
# instead of) a status transition; only the status transition itself is ever sent.
_TERMINAL_DISCORD_STATUSES = frozenset({"completed", "cancelled"})


class ScheduledEventOutcome(StrEnum):
    """Why `ScheduledEventSynchronizer.apply` skipped without mutating anything."""

    SKIPPED_NOT_ENABLED = "skipped_not_enabled"
    SKIPPED_NOT_SUPPORTED = "skipped_not_supported"
    SKIPPED_NOT_REGISTERED = "skipped_not_registered"
    SKIPPED_MISSING_PERMISSIONS = "skipped_missing_permissions"
    SKIPPED_NOT_OWNED = "skipped_not_owned"
    SKIPPED_INVALID_TRANSITION = "skipped_invalid_transition"
    SKIPPED_DISCORD_ERROR = "skipped_discord_error"


@dataclass(frozen=True, slots=True)
class ScheduledStreamEvent:
    """One creator's scheduled/live stream, normalized for Scheduled Event sync.

    Deliberately separate from `ContentEvent` (Task 6's content ledger): this carries
    only the fields a Discord Scheduled Event needs — bounded title/description, an
    external URL used as both location and event link, explicit start/end times, and
    an optional pre-fetched cover image. `state` drives the lifecycle transition; only
    `SCHEDULED`, `DELAYED`, `LIVE`, `ENDED`, and `CANCELLED` are meaningful here.
    """

    guild_id: int
    account_id: str
    platform: Platform
    external_id: str
    state: ContentState
    title: str
    description: str
    canonical_url: str
    scheduled_start_at: datetime
    scheduled_end_at: datetime | None = None
    cover_image_bytes: bytes | None = None

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if not self.account_id.strip():
            raise ValueError("account_id must not be blank")
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        if not self.external_id.strip():
            raise ValueError("external_id must not be blank")
        if type(self.state) is not ContentState:
            raise ValueError("state must be a ContentState")
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if self.scheduled_start_at.tzinfo is None:
            raise ValueError("scheduled_start_at must include a timezone")
        if self.scheduled_end_at is not None and self.scheduled_end_at.tzinfo is None:
            raise ValueError("scheduled_end_at must include a timezone")


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _content_hash(event: ScheduledStreamEvent, desired_status: str) -> str:
    payload = "\n".join(
        (
            desired_status,
            _bounded(event.title, _MAX_NAME_LENGTH),
            _bounded(event.description, _MAX_DESCRIPTION_LENGTH),
            _bounded(event.canonical_url, _MAX_LOCATION_LENGTH),
            event.scheduled_start_at.isoformat(),
            event.scheduled_end_at.isoformat() if event.scheduled_end_at is not None else "",
        )
    )
    return sha256(payload.encode()).hexdigest()


def _has_scheduled_event_permissions(guild: discord.Guild) -> bool:
    granted = getattr(guild.me, "guild_permissions", None)
    return bool(getattr(granted, "create_events", False)) and bool(
        getattr(granted, "manage_events", False)
    )


def _reason(event: ScheduledStreamEvent) -> str:
    return f"Krubit scheduled event sync {event.platform.value}:{event.external_id[:64]}"


class ScheduledEventSynchronizer:
    """Mirror one guild's registered creators' scheduled streams onto Discord.

    Bound to a single `discord.Guild` for its lifetime (unlike `ContentRuntime`, which
    takes the guild per call) since every Scheduled Event mutation is inherently
    guild-scoped and callers already hold the guild when scheduling this work.
    """

    def __init__(
        self,
        store: SQLiteStore,
        guild: discord.Guild,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._guild = guild
        self._now = now or (lambda: datetime.now(UTC))

    async def apply(
        self, event: ScheduledStreamEvent
    ) -> ScheduledEventMapping | ScheduledEventOutcome:
        """Apply one stream's current lifecycle state, idempotently and safely.

        Every lookup is by the exact stored `(guild_id, platform, external_id)`
        mapping key and, for an existing mapping, the exact stored
        `discord_event_id` — never by the Discord event's mutable `name`. A mapping
        not marked `owned_by_krubit` is refused outright, and a status transition
        outside Discord's `scheduled -> active -> completed` / `scheduled ->
        cancelled` state machine is skipped without any Discord API call.
        """
        if event.guild_id != self._guild.id:
            return ScheduledEventOutcome.SKIPPED_NOT_ENABLED
        if not await self._store.guild_is_enabled(self._guild.id):
            return ScheduledEventOutcome.SKIPPED_NOT_ENABLED
        if event.platform not in _SUPPORTED_PLATFORMS or event.state not in _RELEVANT_STATES:
            return ScheduledEventOutcome.SKIPPED_NOT_SUPPORTED
        if await self._store.get_creator_account(self._guild.id, event.account_id) is None:
            return ScheduledEventOutcome.SKIPPED_NOT_REGISTERED
        if not _has_scheduled_event_permissions(self._guild):
            return ScheduledEventOutcome.SKIPPED_MISSING_PERMISSIONS

        desired_status = _DISCORD_STATUS_FOR_STATE[event.state]
        existing = await self._store.get_scheduled_event_mapping(
            self._guild.id, event.platform, event.external_id
        )
        content_hash = _content_hash(event, desired_status)

        if existing is None:
            if desired_status != "scheduled":
                return ScheduledEventOutcome.SKIPPED_INVALID_TRANSITION
            return await self._create(event, content_hash)

        if not existing.owned_by_krubit:
            return ScheduledEventOutcome.SKIPPED_NOT_OWNED
        if desired_status not in _VALID_TRANSITIONS.get(existing.discord_status, frozenset()):
            return ScheduledEventOutcome.SKIPPED_INVALID_TRANSITION
        if existing.discord_status == desired_status and existing.content_hash == content_hash:
            return existing
        return await self._update(event, existing, desired_status, content_hash)

    async def _create(
        self, event: ScheduledStreamEvent, content_hash: str
    ) -> ScheduledEventMapping | ScheduledEventOutcome:
        end_at = event.scheduled_end_at or event.scheduled_start_at + _DEFAULT_DURATION
        image = (
            event.cover_image_bytes
            if event.cover_image_bytes is not None
            else discord.utils.MISSING
        )
        try:
            discord_event = await self._guild.create_scheduled_event(
                name=_bounded(event.title, _MAX_NAME_LENGTH),
                description=_bounded(event.description, _MAX_DESCRIPTION_LENGTH),
                start_time=event.scheduled_start_at,
                end_time=end_at,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=_bounded(event.canonical_url, _MAX_LOCATION_LENGTH),
                image=image,
                reason=_reason(event),
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
            return ScheduledEventOutcome.SKIPPED_DISCORD_ERROR
        now = self._now()
        mapping = ScheduledEventMapping(
            guild_id=event.guild_id,
            account_id=event.account_id,
            platform=event.platform,
            external_id=event.external_id,
            discord_event_id=discord_event.id,
            discord_status="scheduled",
            owned_by_krubit=True,
            content_hash=content_hash,
            created_at=now,
            updated_at=now,
        )
        return await self._store.save_scheduled_event_mapping(mapping)

    async def _update(
        self,
        event: ScheduledStreamEvent,
        existing: ScheduledEventMapping,
        desired_status: str,
        content_hash: str,
    ) -> ScheduledEventMapping | ScheduledEventOutcome:
        if existing.discord_event_id is None:
            return ScheduledEventOutcome.SKIPPED_DISCORD_ERROR
        discord_event = self._guild.get_scheduled_event(existing.discord_event_id)
        if discord_event is None:
            return ScheduledEventOutcome.SKIPPED_DISCORD_ERROR

        status = (
            _EVENT_STATUS_FOR_DISCORD_STATUS[desired_status]
            if desired_status != existing.discord_status
            else discord.utils.MISSING
        )
        detail_fields_apply = desired_status not in _TERMINAL_DISCORD_STATUSES
        if not detail_fields_apply and status is discord.utils.MISSING:
            return existing
        end_at = event.scheduled_end_at or event.scheduled_start_at + _DEFAULT_DURATION
        image = (
            event.cover_image_bytes
            if event.cover_image_bytes is not None
            else discord.utils.MISSING
        )
        try:
            if detail_fields_apply:
                await discord_event.edit(
                    name=_bounded(event.title, _MAX_NAME_LENGTH),
                    description=_bounded(event.description, _MAX_DESCRIPTION_LENGTH),
                    start_time=event.scheduled_start_at,
                    end_time=end_at,
                    location=_bounded(event.canonical_url, _MAX_LOCATION_LENGTH),
                    image=image,
                    status=status,
                    reason=_reason(event),
                )
            else:
                await discord_event.edit(status=status, reason=_reason(event))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
            return ScheduledEventOutcome.SKIPPED_DISCORD_ERROR

        mapping = ScheduledEventMapping(
            guild_id=event.guild_id,
            account_id=event.account_id,
            platform=event.platform,
            external_id=event.external_id,
            discord_event_id=existing.discord_event_id,
            discord_status=desired_status,
            owned_by_krubit=True,
            content_hash=content_hash,
            created_at=existing.created_at,
            updated_at=self._now(),
        )
        return await self._store.save_scheduled_event_mapping(mapping)
