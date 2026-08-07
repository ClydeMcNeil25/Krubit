"""Pure activation, retention, and participation-trend calculation.

Every function in this module is pure: no I/O, no clock reads, no randomness, no
Discord objects. Calling any function twice with the same arguments always returns
an equal result, matching the discipline `krubit.domain.watchdog.evaluate_risk_band`
established in Phase 3. See `krubit.domain.activity_ledger`'s module docstring for
the "meaningful action" event-kind rationale and the cohort-window boundary
discipline these functions implement.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from krubit.domain.activity_ledger import (
    MEANINGFUL_EVENT_KINDS,
    ActivationResult,
    CohortResult,
    CohortWindow,
    JoinEvent,
    LedgerEvent,
    ParticipationTrend,
    cohort_window_days,
)

_CHANNEL_BEARING_KINDS = {"message", "reaction", "voice_session"}


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _channel_id(event: LedgerEvent) -> int | None:
    """Return the channel an event happened in, or None if it has no channel."""
    return getattr(event, "channel_id", None)


def _scheduled_event_id(event: LedgerEvent) -> int | None:
    return getattr(event, "scheduled_event_id", None)


def time_to_activation(
    join_at: datetime, events: tuple[LedgerEvent, ...]
) -> ActivationResult:
    """Find the first meaningful action at or after `join_at` and its elapsed time.

    A "meaningful action" is any event whose `kind` is in
    `krubit.domain.activity_ledger.MEANINGFUL_EVENT_KINDS` (message, reaction, voice
    session, event attendance) — see that module's docstring for why exactly this
    set. Events strictly before `join_at` are ignored entirely (they belong to a
    prior membership period, e.g. before a rejoin); events at or after `join_at`
    are candidates, using the same "current instant included" half-open-interval
    convention Phase 3's quiet-hours logic uses for `[start, end)` windows.

    Absence of any qualifying event is reported as `activated=False`, never an
    error — "not yet activated" is an ordinary, expected outcome.
    """
    _require_aware("join_at", join_at)
    if type(events) is not tuple:
        raise ValueError("events must be a tuple")

    candidates = sorted(
        (
            event
            for event in events
            if event.kind in MEANINGFUL_EVENT_KINDS and event.occurred_at >= join_at
        ),
        key=lambda event: event.occurred_at,
    )
    if not candidates:
        return ActivationResult(activated=False, time_to_activation=None, activating_kind=None)

    earliest = candidates[0]
    return ActivationResult(
        activated=True,
        time_to_activation=earliest.occurred_at - join_at,
        activating_kind=earliest.kind,
    )


def cohort_membership(
    joins: tuple[JoinEvent, ...],
    events: tuple[LedgerEvent, ...],
    window: CohortWindow,
) -> CohortResult:
    """Compute the fraction of a join cohort retained within `window` days of joining.

    A member counts as retained if at least one meaningful-action event (see
    `time_to_activation`'s docstring) for that same member exists whose **calendar
    date** falls in `[join_date, join_date + window_days]`, inclusive on both ends —
    the join day itself counts, and the window's final day counts, matching Phase
    3's quiet-hours half-open-interval discipline applied to date boundaries (see
    the module docstring in `krubit.domain.activity_ledger` for the exact rationale).
    Comparison is by calendar date, not exact timestamp, so an event that happened
    earlier in the day than the member's exact join timestamp — but on the same
    calendar day — still counts.

    `cohort_size` is `len(joins)`; if a member appears more than once in `joins`
    (e.g. a rejoin), each entry is counted as its own cohort member, matching the
    per-`JoinEvent` (not per-member) counting the design doc's fixtures use.
    """
    if type(joins) is not tuple:
        raise ValueError("joins must be a tuple")
    if type(events) is not tuple:
        raise ValueError("events must be a tuple")

    window_days = cohort_window_days(window)

    meaningful_events = [event for event in events if event.kind in MEANINGFUL_EVENT_KINDS]

    retained_count = 0
    for join in joins:
        join_date = join.occurred_at.date()
        window_end_date = join_date + timedelta(days=window_days)
        if any(
            event.member_id == join.member_id
            and _in_inclusive_date_range(event.occurred_at.date(), join_date, window_end_date)
            for event in meaningful_events
        ):
            retained_count += 1

    cohort_size = len(joins)
    retention_rate = 0.0 if cohort_size == 0 else retained_count / cohort_size
    return CohortResult(
        window=window,
        cohort_size=cohort_size,
        retained_count=retained_count,
        retention_rate=retention_rate,
    )


def _in_inclusive_date_range(value: date, start: date, end: date) -> bool:
    return start <= value <= end


def participation_trend_fetch_window_days(
    window_days: int, inactivity_threshold: timedelta
) -> int:
    """How many trailing days of raw events a caller must fetch/pre-filter before
    calling `participation_trend`, so a real gap longer than `inactivity_threshold`
    can still be represented even when the threshold is as large as, or larger
    than, `window_days` itself.

    See `participation_trend`'s `returning` docstring: it looks for a gap between
    two consecutive active days among ALL the events it is given, not just the
    events inside its own reported `window`. If a caller pre-filters `events` to
    exactly `window_days` before calling, the widest gap two active days within
    that filtered set can ever have is `window_days - 1` days -- so whenever
    `inactivity_threshold >= window_days - 1`, `returning` becomes structurally
    unreachable: no representable gap can exceed the threshold. This function
    returns a wider trailing-day count -- `max(window_days, threshold_days) +
    window_days` -- that a caller should use for the *fetch/pre-filter* step only,
    while still passing the original, unwidened `window` (a `CohortWindow`) to
    `participation_trend` itself so its reported `active_day_count`/diversity
    figures stay scoped to the real, unwidened window.
    """
    threshold_days = inactivity_threshold.days
    return max(window_days, threshold_days) + window_days


def participation_trend(
    events: tuple[LedgerEvent, ...],
    window: CohortWindow,
    inactivity_threshold: timedelta,
) -> ParticipationTrend:
    """Compute active-day count, a "returning" flag, and channel/event diversity.

    **CALLER CONTRACT — READ BEFORE CALLING:** `events` MUST already be pre-filtered
    to a real trailing window ending at actual wall-clock "now" before it is passed
    in here. This function is pure and never reads a clock, so it has no concept of
    real "now" — it anchors its trailing window to the **latest event actually
    present in `events`** (see below). If you pass a member's full, unfiltered
    historical event log, the anchor day becomes that member's last-ever event, and
    the result will show `active_day_count > 0` and look "currently active" no
    matter how long ago that activity actually happened — a member silent for a
    year looks identical to one active five minutes ago. There is no way for this
    function to detect or warn about that misuse internally; filtering `events` to
    the real trailing window before calling is entirely the caller's responsibility.
    This matters most for the design doc's "Inactive view" and "community-pulse
    view," which must be able to tell true current inactivity apart from old
    activity — do not call this with an unfiltered history and expect it to work.

    Pure, so there is no clock to read for "now." Instead, the trailing `window`
    (7 or 30 calendar days) is anchored to the **latest meaningful-action event's
    calendar date** among the supplied `events` — the most recent day of observed
    activity — covering `[anchor_day - (window_days - 1), anchor_day]` inclusive on
    both ends. If `events` contains no meaningful-action event, the trend is all
    zeros / not returning; there is no activity to anchor a window to.

    - `active_day_count`: distinct calendar dates with at least one meaningful-action
      event, within the trend window.
    - `channel_diversity`: distinct channel IDs across message/reaction/voice-session
      events within the trend window.
    - `event_diversity`: distinct Scheduled Event IDs across event-attendance events
      within the trend window.
    - `returning`: True if, among ALL meaningful-action active days (not limited to
      the trend window), two consecutive active days are separated by more than
      `inactivity_threshold`, and the later day of that gap falls within the trend
      window — i.e. the member went quiet for longer than the configured threshold
      and then resumed activity inside this window.

    All four figures are explainable and traceable to specific stored events, never
    a single opaque trend score, per the design doc.
    """
    if type(events) is not tuple:
        raise ValueError("events must be a tuple")
    if inactivity_threshold <= timedelta(0):
        raise ValueError("inactivity_threshold must be positive")

    meaningful_events = [event for event in events if event.kind in MEANINGFUL_EVENT_KINDS]
    if not meaningful_events:
        return ParticipationTrend(
            window=window,
            active_day_count=0,
            returning=False,
            channel_diversity=0,
            event_diversity=0,
        )

    window_days = cohort_window_days(window)
    anchor_day = max(event.occurred_at.date() for event in meaningful_events)
    window_start_day = anchor_day - timedelta(days=window_days - 1)

    active_days_all = sorted({event.occurred_at.date() for event in meaningful_events})
    active_days_in_window = [
        day for day in active_days_all if window_start_day <= day <= anchor_day
    ]

    channels_in_window = {
        _channel_id(event)
        for event in meaningful_events
        if event.kind in _CHANNEL_BEARING_KINDS
        and window_start_day <= event.occurred_at.date() <= anchor_day
    }
    events_in_window = {
        _scheduled_event_id(event)
        for event in meaningful_events
        if event.kind == "event_attendance"
        and window_start_day <= event.occurred_at.date() <= anchor_day
    }

    returning = False
    for previous_day, current_day in zip(active_days_all, active_days_all[1:], strict=False):
        gap = timedelta(days=(current_day - previous_day).days)
        if gap > inactivity_threshold and current_day >= window_start_day:
            returning = True
            break

    return ParticipationTrend(
        window=window,
        active_day_count=len(active_days_in_window),
        returning=returning,
        channel_diversity=len(channels_in_window),
        event_diversity=len(events_in_window),
    )
