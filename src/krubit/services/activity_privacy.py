"""Privacy controls for the activity ledger (Phase 4): retention sweep, member
deletion, and member data export.

See the design doc's "Privacy Controls" section (Retention window, Deletion,
Export) for the product requirements this module implements.

## Deletion receipt ordering is load-bearing

`SQLiteStore.delete_member_ledger_data` (Task 2) deletes every member-scoped row
across `ledger_events`, `milestones`, AND `activity_receipts` — including any prior
receipt that happens to reference this `member_id`, since `record_activity_receipt`
is a generic, caller-controlled method whose `detail` may hold genuine member-scoped
content. That means `delete_member` below must call `delete_member_ledger_data`
**first** and only write its own deletion receipt **afterward** — writing the
receipt before deletion would let deletion immediately erase it, defeating its
purpose as a *durable* record that deletion occurred.

## Deletion receipt content is deliberately minimal

The design doc is explicit: the deletion receipt records "that deletion occurred
(not what was deleted, to avoid the receipt itself becoming a retained copy of
otherwise-deleted data)". `delete_member`'s receipt `detail` therefore contains only
two facts — `requested_by` (who asked for the deletion) and `deleted_at` (when it
happened) — never a table list, row count, or summary of the deleted content.

## Retention sweep only ages out raw rows

Per the design doc: "Cohort/milestone aggregates already computed from pruned raw
rows are retained ... unless the guild's retention policy explicitly says
otherwise." `RetentionPolicy` carries no such override flag, so `RetentionSweepService`
only ever prunes `ledger_events` (via `SQLiteStore.prune_ledger_events_older_than`) —
`milestones` and `activity_receipts` are never touched by the sweep; only explicit
`delete_member` calls remove those.

## Sweep isolation mirrors `WatchdogRuntime.sweep_cycle`

`RetentionSweepService.sweep` isolates each prune target within one guild (currently
a single target, `ledger_events`, but structured as a list so a future target can be
added without weakening isolation); `sweep_all_guilds` isolates each guild from every
other guild. Both catch and log rather than propagate, exactly matching
`krubit.discord.watchdog_runtime.WatchdogRuntime.sweep_cycle`'s and Phase 2's
`ConnectorScheduler.run_cycle`'s discipline that one guild's or one job's failure
must never cancel or block any other guild's or job's work in the same cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from krubit.domain.activity_ledger import LedgerEvent, Milestone
from krubit.domain.models import JSONValue
from krubit.storage.sqlite import SQLiteStore

_logger = logging.getLogger(__name__)

_DELETION_ACTION = "member_data_deleted"

# Every activity-ledger (Phase 4) table `SQLiteStore._initialize` creates for this
# feature (see the schema block alongside `ledger_events`/`milestones`/
# `channel_exclusions`/`retention_policies`/`activity_receipts` in `sqlite.py`).
ACTIVITY_LEDGER_TABLES: tuple[str, ...] = (
    "ledger_events",
    "milestones",
    "channel_exclusions",
    "retention_policies",
    "activity_receipts",
)

# The subset of ACTIVITY_LEDGER_TABLES that carry a member_id column — i.e. exactly
# the tables `SQLiteStore.delete_member_ledger_data` must clear for deletion to be
# complete. This mirrors that method's own docstring enumeration. It is a plain
# constant (not itself schema-derived, since a table's presence in
# ACTIVITY_LEDGER_TABLES is still a human decision), but
# `tests/test_activity_privacy.py::test_all_member_scoped_tables_matches_live_schema`
# cross-checks it against a live `PRAGMA table_info` scan of every table in
# `ACTIVITY_LEDGER_TABLES`, so a future table added to that list without also
# updating this one fails loudly instead of silently drifting.
ALL_MEMBER_SCOPED_TABLES: tuple[str, ...] = (
    "ledger_events",
    "milestones",
    "activity_receipts",
)


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class ActivityReceipt:
    """A durable record that a privacy-relevant action occurred — never a record of
    what the action touched. See `delete_member`'s docstring for the exact minimal
    detail contract this module writes.
    """

    guild_id: int
    receipt_id: str
    member_id: int | None
    action: str
    detail: dict[str, JSONValue]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        if self.member_id is not None:
            _require_positive_id("member_id", self.member_id)
        if not self.action.strip():
            raise ValueError("action must not be blank")
        _require_aware("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class MemberExportPackage:
    """A member-scoped export: that member's own ledger events and milestones,
    never another member's data. Structurally enforced in `__post_init__` — a
    package cannot be constructed with an event or milestone belonging to a
    different `guild_id`/`member_id`.
    """

    guild_id: int
    member_id: int
    generated_at: datetime
    events: tuple[LedgerEvent, ...]
    milestones: tuple[Milestone, ...]

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("generated_at", self.generated_at)
        for event in self.events:
            if event.guild_id != self.guild_id or event.member_id != self.member_id:
                raise ValueError(
                    "events must all belong to this package's guild_id/member_id"
                )
        for milestone in self.milestones:
            if milestone.guild_id != self.guild_id or milestone.member_id != self.member_id:
                raise ValueError(
                    "milestones must all belong to this package's guild_id/member_id"
                )


async def delete_member(
    store: SQLiteStore,
    guild_id: int,
    member_id: int,
    *,
    requested_by: int,
    now: datetime,
) -> ActivityReceipt:
    """Delete every derived record for one member and record a minimal,
    content-free receipt that deletion occurred.

    See the module docstring's "Deletion receipt ordering is load-bearing" and
    "Deletion receipt content is deliberately minimal" sections — this function's
    two-step order (delete, then record) and its two-field `detail`
    (`requested_by`, `deleted_at`) are both deliberate, not incidental.
    """
    _require_positive_id("guild_id", guild_id)
    _require_positive_id("member_id", member_id)
    _require_positive_id("requested_by", requested_by)
    _require_aware("now", now)

    await store.delete_member_ledger_data(guild_id, member_id)

    receipt_id = str(uuid4())
    detail: dict[str, JSONValue] = {
        "requested_by": requested_by,
        "deleted_at": now.isoformat(),
    }
    await store.record_activity_receipt(
        guild_id=guild_id,
        receipt_id=receipt_id,
        member_id=member_id,
        action=_DELETION_ACTION,
        detail=detail,
        created_at=now,
    )
    return ActivityReceipt(
        guild_id=guild_id,
        receipt_id=receipt_id,
        member_id=member_id,
        action=_DELETION_ACTION,
        detail=detail,
        created_at=now,
    )


async def export_member_data(
    store: SQLiteStore, guild_id: int, member_id: int, now: datetime
) -> MemberExportPackage:
    """Return one member's own ledger events and milestones as a structured
    package — never another member's data, per the design doc's Export section.

    Uses `SQLiteStore.list_all_ledger_events_for_member` (uncapped) rather than
    `list_ledger_events` (capped at 500 rows for interactive views), since an
    export must be complete rather than silently truncated.
    """
    _require_positive_id("guild_id", guild_id)
    _require_positive_id("member_id", member_id)
    _require_aware("now", now)

    events = await store.list_all_ledger_events_for_member(guild_id, member_id=member_id)
    milestones = await store.list_milestones(guild_id, member_id=member_id)
    return MemberExportPackage(
        guild_id=guild_id,
        member_id=member_id,
        generated_at=now,
        events=events,
        milestones=milestones,
    )


class RetentionSweepService:
    """Prunes raw `ledger_events` rows older than each guild's configured
    retention window. See the module docstring's "Retention sweep only ages out
    raw rows" and "Sweep isolation mirrors `WatchdogRuntime.sweep_cycle`" sections.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def sweep(self, guild_id: int, now: datetime) -> int:
        """Prune one guild's aged-out raw ledger rows.

        Returns the number of rows removed. Returns 0 without touching storage if
        the guild has no configured `RetentionPolicy` (retention is opt-in, not a
        default cap). Each prune target is isolated: a failing target is logged and
        skipped rather than aborting the whole sweep, so a future second target
        cannot let one failure silently suppress pruning done by another.
        """
        _require_positive_id("guild_id", guild_id)
        _require_aware("now", now)

        policy = await self._store.get_retention_policy(guild_id)
        if policy is None:
            return 0
        cutoff = now - timedelta(days=policy.max_age_days)

        total = 0
        for target_name, prune in self._prune_targets(guild_id, cutoff):
            try:
                total += await prune()
            except Exception:
                _logger.exception(
                    "RetentionSweepService.sweep: %s prune failed for guild %s; "
                    "continuing with the next prune target",
                    target_name,
                    guild_id,
                )
        return total

    def _prune_targets(
        self, guild_id: int, cutoff: datetime
    ) -> tuple[tuple[str, Callable[[], Awaitable[int]]], ...]:
        return (
            (
                "ledger_events",
                lambda: self._store.prune_ledger_events_older_than(guild_id, cutoff),
            ),
        )

    async def sweep_all_guilds(self, guild_ids: Iterable[int], now: datetime) -> None:
        """Sweep every guild in `guild_ids`, isolating each guild's failure from
        every other guild's — mirroring `WatchdogRuntime.sweep_cycle`'s per-guild
        isolation discipline exactly.
        """
        _require_aware("now", now)
        for guild_id in guild_ids:
            try:
                await self.sweep(guild_id, now)
            except Exception:
                _logger.exception(
                    "RetentionSweepService.sweep_all_guilds: sweep failed for guild %s; "
                    "continuing with the next guild",
                    guild_id,
                )
