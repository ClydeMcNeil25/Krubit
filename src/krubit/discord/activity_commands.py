"""Staff-only `/fetch member`, `/fetch newcomers`, `/fetch inactive`, `/fetch
retention`, and `/fetch community-pulse` command surfaces, plus the two
staff-or-self commands `/fetch activity` and `/fetch milestones`.

`ActivityCommandService` is the framework-independent core, matching
`WatchdogCommandService`'s (`krubit.discord.watchdog_commands`) own division of
labor: every method takes a plain `ActivityActorContext` (never a
`discord.Interaction`) so authority and self-view properties are directly
unit-testable without any `discord.Interaction` mocking. `CommandStatus`/
`CommandResult` are reused from `content_commands` rather than redefined here,
matching `watchdog_commands.py`'s own precedent.

## Authority: staff-only vs. staff-or-self

`member`, `newcomers`, `inactive`, `retention`, and `community_pulse` check
`actor.is_staff` as their first statement, before any storage query -- a
non-staff actor never causes a single read against `SQLiteStore`, matching
`watchdog_commands.py`'s "denied before any query or preview" discipline (a
Phase 2 review specifically found and fixed a command that skipped this
ordering, and Phase 3's `WatchdogCommandService` module docstring records the
same lesson).

`activity` and `milestones` additionally accept a self-view path: a caller
querying their own data (`target.member_id == actor.member_id`) is allowed even
when `actor.is_staff` is `False`. That equality check happens inside this
service, not merely in the Discord-layer UI default for an omitted `member`
argument -- a caller who explicitly passes another member's ID as the `target`
argument is re-validated here and denied unless they are staff, exactly the
same as any other cross-member access attempt. Per the design doc's Explicit
Exclusions ("No comparison of one member's data to another's in the
self-view"), the self-view render is a genuinely reduced subset of the
staff-view render (see `activity`'s docstring) rather than merely the same
data with a different title.

## No personality, loyalty, mental-health, or guilt language

Every card this module renders states plain, stored facts only (counts,
timestamps, boolean flags) -- matching the design doc's Explicit Exclusions and
the rollout doc's Non-Negotiable Boundaries. Nothing here computes or renders a
"worthiness," "engagement," or "loyalty" score.

## `inactivity_threshold`: read at the Discord layer, passed as a plain argument

Per Task 7's design decision (`Settings.activity_ledger_inactivity_threshold_days`
is a per-query parameter, deliberately never stored/seeded), `inactive` and
`activity` both accept `inactivity_threshold: timedelta` as a caller-supplied
argument rather than reading `Settings` themselves -- keeping this module
framework- and settings-independent, matching every other service in this
codebase. `krubit.discord.bot.FetchCommands` is the one place that reads
`Settings.activity_ledger_inactivity_threshold_days` and resolves it (with a
documented default fallback when unset) before calling either method.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from krubit.discord.content_commands import CommandResult, CommandStatus, _confirmation
from krubit.domain.activity_ledger import (
    CohortWindow,
    JoinEvent,
    LedgerEvent,
    ModerationReceiptEvent,
    cohort_window_days,
)
from krubit.domain.models import Card, CardField
from krubit.services.activation_retention import (
    cohort_membership,
    participation_trend,
    participation_trend_fetch_window_days,
    time_to_activation,
)
from krubit.services.activity_privacy import delete_member as delete_member_fn
from krubit.services.activity_views import community_pulse as _community_pulse_view
from krubit.services.activity_views import inactive_view, newcomer_view, returning_member_view
from krubit.services.milestones import recognition_candidates as recognition_candidates_fn

if TYPE_CHECKING:
    from krubit.storage.sqlite import SQLiteStore

# `/fetch member`/`/fetch activity` read at most this many of a member's most
# recent raw ledger rows -- matching `SQLiteStore.list_ledger_events`'s own
# "sized for interactive views" cap (as opposed to
# `list_all_ledger_events_for_member`'s uncapped export-only read).
_PROFILE_EVENT_LIMIT = 500

# `/fetch activity`'s participation-trend window. No command parameter exists
# for this in the design doc's command list, so it is a fixed, named constant
# (matching `activity_views._RETURNING_TREND_WINDOW`'s own precedent) rather
# than an unbounded, undocumented default.
_ACTIVITY_TREND_WINDOW = CohortWindow.THIRTY_DAY

# `/fetch newcomers`'s recent-join window. Likewise fixed and named: the design
# doc's `newcomer_view` signature takes `recent_window` as a caller-supplied
# parameter but no `/fetch newcomers` command parameter is specified, so a
# fixed 30-day lookback is used, matching `_ACTIVITY_TREND_WINDOW`'s own
# 30-day figure.
_NEWCOMER_RECENT_WINDOW = timedelta(days=30)

# `/fetch retention` reports both windows the design doc requires (7-day and
# 30-day), not just one.
_RETENTION_WINDOWS: tuple[CohortWindow, ...] = (CohortWindow.SEVEN_DAY, CohortWindow.THIRTY_DAY)

# `/fetch community-pulse`'s window. No command parameter exists for this
# either, so a fixed 30-day window is used, matching `_ACTIVITY_TREND_WINDOW`.
_COMMUNITY_PULSE_WINDOW = CohortWindow.THIRTY_DAY

# `/fetch recognition-candidates`'s window. No command parameter exists for
# this either, so a fixed 30-day window is used, matching
# `_ACTIVITY_TREND_WINDOW`/`_COMMUNITY_PULSE_WINDOW`.
_RECOGNITION_WINDOW = CohortWindow.THIRTY_DAY

# Every list-rendering `/fetch` command caps its rendered entries at this many
# lines and appends a "...and N more." summary line rather than risk exceeding
# Discord's 4096-character embed description limit for large guilds. This is
# new, deliberately defensive behavior -- no existing `/fetch` command guards
# this today.
_MAX_LIST_ENTRIES = 40


def _render_capped_lines(lines: list[str], total: int) -> str:
    if not lines:
        return "None found."
    if total > len(lines):
        lines = [*lines[:_MAX_LIST_ENTRIES], f"...and {total - _MAX_LIST_ENTRIES} more."]
    return "\n".join(lines)


def _trailing_window_events(
    events: tuple[LedgerEvent, ...], window_days: int, now: datetime
) -> tuple[LedgerEvent, ...]:
    """Pre-filter `events` to the trailing window ending at real `now`.

    Required by `participation_trend`'s caller contract: that function anchors
    its window to the latest event date it is given, so callers must supply a
    real trailing window ending at actual wall-clock `now` themselves. See
    `krubit.services.milestones._trailing_window_events`'s identical precedent.
    """
    window_start = now - timedelta(days=window_days - 1)
    return tuple(event for event in events if window_start <= event.occurred_at <= now)


@dataclass(frozen=True, slots=True)
class ActivityActorContext:
    """The plain facts an activity command needs about the member invoking it
    (or, for a `target` argument, the member being looked up). Deliberately
    framework-independent, matching `WatchdogActorContext`. `is_staff` is
    resolved by the Discord-layer command wrapper (Manage Guild or
    Administrator, matching every other staff-gated `/fetch` command) before
    this service is ever called."""

    guild_id: int
    member_id: int
    is_staff: bool = False


def _denied() -> CommandResult:
    return CommandResult(
        CommandStatus.DENIED, detail={"reason": "staff authority required"}
    )


def _denied_self_or_staff() -> CommandResult:
    return CommandResult(
        CommandStatus.DENIED,
        detail={"reason": "staff authority or self-view required"},
    )


class ActivityCommandService:
    """The framework-independent core of every `/fetch member|activity|
    newcomers|inactive|milestones|retention|community-pulse` command."""

    def __init__(
        self, store: SQLiteStore, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))

    # -- member: staff-only detailed profile -------------------------------------

    async def member(
        self, *, actor: ActivityActorContext, target: ActivityActorContext
    ) -> CommandResult:
        """Full ledger summary for one member: activation status, milestones
        reached, and moderation-receipt pointers (never the pointed-to
        incident's raw content -- see
        `krubit.domain.activity_ledger.ModerationReceiptEvent`'s docstring)."""
        if not actor.is_staff:
            return _denied()
        events = await self._store.list_ledger_events(
            actor.guild_id, member_id=target.member_id, limit=_PROFILE_EVENT_LIMIT
        )
        milestone_records = await self._store.list_milestones(
            actor.guild_id, member_id=target.member_id
        )
        joins = tuple(event for event in events if isinstance(event, JoinEvent))
        earliest_join = min(joins, key=lambda join: join.occurred_at) if joins else None
        activation = (
            time_to_activation(earliest_join.occurred_at, events)
            if earliest_join is not None
            else None
        )
        receipts = tuple(
            event for event in events if isinstance(event, ModerationReceiptEvent)
        )
        milestone_lines = ", ".join(
            f"{m.kind.value}:{m.detail}" for m in milestone_records
        ) or "None recorded"
        receipt_lines = ", ".join(r.receipt_id for r in receipts) or "None"
        card = Card(
            kind="fetched",
            title=f"Fetched: Member Profile <@{target.member_id}>",
            description=(
                f"Joined: "
                f"{earliest_join.occurred_at.isoformat() if earliest_join else 'Unknown'}\n"
                f"Activated: "
                f"{'Yes' if activation is not None and activation.activated else 'No'}\n\n"
                f"Milestones reached: {milestone_lines}\n\n"
                f"Moderation receipt pointers: {receipt_lines}"
            ),
            fields=(
                CardField("Milestone count", str(len(milestone_records)), True),
                CardField("Moderation receipt count", str(len(receipts)), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                "member_id": target.member_id,
                "milestone_count": len(milestone_records),
                "activated": activation is not None and activation.activated,
            },
        )

    # -- activity: staff-view for any member, reduced self-view for oneself ------

    async def activity(
        self,
        *,
        actor: ActivityActorContext,
        target: ActivityActorContext,
        inactivity_threshold: timedelta,
    ) -> CommandResult:
        """Participation-trend detail for `target`.

        `self_view` is computed from `target.member_id == actor.member_id`
        here, independent of anything a Discord-layer UI default may have
        already done -- a caller who is not staff and whose `target` does not
        equal their own ID is denied, full stop. The self-view render omits
        the staff-view's `Activated` field (derived by cross-referencing the
        member's earliest join event) so the self-view is a genuinely reduced
        subset, never merely the same data under a different title, per the
        design doc's "No comparison of one member's data to another's in the
        self-view" exclusion.
        """
        self_view = target.member_id == actor.member_id
        if not self_view and not actor.is_staff:
            return _denied_self_or_staff()
        effective_member_id = actor.member_id if self_view else target.member_id
        events = await self._store.list_ledger_events(
            actor.guild_id, member_id=effective_member_id, limit=_PROFILE_EVENT_LIMIT
        )
        now = self._now()
        # See `participation_trend_fetch_window_days`'s docstring: fetching only
        # `_ACTIVITY_TREND_WINDOW`'s 30 days makes `returning=True` structurally
        # unreachable whenever an operator sets `KRUBIT_ACTIVITY_LEDGER_
        # INACTIVITY_THRESHOLD_DAYS >= 30` -- a real, whole-branch-review finding.
        # `_ACTIVITY_TREND_WINDOW` (unwidened) is still what's passed to
        # `participation_trend` below.
        fetch_window_days = participation_trend_fetch_window_days(
            cohort_window_days(_ACTIVITY_TREND_WINDOW), inactivity_threshold
        )
        windowed = _trailing_window_events(events, fetch_window_days, now)
        trend = participation_trend(
            windowed, _ACTIVITY_TREND_WINDOW, inactivity_threshold=inactivity_threshold
        )
        fields = [
            CardField("Active days", str(trend.active_day_count), True),
            CardField("Channel diversity", str(trend.channel_diversity), True),
            CardField("Event diversity", str(trend.event_diversity), True),
            CardField("Returning", "Yes" if trend.returning else "No", True),
        ]
        if self_view:
            title = "Fetched: Your Activity"
            description = (
                "Your own participation trend over the trailing "
                f"{cohort_window_days(_ACTIVITY_TREND_WINDOW)} days. Nobody else's "
                "data is included."
            )
        else:
            joins = tuple(event for event in events if isinstance(event, JoinEvent))
            earliest_join = (
                min(joins, key=lambda join: join.occurred_at) if joins else None
            )
            activation = (
                time_to_activation(earliest_join.occurred_at, events)
                if earliest_join is not None
                else None
            )
            title = f"Fetched: Activity <@{effective_member_id}>"
            description = (
                f"Participation trend over the trailing "
                f"{cohort_window_days(_ACTIVITY_TREND_WINDOW)} days for "
                f"<@{effective_member_id}>."
            )
            fields.append(
                CardField(
                    "Activated",
                    "Yes" if activation is not None and activation.activated else "No",
                    True,
                )
            )
        card = Card(
            kind="fetched", title=title, description=description, fields=tuple(fields)
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"self_view": self_view, "returning": trend.returning},
        )

    # -- newcomers: staff-only guild-wide newcomer view ---------------------------

    async def newcomers(self, *, actor: ActivityActorContext) -> CommandResult:
        if not actor.is_staff:
            return _denied()
        now = self._now()
        entries = await newcomer_view(
            self._store, actor.guild_id, _NEWCOMER_RECENT_WINDOW, now
        )
        lines = "\n".join(
            f"<@{e.member_id}> — {'activated' if e.activated else 'not yet activated'} "
            f"(joined {e.joined_at.isoformat()})"
            for e in entries
        ) or "No newcomers in the lookback window."
        card = Card(
            kind="fetched",
            title="Fetched: Newcomers",
            description=lines,
            fields=(CardField("Count", str(len(entries)), True),),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED, card=card, detail={"count": len(entries)}
        )

    # -- inactive: staff-only guild-wide inactive-member view ---------------------

    async def inactive(
        self, *, actor: ActivityActorContext, inactivity_threshold: timedelta
    ) -> CommandResult:
        """Members with no meaningful action within `inactivity_threshold`.

        `inactivity_threshold` must be `krubit.discord.bot.FetchCommands`'s
        resolved `Settings.activity_ledger_inactivity_threshold_days` value --
        this is the one command Task 7's design explicitly calls out as the
        consumer of that setting (see the module docstring's
        "`inactivity_threshold`" section).
        """
        if not actor.is_staff:
            return _denied()
        now = self._now()
        entries = await inactive_view(
            self._store, actor.guild_id, inactivity_threshold, now
        )
        lines = "\n".join(
            f"<@{e.member_id}> — last active "
            f"{e.last_meaningful_action_at.isoformat() if e.last_meaningful_action_at else 'never'}"
            for e in entries
        ) or "No inactive members found."
        card = Card(
            kind="fetched",
            title="Fetched: Inactive Members",
            description=lines,
            fields=(
                CardField("Count", str(len(entries)), True),
                CardField("Threshold (days)", str(inactivity_threshold.days), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"count": len(entries), "threshold_days": inactivity_threshold.days},
        )

    # -- milestones: self-accessible for oneself, staff-only for another member --

    async def milestones(
        self, *, actor: ActivityActorContext, target: ActivityActorContext
    ) -> CommandResult:
        self_view = target.member_id == actor.member_id
        if not self_view and not actor.is_staff:
            return _denied_self_or_staff()
        effective_member_id = actor.member_id if self_view else target.member_id
        records = await self._store.list_milestones(
            actor.guild_id, member_id=effective_member_id
        )
        lines = "\n".join(
            f"{m.kind.value} — {m.detail} ({m.reached_at.isoformat()})" for m in records
        ) or "No milestones reached yet."
        title = (
            "Fetched: Your Milestones"
            if self_view
            else f"Fetched: Milestones <@{effective_member_id}>"
        )
        card = Card(
            kind="fetched",
            title=title,
            description=lines,
            fields=(CardField("Count", str(len(records)), True),),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"count": len(records), "self_view": self_view},
        )

    # -- retention: staff-only guild-wide cohort retention -------------------------

    async def retention(self, *, actor: ActivityActorContext) -> CommandResult:
        """Cohort retention for both the 7-day and 30-day windows the design
        doc requires, computed over every join event recorded for the guild
        (matching `activity_views.community_pulse`'s own semantics of
        measuring retention relative to each member's join date, not filtered
        to a display window)."""
        if not actor.is_staff:
            return _denied()
        all_events = await self._store.list_ledger_events_for_guild(actor.guild_id)
        joins = tuple(event for event in all_events if isinstance(event, JoinEvent))
        results = tuple(
            cohort_membership(joins, all_events, window) for window in _RETENTION_WINDOWS
        )
        lines = "\n".join(
            f"{r.window.value}: {r.retained_count}/{r.cohort_size} retained "
            f"({r.retention_rate:.0%})"
            for r in results
        ) or "No join cohorts recorded yet."
        card = Card(
            kind="fetched",
            title="Fetched: Cohort Retention",
            description=lines,
            fields=tuple(
                CardField(f"{r.window.value} retention", f"{r.retention_rate:.0%}", True)
                for r in results
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                f"{r.window.value}_retention_pct": round(r.retention_rate * 100)
                for r in results
            },
        )

    # -- community-pulse: staff-only guild-wide factual summary --------------------

    async def community_pulse(self, *, actor: ActivityActorContext) -> CommandResult:
        if not actor.is_staff:
            return _denied()
        now = self._now()
        pulse = await _community_pulse_view(
            self._store, actor.guild_id, _COMMUNITY_PULSE_WINDOW, now
        )
        card = Card(
            kind="fetched",
            title="Fetched: Community Pulse",
            description=(
                f"Active members (last "
                f"{cohort_window_days(_COMMUNITY_PULSE_WINDOW)} days): "
                f"{pulse.active_member_count}\n"
                f"Cohort retention: {pulse.cohort.retained_count}/"
                f"{pulse.cohort.cohort_size} ({pulse.cohort.retention_rate:.0%})"
            ),
            fields=(
                CardField("Channel contributions", str(pulse.channel_contribution_count), True),
                CardField("Event contributions", str(pulse.event_contribution_count), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                "active_member_count": pulse.active_member_count,
                "retention_pct": round(pulse.cohort.retention_rate * 100),
            },
        )

    # -- returning: staff-only guild-wide returning-member view -------------------

    async def returning(
        self, *, actor: ActivityActorContext, inactivity_threshold: timedelta
    ) -> CommandResult:
        """Members who had a gap exceeding `inactivity_threshold` and then
        resumed activity, per `returning_member_view`."""
        if not actor.is_staff:
            return _denied()
        now = self._now()
        entries = await returning_member_view(
            self._store, actor.guild_id, inactivity_threshold, now
        )
        lines = [
            f"<@{e.member_id}> — {e.trend.active_day_count} active days, "
            f"{e.trend.channel_diversity} channels (trailing "
            f"{cohort_window_days(e.trend.window)} days)"
            for e in entries[:_MAX_LIST_ENTRIES]
        ]
        description = _render_capped_lines(lines, len(entries))
        card = Card(
            kind="fetched",
            title="Fetched: Returning Members",
            description=description,
            fields=(CardField("Count", str(len(entries)), True),),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED, card=card, detail={"count": len(entries)}
        )

    # -- recognition-candidates: staff-only guild-wide recognition shortlist ------

    async def recognition_candidates(self, *, actor: ActivityActorContext) -> CommandResult:
        """A factual shortlist of members with notable, verifiable activity,
        per `recognition_candidates_fn` -- never a numeric score."""
        if not actor.is_staff:
            return _denied()
        now = self._now()
        events = await self._store.list_ledger_events_for_guild(actor.guild_id)
        candidates = recognition_candidates_fn(
            actor.guild_id, events, _RECOGNITION_WINDOW, now
        )
        lines = [
            f"<@{c.member_id}> — {', '.join(c.reasons)}"
            for c in candidates[:_MAX_LIST_ENTRIES]
        ]
        description = _render_capped_lines(lines, len(candidates))
        card = Card(
            kind="fetched",
            title="Fetched: Recognition Candidates",
            description=description,
            fields=(CardField("Count", str(len(candidates)), True),),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED, card=card, detail={"count": len(candidates)}
        )

    # -- member delete: staff-only, irreversible, two-call confirm ----------------

    async def delete_member(
        self,
        *,
        actor: ActivityActorContext,
        target: ActivityActorContext,
        confirm: bool = False,
    ) -> CommandResult:
        """Staff-triggered, irreversible deletion of one member's ledger data.

        Per the design spec's Privacy Controls section, deletion is staff-only --
        unlike `activity`/`milestones`, there is no self-view/self-delete path.
        """
        if not actor.is_staff:
            return _denied()
        if not confirm:
            card = _confirmation(
                title="Delete Member Data",
                description=(
                    f"Permanently delete all activity-ledger data for "
                    f"<@{target.member_id}>? This cannot be undone."
                ),
                Member=f"<@{target.member_id}>",
            )
            return CommandResult(
                CommandStatus.CONFIRMATION_REQUIRED,
                card=card,
                detail={"member_id": target.member_id},
            )
        now = self._now()
        receipt = await delete_member_fn(
            self._store,
            target.guild_id,
            target.member_id,
            requested_by=actor.member_id,
            now=now,
        )
        card = Card(
            kind="fetched",
            title="Fetched: Member Data Deleted",
            description=f"Deleted activity-ledger data for <@{target.member_id}>.",
            fields=(
                CardField("Receipt ID", receipt.receipt_id, True),
                CardField("Deleted At", receipt.created_at.isoformat(), True),
            ),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"receipt_id": receipt.receipt_id, "member_id": target.member_id},
        )
