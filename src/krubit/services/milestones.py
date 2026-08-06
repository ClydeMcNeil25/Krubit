"""Milestone evaluation and recognition-candidate shortlisting.

Every function in this module is pure: no I/O, no clock reads (the caller supplies
`now`), no Discord objects, no randomness — matching the discipline established by
`krubit.services.activation_retention`. Milestone rules are **named, explainable
rules** (message-count tiers, join anniversaries), never a black-box "loyalty
score" (see `krubit.domain.activity_ledger.MilestoneKind`'s docstring). Recognition
candidates surface **facts only** — which milestones were reached, which
participation-trend figures crossed a documented threshold — never a numeric
"worthiness" score and never generated recognition text. Per the design doc's
Recognition-candidate view and the rollout doc's Non-Negotiable Boundaries: Krubit
never decides who "deserves" recognition or drafts recognition wording — that is
explicitly Zariya's role. `RecognitionCandidate.reasons` is structurally required
to be non-empty (see its `__post_init__`), so this module cannot even accidentally
produce a bare, unexplained candidate.

## Message-count threshold design

`_MESSAGE_COUNT_THRESHOLDS` is a fixed, named tuple of tiers: 1 (first message —
celebrates a newcomer's first post), 10 and 50 (early, developing engagement), 100
(an established, regular participant), 500 and 1000 (sustained, high-volume
participants). These mirror common community-recognition tiers (first post, then
order-of-magnitude milestones) and are deliberately coarse and few in number so
each tier is meaningful and explainable, not a black-box curve. Like Phase 3's
risk-band thresholds, this is a configuration decision documented here rather than
derived from data; a guild wanting different tiers would need this constant
changed (or, in a later task, made per-guild configurable).

## Join-anniversary design

A join anniversary fires once per full year elapsed since the member's **earliest**
recorded join event for this guild (if a member rejoined, the earliest join is used
as the anchor — "member since" recognition conventionally counts from a member's
original join, not their most recent rejoin). Anniversary N's calendar date is the
join date advanced by N years (Feb 29 joins clamp to Feb 28 in non-leap years,
since there is no Feb 29 to land on). An anniversary "fires" (produces a
`Milestone`) once `now` is on or after that calendar date.

## Recognition-candidate notability thresholds

A member becomes a recognition candidate for a given window if, within that
trailing window ending at `now`:

- they reached at least one milestone (message-count tier or join anniversary), or
- their channel diversity reached `_NOTABLE_CHANNEL_DIVERSITY` (3) distinct
  channels — posting/reacting/joining voice across three or more channels is a
  concrete, verifiable breadth-of-participation fact, or
- their event-attendance diversity reached `_NOTABLE_EVENT_DIVERSITY` (2) distinct
  Scheduled Events, or
- their `returning` flag is set (they went quiet longer than the configured
  inactivity threshold and then resumed activity within the window) — a "welcome
  back" fact worth surfacing, not a judgment about why they were away.

Each of these is a plain, independently verifiable fact drawn from stored events —
never combined into a single score, and never used to rank candidates against each
other (the return order is by `member_id`, not by any notion of "best").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from krubit.domain.activity_ledger import (
    CohortWindow,
    JoinEvent,
    LedgerEvent,
    LedgerEventKind,
    Milestone,
    MilestoneKind,
    RecognitionCandidate,
    cohort_window_days,
)
from krubit.services.activation_retention import participation_trend

_MESSAGE_COUNT_THRESHOLDS: tuple[int, ...] = (1, 10, 50, 100, 500, 1000)

_NOTABLE_CHANNEL_DIVERSITY = 3
_NOTABLE_EVENT_DIVERSITY = 2
_INACTIVITY_THRESHOLD = timedelta(days=14)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _add_years(value: date, years: int) -> date:
    """Advance `value` by `years` calendar years, clamping Feb 29 to Feb 28."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # value was Feb 29 and value.year + years is not a leap year.
        return value.replace(year=value.year + years, month=2, day=28)


