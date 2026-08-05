"""Bounded, auto-expiring post-join watch window: lifecycle + message inspection.

`WatchWindowService` is the second service-layer consumer of Task 2's watchdog
storage methods (after `krubit.services.entry_sniff.EntrySniffService`) and the first
consumer of Task 3's `krubit.discord.watchdog_events.extract_join_signals`'s sibling,
`extract_message_signals`. Per the design doc's Post-Join Watch Window section, a
watch window is "opened automatically only for `watch` band or higher (never for
`clear`)... downgrades/closes automatically on expiry — Krubit does not require a
human action to end a watch window for a member who caused no further signal." This
service owns exactly that lifecycle and nothing else: it never opens a window for
`RiskBand.CLEAR`, it never mutates a member/role/message, and every open/close
transition is receipted via `record_sniff_receipt`, matching
`EntrySniffService._record_receipt`'s established pattern.

## Watch window duration (safety-sensitive — read before changing)

The design doc calls for a "bounded duration, configurable per guild with a safe
default" but does not name a number, and no per-guild configuration table exists yet
in this phase's data model (`docs/superpowers/specs/2026-08-05-phase-3-watchdog-
design.md`'s Data Model section has no `guild_id`-scoped duration column anywhere).
This task therefore ships a single fixed default, `WATCH_WINDOW_DURATION = 24 hours`,
accepted as a constructor override (`duration=`) so a later per-guild-configuration
task can thread a stored value through without changing this service's shape:

- Long enough to span a full day/night cycle. Raid and spam-wave participants often
  operate from a different timezone than the guild's active hours, and a coordinated
  actor may deliberately wait past the first few minutes (when moderators are most
  alert) before posting — a window measured in minutes would miss that "wait it out"
  pattern entirely.
- Bounded, not standing. 24 hours is a small, fixed fraction of a member's Discord
  tenure, matching "a bounded, automatically-expiring elevated-monitoring state, not a
  standing surveillance mode" — a genuinely clean member ages back out of any
  elevated state within one day of joining, with no residual behavioral log kept past
  that point (this service's own in-memory message-similarity cache for that member is
  discarded the moment `sweep_expired` closes their window; see below).

## In-memory-only message history: the second half of "no standing behavioral log"

`inspect_message`'s repeated-content check ("a bounded similarity check against the
member's own last few messages within the window") needs *something* to compare a new
message against. This service keeps that comparison state — the last
`_MESSAGE_HISTORY_LIMIT` message bodies per `(guild_id, member_id)` — entirely in
process memory, never in `SQLiteStore`. This is a deliberate reading of the design
doc's "no standing behavioral log survives a closed, clean watch window" boundary:
even the raw text needed for same-member near-duplicate detection must not become a
durable artifact, so it lives only as long as the *process* keeps it, and is
explicitly discarded (`_message_history.pop`) the moment `sweep_expired` closes that
member's window. The unavoidable cost of this choice is that a bot restart during an
open window silently resets each watched member's comparison history — an accepted
trade-off, since the alternative (a durable per-message content table) is exactly the
standing behavioral log the design doc rules out.

## Cross-member isolation

Per the design doc, cross-member near-duplicate correlation ("multiple currently-
watched ... members posting near-duplicate content within a short guild-wide window")
is `RaidDetector`'s job (Task 5, not yet built), explicitly distinct from this
service's `repeated_content_near_duplicate` check, which is keyed by
`(guild_id, member_id)` and therefore structurally incapable of comparing one member's
message against another member's history.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Final, Protocol
from uuid import uuid4

from krubit.discord.watchdog_events import MessageSubject, extract_message_signals
from krubit.domain.models import JSONValue
from krubit.domain.watchdog import (
    EntrySniffAssessment,
    RiskBand,
    RiskSignal,
    WatchWindow,
    WatchWindowCloseReason,
)
from krubit.storage.sqlite import SQLiteStore

WATCH_WINDOW_DURATION: Final[timedelta] = timedelta(hours=24)

# Bounded to a handful of recent messages per member — enough to catch a rapid-fire
# repeated-message flood without holding an unbounded amount of text in memory.
_MESSAGE_HISTORY_LIMIT = 5
_REPEATED_HISTORY_SIMILARITY = 0.85
_REPEATED_HISTORY_WEIGHT = 4
_REPEATED_HISTORY_CONFIDENCE = 0.55


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class _HasId(Protocol):
    @property
    def id(self) -> int: ...


class InspectableMessage(MessageSubject, Protocol):
    """`MessageSubject` plus the routing fields `inspect_message` needs to find a
    member's watch window. Never a DM — `guild` is nullable only because
    `discord.Message.guild` is `None` for DMs; `inspect_message` treats that as "not
    inspectable" and returns `None`, matching the design doc's "it never reads or
    stores DM content at all" boundary.
    """

    @property
    def author(self) -> _HasId: ...
    @property
    def guild(self) -> _HasId | None: ...


class WatchWindowService:
    """Own the bounded watch-window lifecycle while storage stays dumb persistence."""

    def __init__(self, store: SQLiteStore, *, duration: timedelta = WATCH_WINDOW_DURATION) -> None:
        if duration <= timedelta(0):
            raise ValueError("duration must be positive")
        self._store = store
        self._duration = duration
        self._message_history: dict[tuple[int, int], deque[str]] = defaultdict(
            lambda: deque(maxlen=_MESSAGE_HISTORY_LIMIT)
        )

    async def open_if_warranted(
        self, assessment: EntrySniffAssessment, now: datetime
    ) -> WatchWindow | None:
        """Open a bounded watch window for `assessment`, or do nothing for `CLEAR`.

        Per the design doc, opened "only for `watch` band or higher (never for
        `clear`)" — this is checked structurally against `assessment.band` here, not
        left to `WatchWindow`'s own `__post_init__` guard (which exists as a second,
        independent line of defense, not the primary gate).
        """
        _require_aware("now", now)
        if assessment.band is RiskBand.CLEAR:
            return None

        window = WatchWindow(
            guild_id=assessment.guild_id,
            member_id=assessment.member_id,
            opened_at=now,
            expires_at=now + self._duration,
            band=assessment.band,
            closed_at=None,
            close_reason=None,
        )
        saved = await self._store.open_watch_window(window)
        await self._record_receipt(
            guild_id=saved.guild_id,
            member_id=saved.member_id,
            action="watch_window_opened",
            detail={"band": saved.band.value, "expires_at": saved.expires_at.isoformat()},
            now=now,
        )
        return saved

    async def sweep_expired(self, guild_id: int, now: datetime) -> tuple[WatchWindow, ...]:
        """Close every open watch window in `guild_id` past its `expires_at`.

        Idempotent by construction: `SQLiteStore.close_watch_window` only matches rows
        still `closed_at IS NULL`, so sweeping the same already-closed window again is
        a no-op that returns nothing for it, matching the storage layer's own
        documented idempotent-close guarantee.
        """
        _require_aware("now", now)
        open_windows = await self._store.list_open_watch_windows(guild_id)

        closed: list[WatchWindow] = []
        for window in open_windows:
            if window.expires_at > now:
                continue
            result = await self._store.close_watch_window(
                guild_id, window.member_id, reason=WatchWindowCloseReason.EXPIRED, now=now
            )
            self._message_history.pop((guild_id, window.member_id), None)
            if result is None:
                continue
            await self._record_receipt(
                guild_id=result.guild_id,
                member_id=result.member_id,
                action="watch_window_closed",
                detail={"close_reason": WatchWindowCloseReason.EXPIRED.value},
                now=now,
            )
            closed.append(result)
        return tuple(closed)

    async def inspect_message(
        self, message: InspectableMessage, now: datetime
    ) -> RiskSignal | None:
        """Extract the single most significant signal from one watched member's message.

        Defends against being called for a member with no currently open watch
        window — including a DM (`message.guild is None`) — by returning `None`
        rather than raising, since "a race between window expiry and an in-flight
        message is expected and normal" (this task's brief). The caller (Task 7's
        runtime) is expected to only invoke this for watched members in the first
        place; this check is a second, independent line of defense, not the primary
        gate.

        `extract_message_signals` can return several independent signals for one
        message; this method also appends its own stateful, same-member-only
        `repeated_content_near_duplicate` check (see the module docstring). Because the
        interface returns a single `RiskSignal | None`, when more than one signal
        fires this method returns whichever has the greatest effective weight
        (`weight * confidence`) — the same quantity `evaluate_risk_band` sums over, so
        the signal returned here is always the single strongest piece of evidence, not
        an arbitrary pick. No signal information is persisted or discarded by this
        method beyond that selection; a future incident-evidence task (Task 6) that
        needs every fired signal, not just the strongest, should call
        `extract_message_signals` directly rather than through this method.
        """
        _require_aware("now", now)
        guild = message.guild
        if guild is None:
            return None

        window = await self._get_open_window(guild.id, message.author.id)
        if window is None:
            return None

        signals: list[RiskSignal] = list(extract_message_signals(message, now))
        history_signal = self._check_repeated_history(guild.id, message.author.id, message.content)
        if history_signal is not None:
            signals.append(history_signal)

        self._remember_message(guild.id, message.author.id, message.content)

        if not signals:
            return None
        return max(signals, key=lambda signal: signal.weight * signal.confidence)

    async def _get_open_window(self, guild_id: int, member_id: int) -> WatchWindow | None:
        windows = await self._store.list_open_watch_windows(guild_id)
        return next((window for window in windows if window.member_id == member_id), None)

    def _check_repeated_history(
        self, guild_id: int, member_id: int, content: str
    ) -> RiskSignal | None:
        normalized = content.strip().lower()
        if not normalized:
            return None
        history = self._message_history.get((guild_id, member_id))
        if not history:
            return None
        for previous in history:
            similarity = SequenceMatcher(None, normalized, previous).ratio()
            if similarity >= _REPEATED_HISTORY_SIMILARITY:
                return RiskSignal(
                    name="repeated_content_near_duplicate",
                    weight=_REPEATED_HISTORY_WEIGHT,
                    detail=(
                        f"message is a {similarity:.0%} match to this member's own "
                        "recent message within this watch window"
                    ),
                    confidence=_REPEATED_HISTORY_CONFIDENCE,
                )
        return None

    def _remember_message(self, guild_id: int, member_id: int, content: str) -> None:
        normalized = content.strip().lower()
        if not normalized:
            return
        self._message_history[(guild_id, member_id)].append(normalized)

    async def _record_receipt(
        self,
        *,
        guild_id: int,
        member_id: int,
        action: str,
        detail: dict[str, JSONValue],
        now: datetime,
    ) -> None:
        await self._store.record_sniff_receipt(
            guild_id=guild_id,
            receipt_id=f"watch-window:{uuid4().hex}",
            member_id=member_id,
            action=action,
            detail=detail,
            created_at=now,
        )
