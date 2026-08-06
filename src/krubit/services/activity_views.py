"""Newcomer, inactive, returning-member, and community-pulse views.

Unlike `krubit.services.activation_retention` and `krubit.services.milestones`
(pure functions over a caller-supplied event tuple), the four views in this module
are guild-wide: they need to reason across every member in a guild at once, so
each one reads directly from `SQLiteStore` rather than taking a pre-filtered event
tuple. Every view is guild-scoped, reads only stored facts (see the design doc's
"Views" section), and never leaks one member's detail into another member's entry
beyond what the view's stated purpose requires — `community_pulse` is guild-wide
by design; `newcomer_view`, `inactive_view`, and `returning_member_view` are
one-entry-per-member and never mix events across members when building an entry.

## "Has this member left" — reusing Phase 1's tracking, not duplicating it

The activity ledger (`krubit.domain.activity_ledger`) has no "member left" event
kind of its own — Phase 1 already tracks this via `KrubitBot.on_member_remove`
(`discord/bot.py`), which records a `"member_left"` row in the pre-existing,
guild-scoped `guild_events` table (`SQLiteStore.accept_event`/`list_events`).
`inactive_view` reads that table directly through `SQLiteStore.list_events`,
exactly the precedent `WebhookAbuseDetector`/`PermissionRiskDetector`
(`services/webhook_and_permission_risk.py`) already set for a later-phase service
reading Phase 1's event log rather than inventing a second leave-tracking
mechanism. `list_events` is capped at `_LEFT_EVENT_SCAN_LIMIT` most-recent guild
events (matching `webhook_and_permission_risk.py`'s `_EVENTS_SCAN_LIMIT`
precedent exactly), so a guild whose event volume between a member's departure and
"now" exceeds that cap could miss that departure — an accepted limitation of the
existing table's scan-based reads, not something this module introduces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from krubit.domain.activity_ledger import (
    MEANINGFUL_EVENT_KINDS,
    CohortResult,
    CohortWindow,
    EventAttendanceEvent,
    JoinEvent,
    LedgerEvent,
    MessageEvent,
    ParticipationTrend,
    ReactionEvent,
    VoiceSessionEvent,
    cohort_window_days,
)
from krubit.services.activation_retention import (
    cohort_membership,
    participation_trend,
    time_to_activation,
)
from krubit.storage.sqlite import SQLiteStore

_LEFT_EVENT_SCAN_LIMIT = 500
_LEFT_EVENT_TYPE = "member_left"

# Fixed trailing window used to decide whether a member's "returning" resumption
# happened recently enough to surface. The design doc's `returning_member_view`
# signature (guild_id, inactivity_threshold, now) names no separate window
# parameter, so this stays a fixed, named constant rather than silently growing
# the view's parameter list — matching the "named, explainable rule, not
# per-guild configuration" discipline `activity_ledger`'s module docstring uses
# for `MEANINGFUL_EVENT_KINDS`.
_RETURNING_TREND_WINDOW = CohortWindow.THIRTY_DAY


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _require_positive_timedelta(name: str, value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class NewcomerEntry:
    """One member who joined within `newcomer_view`'s recent window.

    `last_meaningful_action_at` is this member's own most recent meaningful-action
    timestamp only (or `None` if they have none yet) — never another member's.
    """

    guild_id: int
    member_id: int
    joined_at: datetime
    activated: bool
    time_to_activation: timedelta | None
    last_meaningful_action_at: datetime | None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("joined_at", self.joined_at)
        if self.activated and self.time_to_activation is None:
            raise ValueError("an activated entry must set time_to_activation")
        if not self.activated and self.time_to_activation is not None:
            raise ValueError("a non-activated entry must not set time_to_activation")


@dataclass(frozen=True, slots=True)
class InactiveEntry:
    """One member with no meaningful action within `inactive_view`'s threshold.

    Members known to have left the guild (see the module docstring) are never
    represented here.
    """

    guild_id: int
    member_id: int
    joined_at: datetime | None
    last_meaningful_action_at: datetime | None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)


@dataclass(frozen=True, slots=True)
class ReturningMemberEntry:
    """One member whose participation trend shows a resumed-activity gap."""

    guild_id: int
    member_id: int
    trend: ParticipationTrend

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        if not self.trend.returning:
            raise ValueError("a ReturningMemberEntry's trend must be returning")


@dataclass(frozen=True, slots=True)
class CommunityPulse:
    """A guild-wide factual summary: no sentiment, no member-level detail beyond
    what's already staff-authorized elsewhere (per the design doc's Views section).
    """

    guild_id: int
    window: CohortWindow
    active_member_count: int
    cohort: CohortResult
    channel_contribution_count: int
    event_contribution_count: int

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        if type(self.window) is not CohortWindow:
            raise ValueError("window must be a CohortWindow")
        if self.active_member_count < 0:
            raise ValueError("active_member_count must not be negative")
        if self.channel_contribution_count < 0:
            raise ValueError("channel_contribution_count must not be negative")
        if self.event_contribution_count < 0:
            raise ValueError("event_contribution_count must not be negative")


def _group_events_by_member(
    events: tuple[LedgerEvent, ...],
) -> dict[int, tuple[LedgerEvent, ...]]:
    grouped: dict[int, list[LedgerEvent]] = {}
    for event in events:
        grouped.setdefault(event.member_id, []).append(event)
    return {member_id: tuple(member_events) for member_id, member_events in grouped.items()}


def _trailing_window_events(
    events: tuple[LedgerEvent, ...], window_days: int, now: datetime
) -> tuple[LedgerEvent, ...]:
    """Pre-filter `events` to the trailing window ending at real `now`.

    Required by `participation_trend`'s caller contract: that function anchors its
    window to the latest event date it is given, so callers must supply a real
    trailing window ending at actual wall-clock `now` themselves. See
    `krubit.services.milestones._trailing_window_events` for the identical
    precedent this mirrors.
    """
    window_start = now - timedelta(days=window_days - 1)
    return tuple(event for event in events if window_start <= event.occurred_at <= now)


async def _left_member_ids(store: SQLiteStore, guild_id: int) -> frozenset[int]:
    """Members known to have left `guild_id`, per Phase 1's `guild_events` table.

    See the module docstring's "Has this member left" section.
    """
    events = await store.list_events(guild_id, limit=_LEFT_EVENT_SCAN_LIMIT)
    left_ids: set[int] = set()
    for event in events:
        if event.event_type != _LEFT_EVENT_TYPE:
            continue
        entity_id = event.payload.get("entity_id")
        if entity_id is None:
            continue
        try:
            left_ids.add(int(str(entity_id)))
        except ValueError:
            continue
    return frozenset(left_ids)


async def newcomer_view(
    store: SQLiteStore, guild_id: int, recent_window: timedelta, now: datetime
) -> tuple[NewcomerEntry, ...]:
    """Members who joined within `recent_window` of `now`, their activation status,
    and their own most recent meaningful-action timestamp.

    If a member has more than one join event within the window (a rejoin), the
    latest one is used as their entry's `joined_at`, matching `newcomer_view`'s
    one-entry-per-member contract. Entries are ordered by `member_id`.
    """
    _require_positive_id("guild_id", guild_id)
    _require_aware("now", now)
    _require_positive_timedelta("recent_window", recent_window)

    all_events = await store.list_ledger_events_for_guild(guild_id)
    events_by_member = _group_events_by_member(all_events)

    window_start = now - recent_window
    latest_join_by_member: dict[int, JoinEvent] = {}
    for event in all_events:
        if isinstance(event, JoinEvent) and window_start <= event.occurred_at <= now:
            current = latest_join_by_member.get(event.member_id)
            if current is None or event.occurred_at > current.occurred_at:
                latest_join_by_member[event.member_id] = event

    entries: list[NewcomerEntry] = []
    for member_id in sorted(latest_join_by_member):
        join = latest_join_by_member[member_id]
        member_events = events_by_member.get(member_id, ())
        activation = time_to_activation(join.occurred_at, member_events)
        meaningful_at = [
            event.occurred_at for event in member_events if event.kind in MEANINGFUL_EVENT_KINDS
        ]
        entries.append(
            NewcomerEntry(
                guild_id=guild_id,
                member_id=member_id,
                joined_at=join.occurred_at,
                activated=activation.activated,
                time_to_activation=activation.time_to_activation,
                last_meaningful_action_at=max(meaningful_at, default=None),
            )
        )
    return tuple(entries)


async def inactive_view(
    store: SQLiteStore, guild_id: int, inactivity_threshold: timedelta, now: datetime
) -> tuple[InactiveEntry, ...]:
    """Members with no meaningful action within `inactivity_threshold` of `now`,
    excluding members who left (see the module docstring).

    "Within the threshold" is the half-open trailing window `[now -
    inactivity_threshold, now]`: a meaningful action at or after that cutoff counts
    as active, matching the codebase's quiet-hours boundary discipline (Phase 3)
    applied here to a recency cutoff. A member with no meaningful action at all is
    inactive by the same definition. Entries are ordered by `member_id`.
    """
    _require_positive_id("guild_id", guild_id)
    _require_aware("now", now)
    _require_positive_timedelta("inactivity_threshold", inactivity_threshold)

    all_events = await store.list_ledger_events_for_guild(guild_id)
    events_by_member = _group_events_by_member(all_events)
    left_member_ids = await _left_member_ids(store, guild_id)

    earliest_join_by_member: dict[int, datetime] = {}
    for event in all_events:
        if isinstance(event, JoinEvent):
            existing = earliest_join_by_member.get(event.member_id)
            if existing is None or event.occurred_at < existing:
                earliest_join_by_member[event.member_id] = event.occurred_at

    cutoff = now - inactivity_threshold
    entries: list[InactiveEntry] = []
    for member_id in sorted(events_by_member):
        if member_id in left_member_ids:
            continue
        member_events = events_by_member[member_id]
        meaningful_at = [
            event.occurred_at for event in member_events if event.kind in MEANINGFUL_EVENT_KINDS
        ]
        last_meaningful_at = max(meaningful_at, default=None)
        if last_meaningful_at is not None and last_meaningful_at >= cutoff:
            continue
        entries.append(
            InactiveEntry(
                guild_id=guild_id,
                member_id=member_id,
                joined_at=earliest_join_by_member.get(member_id),
                last_meaningful_action_at=last_meaningful_at,
            )
        )
    return tuple(entries)


async def returning_member_view(
    store: SQLiteStore, guild_id: int, inactivity_threshold: timedelta, now: datetime
) -> tuple[ReturningMemberEntry, ...]:
    """Members who had a gap exceeding `inactivity_threshold` and then resumed
    activity, per `participation_trend`'s `returning` flag over the fixed
    `_RETURNING_TREND_WINDOW` trailing window ending at `now`. Entries are ordered
    by `member_id`.
    """
    _require_positive_id("guild_id", guild_id)
    _require_aware("now", now)
    _require_positive_timedelta("inactivity_threshold", inactivity_threshold)

    all_events = await store.list_ledger_events_for_guild(guild_id)
    events_by_member = _group_events_by_member(all_events)
    window_days = cohort_window_days(_RETURNING_TREND_WINDOW)

    entries: list[ReturningMemberEntry] = []
    for member_id in sorted(events_by_member):
        windowed_events = _trailing_window_events(events_by_member[member_id], window_days, now)
        trend = participation_trend(
            windowed_events, _RETURNING_TREND_WINDOW, inactivity_threshold=inactivity_threshold
        )
        if trend.returning:
            entries.append(
                ReturningMemberEntry(guild_id=guild_id, member_id=member_id, trend=trend)
            )
    return tuple(entries)


async def community_pulse(
    store: SQLiteStore, guild_id: int, window: CohortWindow, now: datetime
) -> CommunityPulse:
    """A guild-wide factual summary: active-member count, cohort retention rate,
    and channel/event contribution — all within the trailing `window` ending at
    `now`, all no sentiment and no member-level detail beyond what's already
    staff-authorized elsewhere.

    Cohort retention is computed over every join event recorded for the guild
    (not filtered to `window`), matching `cohort_membership`'s own semantics of
    measuring retention relative to each member's join date.
    """
    _require_positive_id("guild_id", guild_id)
    _require_aware("now", now)
    if type(window) is not CohortWindow:
        raise ValueError("window must be a CohortWindow")

    all_events = await store.list_ledger_events_for_guild(guild_id)
    window_days = cohort_window_days(window)
    window_start = now - timedelta(days=window_days - 1)
    windowed_events = tuple(
        event for event in all_events if window_start <= event.occurred_at <= now
    )

    meaningful_in_window = [
        event for event in windowed_events if event.kind in MEANINGFUL_EVENT_KINDS
    ]
    active_member_count = len({event.member_id for event in meaningful_in_window})

    channel_ids: set[int] = set()
    event_ids: set[int] = set()
    for event in meaningful_in_window:
        if isinstance(event, MessageEvent | ReactionEvent | VoiceSessionEvent):
            channel_ids.add(event.channel_id)
        elif isinstance(event, EventAttendanceEvent):
            event_ids.add(event.scheduled_event_id)

    joins = tuple(event for event in all_events if isinstance(event, JoinEvent))
    meaningful_all = tuple(event for event in all_events if event.kind in MEANINGFUL_EVENT_KINDS)
    cohort = cohort_membership(joins, meaningful_all, window)

    return CommunityPulse(
        guild_id=guild_id,
        window=window,
        active_member_count=active_member_count,
        cohort=cohort,
        channel_contribution_count=len(channel_ids),
        event_contribution_count=len(event_ids),
    )