def _message_count_milestones(
    guild_id: int, member_id: int, events: tuple[LedgerEvent, ...]
) -> tuple[Milestone, ...]:
    messages = sorted(
        (event for event in events if event.kind is LedgerEventKind.MESSAGE),
        key=lambda event: event.occurred_at,
    )
    milestones: list[Milestone] = []
    for threshold in _MESSAGE_COUNT_THRESHOLDS:
        if len(messages) >= threshold:
            crossing_message = messages[threshold - 1]
            milestones.append(
                Milestone(
                    guild_id=guild_id,
                    member_id=member_id,
                    kind=MilestoneKind.MESSAGE_COUNT,
                    reached_at=crossing_message.occurred_at,
                    detail=f"message_count_{threshold}",
                )
            )
    return tuple(milestones)


def _join_anniversary_milestones(
    guild_id: int, member_id: int, events: tuple[LedgerEvent, ...], now: datetime
) -> tuple[Milestone, ...]:
    joins = sorted(
        (event for event in events if isinstance(event, JoinEvent)),
        key=lambda event: event.occurred_at,
    )
    if not joins:
        return ()

    earliest_join = joins[0]
    join_date = earliest_join.occurred_at.date()
    now_date = now.date()

    milestones: list[Milestone] = []
    year = 1
    while True:
        anniversary_date = _add_years(join_date, year)
        if anniversary_date > now_date:
            break
        reached_at = datetime.combine(
            anniversary_date, earliest_join.occurred_at.timetz()
        )
        milestones.append(
            Milestone(
                guild_id=guild_id,
                member_id=member_id,
                kind=MilestoneKind.JOIN_ANNIVERSARY,
                reached_at=reached_at,
                detail=f"join_anniversary_year_{year}",
            )
        )
        year += 1
    return tuple(milestones)


def evaluate_milestones(
    member_id: int,
    guild_id: int,
    events: tuple[LedgerEvent, ...],
    now: datetime,
) -> tuple[Milestone, ...]:
    """Evaluate every named milestone rule for one member as of `now`.

    Two rule families, per `krubit.domain.activity_ledger.MilestoneKind`:

    - `MESSAGE_COUNT`: fires once per threshold in `_MESSAGE_COUNT_THRESHOLDS` that
      the member's message count (within `events`) has reached, `reached_at` set to
      the timestamp of the message that crossed that threshold.
    - `JOIN_ANNIVERSARY`: fires once per full year elapsed since the member's
      earliest join event present in `events`, `reached_at` set to the anniversary
      calendar date (in the join event's own timezone/time-of-day). If no join
      event for this member is present in `events`, no anniversary milestones are
      produced (there is nothing to anchor to) — this is not an error, since
      `events` may legitimately be message-only history.

    Only events belonging to `member_id`/`guild_id` are considered; events for
    other members or other guilds are ignored. Milestones already reached before
    `now` and milestones reached exactly at `now` both fire (an anniversary or
    threshold-crossing message dated `now` counts); nothing in `events` dated after
    `now` can affect the result other than by not being present, since `events` is
    caller-supplied history, not filtered here.
    """
    if type(events) is not tuple:
        raise ValueError("events must be a tuple")
    _require_aware("now", now)
    if member_id <= 0:
        raise ValueError("member_id must be positive")
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")

    own_events = tuple(
        event
        for event in events
        if event.member_id == member_id and event.guild_id == guild_id
    )

    return _message_count_milestones(guild_id, member_id, own_events) + (
        _join_anniversary_milestones(guild_id, member_id, own_events, now)
    )


def _trailing_window_events(
    events: tuple[LedgerEvent, ...], window_days: int, now: datetime
) -> tuple[LedgerEvent, ...]:
    """Pre-filter `events` to the trailing window ending at real `now`.

    Required by `participation_trend`'s caller contract: that function anchors its
    window to the latest event date it is given, so callers must supply a real
    trailing window ending at actual wall-clock `now` themselves.
    """
    window_start = now - timedelta(days=window_days - 1)
    return tuple(event for event in events if window_start <= event.occurred_at <= now)


