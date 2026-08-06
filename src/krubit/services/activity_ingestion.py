"""The pre-storage channel-exclusion gate for Phase 4 activity-ledger ingestion.

`ActivityIngestionService.ingest` is the **only** place in the codebase that may
call `SQLiteStore.record_ledger_event` for a Phase 4 `LedgerEvent` — every
extraction function in `krubit.discord.activity_events` returns a plain domain
value object, never touches storage, and every future Discord-event handler
(cog wiring, a later task) is expected to route its extracted event through this
one service rather than calling `SQLiteStore` directly. That single-entry-point
design is what makes the exclusion check below structurally unavoidable rather
than a merely-conventional ordering:

- `ingest` contains exactly one call to `self._store.record_ledger_event`, and it
  is the last statement in the method, unconditionally reached only after the
  exclusion check above it returns without triggering the early `return False`.
  There is no second code path, no alternate public method, and no way to reach
  that call except by falling through the guard in this same function body — a
  future maintainer cannot add a new event kind or a new caller that skips the
  check without editing this exact function and visibly deleting the guard.
- The check itself only needs to run for event kinds that actually name a
  channel (`MessageEvent`, `ReactionEvent`, `VoiceSessionEvent` — see
  `_channel_id`); event kinds with no channel (join, onboarding, role change,
  milestone, moderation receipt, event attendance) have nothing for a channel
  exclusion to apply to, so they always proceed to storage. This mirrors the
  design doc's "Channel exclusion is enforced before storage" requirement
  without inventing a channel concept for event kinds that structurally don't
  have one.
"""

from __future__ import annotations

from krubit.domain.activity_ledger import (
    LedgerEvent,
    MessageEvent,
    ReactionEvent,
    VoiceSessionEvent,
)
from krubit.storage.sqlite import SQLiteStore


def _channel_id(event: LedgerEvent) -> int | None:
    """Return the channel `event` belongs to, or `None` for a channel-less kind."""
    if isinstance(event, (MessageEvent, ReactionEvent, VoiceSessionEvent)):
        return event.channel_id
    return None


class ActivityIngestionService:
    """Routes one extracted `LedgerEvent` past the guild's channel-exclusion list
    and, only if it passes, into durable ledger storage.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def ingest(self, event: LedgerEvent) -> bool:
        """Store `event` unless its channel is excluded for its guild.

        Returns whether the event was actually stored: `False` for an excluded
        channel, `True` otherwise. Never partially writes — either the exclusion
        check short-circuits before any storage call, or the event is stored in
        full.
        """
        channel_id = _channel_id(event)
        if channel_id is not None and await self._is_channel_excluded(event.guild_id, channel_id):
            return False
        await self._store.record_ledger_event(event)
        return True

    async def _is_channel_excluded(self, guild_id: int, channel_id: int) -> bool:
        exclusions = await self._store.list_exclusion_entries(guild_id)
        return any(entry.channel_id == channel_id for entry in exclusions)