def _member_ids_in_guild(events: tuple[LedgerEvent, ...], guild_id: int) -> tuple[int, ...]:
    seen: dict[int, None] = {}
    for event in events:
        if event.guild_id == guild_id:
            seen.setdefault(event.member_id, None)
    return tuple(seen)


def _reasons_for_member(
    guild_id: int,
    member_id: int,
    member_events: tuple[LedgerEvent, ...],
    window: CohortWindow,
    now: datetime,
) -> tuple[str, ...]:
    window_days = cohort_window_days(window)
    windowed_events = _trailing_window_events(member_events, window_days, now)
    window_start = now - timedelta(days=window_days - 1)

    milestones = evaluate_milestones(member_id, guild_id, member_events, now)
    milestones_in_window = tuple(
        m for m in milestones if window_start <= m.reached_at <= now
    )

    trend = participation_trend(
        windowed_events, window, inactivity_threshold=_INACTIVITY_THRESHOLD
    )

    reasons: list[str] = [
        f"Reached milestone {m.kind.value} ({m.detail}) at {m.reached_at.isoformat()}"
        for m in milestones_in_window
    ]

    if trend.channel_diversity >= _NOTABLE_CHANNEL_DIVERSITY:
        reasons.append(
            f"channel_diversity={trend.channel_diversity} "
            f"(>= {_NOTABLE_CHANNEL_DIVERSITY}) in the last {window_days} days"
        )
    if trend.event_diversity >= _NOTABLE_EVENT_DIVERSITY:
        reasons.append(
            f"event_diversity={trend.event_diversity} "
            f"(>= {_NOTABLE_EVENT_DIVERSITY}) in the last {window_days} days"
        )
    if trend.returning:
        reasons.append(
            f"returning=true: resumed activity within the last {window_days} days "
            f"after a gap longer than {_INACTIVITY_THRESHOLD.days} days"
        )

    return tuple(reasons)


def recognition_candidates(
    guild_id: int,
    events: tuple[LedgerEvent, ...],
    window: CohortWindow,
    now: datetime,
) -> tuple[RecognitionCandidate, ...]:
    """Return a factual shortlist of members with notable, verifiable activity.

    For every member with at least one event in `events` for `guild_id`, this
    evaluates milestones reached within the trailing `window` ending at `now` and
    the member's `participation_trend` for that same trailing window (pre-filtered
    per that function's caller contract — see `_trailing_window_events`). A member
    becomes a candidate only if at least one fact is notable — see the module
    docstring's "Recognition-candidate notability thresholds" section. Every
    returned `RecognitionCandidate.reasons` is a non-empty tuple of plain factual
    statements (never a numeric score, never generated recognition text) —
    `RecognitionCandidate.__post_init__` enforces non-empty `reasons` structurally.

    Krubit surfaces facts only; deciding who deserves recognition and drafting the
    words is explicitly out of scope here (Zariya's role per the rollout doc).

    Candidates are returned ordered by `member_id`, not by any notion of "best" —
    there is no ranking, since ranking would require a score this module
    deliberately does not compute.
    """
    if type(events) is not tuple:
        raise ValueError("events must be a tuple")
    if type(window) is not CohortWindow:
        raise ValueError("window must be a CohortWindow")
    _require_aware("now", now)
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")

    candidates: list[RecognitionCandidate] = []
    for member_id in sorted(_member_ids_in_guild(events, guild_id)):
        member_events = tuple(
            event
            for event in events
            if event.guild_id == guild_id and event.member_id == member_id
        )
        reasons = _reasons_for_member(guild_id, member_id, member_events, window, now)
        if not reasons:
            continue
        candidates.append(
            RecognitionCandidate(
                guild_id=guild_id,
                member_id=member_id,
                window=window,
                reasons=reasons,
                evaluated_at=now,
            )
        )
    return tuple(candidates)
