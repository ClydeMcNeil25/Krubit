"""Live gateway wiring for Phase 4 Activity Ledger: the one place every Task 1-6
extraction/ingestion/privacy service becomes a live Discord behavior.

Matches `krubit.discord.watchdog_runtime.WatchdogRuntime`'s own division of labor
exactly: every actual extraction rule still lives in `krubit.discord.activity_events`,
every storage write still goes through `krubit.services.activity_ingestion.
ActivityIngestionService` (the sole pre-storage exclusion gate), and every retention
decision still lives in `krubit.services.activity_privacy.RetentionSweepService`. This
module only decides *when* to call them and how to adapt discord.py's actual gateway
callback shapes into what those pure functions/services expect.

## `activity_ledger_enabled`: checked at the real boundary, not just at construction

Learned from Phase 2's `ContentRuntime.apply_plan` and Phase 3's `WatchdogRuntime`
final-review findings (a settings flag parsed but not enforced at the real action
boundary): every public method on `ActivityRuntime` checks `activity_ledger_enabled`
as its first statement, mirroring `WatchdogRuntime`'s pattern exactly. A caller that
constructs this class with the flag off gets a runtime whose every method is a
genuine no-op -- no extraction, no storage read, no storage write, no Discord API
call.

## The voice-session tracking cache (read before changing `on_voice_state_update`)

`extract_voice_session_event(before, after, now)` (Task 4) takes two independent
snapshots and trusts the caller paired them correctly -- it performs no state
tracking of its own. Real discord.py delivers `on_voice_state_update(member, before,
after)` as ONE event per state change (join, leave, move, or a mute/deafen-only
change with no channel change at all), never a pre-paired join+leave. This module
therefore keeps a small in-memory `{(guild_id, member_id): _VoiceJoinSnapshot}`
cache -- populated when a member's `after.channel` differs from `before.channel` and
is not `None` (a join, or the "join" half of a channel move), and consulted (then
cleared) when `before.channel` differs from `after.channel` and `before.channel` is
not `None` (a leave, or the "leave" half of a channel move). A member who leaves
without a tracked join (a bot restart lost the cache, or the join happened before
this process started) is handled gracefully: no cached snapshot means no session is
fabricated, and nothing is ingested -- matching `extract_voice_session_event`'s own
"never fabricate a session out of contradictory snapshots" contract. Stale entries
(a join whose leave never arrives, e.g. a lost gateway event) are pruned by age on
every call, matching the in-memory-cache precedent Phase 3's `SpamWaveDetector`/
`WebhookAbuseDetector` already established -- no new durable table.

## The attendance two-callback bridge (read before changing `on_scheduled_event_user_*`)

Discord delivers Scheduled Event RSVP changes as two separate gateway callbacks,
`on_scheduled_event_user_add`/`on_scheduled_event_user_remove`, each carrying a
`discord.ScheduledEvent` and a `discord.User`/`discord.Member` -- never a single
combined payload. `extract_attendance_event` (Task 4) expects one already-combined
`AttendancePayloadSubject` (event IDs plus which action fired). `on_scheduled_event_
user_add` and `on_scheduled_event_user_remove` are this bridge: each constructs the
same `_AttendancePayload` shape from its own two Discord-supplied arguments, differing
only in the `AttendanceAction` each is hard-wired to, then both funnel through one
shared private helper before calling `extract_attendance_event`.

## Reaction removal is a genuine no-op, not a missing feature

The domain model records that a reaction was *added* (`ReactionEvent`) -- there is no
"reaction removed" ledger event kind, matching the design doc's "factual participation
events" scope (removing a reaction is not itself a new participation act).
`on_reaction_remove` therefore exists (and still checks `activity_ledger_enabled`
first, like every other method here) purely to give `KrubitBot.on_raw_reaction_remove`
a safe, symmetric call site -- it never calls `extract_reaction_event` or `ingest`.

## `on_member_join`/`on_member_remove`/`on_member_update`: wiring already-defined kinds

`LedgerEventKind.JOIN` and `LedgerEventKind.ROLE_CHANGE` (Task 1) have no dedicated
`krubit.discord.activity_events` extraction function -- Task 4 only built extraction
for message/reaction/voice/attendance. Since this task is explicitly "the integration
task that wires everything built in Tasks 1-6 into a live Discord runtime," and
`JoinEvent`/`RoleChangeEvent` are both already-validated domain value objects with
nothing else in the codebase constructing them, `on_member_join` builds and ingests a
`JoinEvent` directly, and `on_member_update` diffs `before.roles`/`after.roles` (the
same before/after role-ID-set diff `KrubitBot._ingest_change`'s own
`member_roles_updated` path already performs) and ingests one `RoleChangeEvent` per
added/removed role. `on_member_remove` ingests no ledger event at all (no "member
left" kind exists in the domain model); it only purges that member's voice-join cache
entry, since Discord does not reliably deliver a clean voice-state-update leave event
for every member-leaves-guild-while-connected-to-voice case.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from krubit.discord.activity_events import (
    MessageSubject,
    ReactionPayloadSubject,
    extract_attendance_event,
    extract_message_event,
    extract_reaction_event,
    extract_voice_session_event,
)
from krubit.domain.activity_ledger import (
    AttendanceAction,
    JoinEvent,
    RetentionPolicy,
    RoleChangeAction,
    RoleChangeEvent,
)
from krubit.services.activity_ingestion import ActivityIngestionService
from krubit.services.activity_privacy import RetentionSweepService
from krubit.storage.sqlite import SQLiteStore

_logger = logging.getLogger(__name__)

GuildIds = Callable[[], Sequence[int]]

# How long a tracked voice join is kept without a matching leave before it is
# discarded as stale. Generous relative to any real voice session, but bounded so a
# lost leave event (a missed gateway message, a crashed process) cannot grow this
# in-memory cache without limit.
_STALE_VOICE_JOIN_AGE = timedelta(hours=24)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class _GuildIdentified(Protocol):
    @property
    def id(self) -> int: ...


class _MemberSubject(Protocol):
    """The minimal shape `on_member_join`/`on_member_remove` need from a member."""

    @property
    def id(self) -> int: ...
    @property
    def guild(self) -> _GuildIdentified: ...


class _RoleSubject(Protocol):
    @property
    def id(self) -> int: ...


class _MemberWithRolesSubject(Protocol):
    """The minimal shape `on_member_update` needs from a before/after member pair."""

    @property
    def id(self) -> int: ...
    @property
    def guild(self) -> _GuildIdentified: ...
    @property
    def roles(self) -> Sequence[_RoleSubject]: ...


class _VoiceChannelSubject(Protocol):
    @property
    def id(self) -> int: ...


class _VoiceStateSubject(Protocol):
    """The minimal shape `on_voice_state_update` needs from `discord.VoiceState`."""

    @property
    def channel(self) -> _VoiceChannelSubject | None: ...


class _ScheduledEventSubject(Protocol):
    """The minimal shape both attendance callbacks need from `discord.ScheduledEvent`."""

    @property
    def id(self) -> int: ...
    @property
    def guild_id(self) -> int | None: ...


class _RSVPUserSubject(Protocol):
    """The minimal shape both attendance callbacks need from the RSVPing user."""

    @property
    def id(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _VoiceJoinSnapshot:
    """One member's voice-channel presence at a single instant.

    Structurally satisfies `krubit.discord.activity_events.VoiceStateSubject` --
    see this module's docstring's voice-tracking-cache section for how a pair of
    these (one at join time, one at leave time) becomes `extract_voice_session_
    event`'s `before`/`after` arguments.
    """

    guild_id: int
    member_id: int
    channel_id: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class _AttendancePayload:
    """The single combined payload shape `extract_attendance_event` expects,
    assembled from either attendance callback's own two Discord-supplied arguments.
    Structurally satisfies `krubit.discord.activity_events.AttendancePayloadSubject`.
    """

    guild_id: int | None
    user_id: int
    scheduled_event_id: int
    action: AttendanceAction


class ActivityRuntime:
    """Wire Task 1-6's activity-ledger extraction/ingestion/privacy services into
    live Discord gateway events.

    ## `default_retention_days`: seeding, not overriding

    `Settings.activity_ledger_retention_days` (`KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS`)
    is genuinely enforced here, not merely parsed-and-ignored: `sweep_cycle` seeds a
    guild's default `RetentionPolicy` the first time it finds none configured for that
    guild. This is deliberately a *seed*, never an override -- `RetentionSweepService.
    sweep` already treats "no policy" as "retention is opt-in, not a default cap" (see
    that method's own docstring), and a guild that already has a policy (staff-
    configured, or seeded by an earlier sweep) is never touched again by this path.
    Unlike `activity_ledger_excluded_channel_ids` (which this runtime deliberately
    does NOT auto-apply -- see `ActivityRuntime`'s module-level design notes in the
    Task 7 report: an `ExclusionEntry` carries a staff-set `reason` a blind reseed
    could clobber), `RetentionPolicy` carries no such staff-authored field to lose --
    only `max_age_days`/`updated_by`/`updated_at`, none of which this seed touches
    again once a policy row exists at all. `default_retention_policy_owner_id` is the
    `updated_by` attribution for a seeded row (the running application's own ID, not a
    real staff member, since no staff member configured it).
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        activity_ledger_enabled: bool,
        guild_ids: GuildIds,
        ingestion: ActivityIngestionService | None = None,
        retention_sweep: RetentionSweepService | None = None,
        default_retention_days: int | None = None,
        default_retention_policy_owner_id: int | None = None,
    ) -> None:
        if default_retention_days is not None and default_retention_policy_owner_id is None:
            raise ValueError(
                "default_retention_policy_owner_id is required when "
                "default_retention_days is set"
            )
        self._store = store
        self._activity_ledger_enabled = activity_ledger_enabled
        self._guild_ids = guild_ids
        self._ingestion = ingestion or ActivityIngestionService(store)
        self._retention_sweep = retention_sweep or RetentionSweepService(store)
        self._default_retention_days = default_retention_days
        self._default_retention_policy_owner_id = default_retention_policy_owner_id
        # See the module docstring's "voice-session tracking cache" section. Never
        # persisted -- a process restart loses in-flight joins gracefully (the
        # matching leave finds no cached snapshot and is skipped rather than
        # fabricating a session), matching Phase 3's in-memory-cache precedent.
        self._voice_joins: dict[tuple[int, int], _VoiceJoinSnapshot] = {}

    # -- messages / reactions --------------------------------------------------

    async def on_message(self, message: MessageSubject, now: datetime) -> None:
        """Ingest one guild message as a `MessageEvent`. A no-op for a DM (handled
        by `extract_message_event` itself) or a bot-authored message (checked here,
        matching `WatchdogRuntime.on_message`'s own `getattr(author, "bot", False)`
        idiom -- a bot's own posts, including Krubit's, are never member
        participation).
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        if getattr(message.author, "bot", False):
            return
        event = extract_message_event(message, now)
        if event is not None:
            await self._ingestion.ingest(event)

    async def on_reaction_add(self, payload: ReactionPayloadSubject, now: datetime) -> None:
        """Ingest one reaction add as a `ReactionEvent`. A no-op for a DM reaction
        (handled by `extract_reaction_event` itself).
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        event = extract_reaction_event(payload, now)
        if event is not None:
            await self._ingestion.ingest(event)

    async def on_reaction_remove(self, payload: ReactionPayloadSubject, now: datetime) -> None:
        """A gated no-op -- see the module docstring's "Reaction removal is a
        genuine no-op" section for why no ledger event kind exists for this.
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        return

    # -- voice --------------------------------------------------------------------

    async def on_voice_state_update(
        self,
        member: _MemberSubject,
        before: _VoiceStateSubject,
        after: _VoiceStateSubject,
        now: datetime,
    ) -> None:
        """Bridge one raw voice-state transition into a completed `VoiceSessionEvent`
        once a matching join and leave are both known. See the module docstring's
        "voice-session tracking cache" section for the full join/leave/move design.
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)

        before_channel_id = before.channel.id if before.channel is not None else None
        after_channel_id = after.channel.id if after.channel is not None else None
        if before_channel_id == after_channel_id:
            # A mute/deafen/self-video-only change: no channel change at all.
            return

        guild_id = member.guild.id
        member_id = member.id
        key = (guild_id, member_id)

        if before_channel_id is not None:
            await self._close_voice_session(key, guild_id, member_id, before_channel_id, now)

        if after_channel_id is not None:
            self._voice_joins[key] = _VoiceJoinSnapshot(guild_id, member_id, after_channel_id, now)

        self._prune_stale_voice_joins(now)

    async def _close_voice_session(
        self,
        key: tuple[int, int],
        guild_id: int,
        member_id: int,
        channel_id: int,
        now: datetime,
    ) -> None:
        snapshot = self._voice_joins.pop(key, None)
        if snapshot is None or snapshot.channel_id != channel_id:
            # No tracked join for this member/channel -- a restart lost the cache,
            # or the join predates this process. Gracefully skip rather than
            # fabricate a session; see `extract_voice_session_event`'s own contract.
            return
        leave_snapshot = _VoiceJoinSnapshot(guild_id, member_id, channel_id, now)
        event = extract_voice_session_event(snapshot, leave_snapshot, now)
        if event is not None:
            await self._ingestion.ingest(event)

    def _prune_stale_voice_joins(self, now: datetime) -> None:
        stale_keys = [
            key
            for key, snapshot in self._voice_joins.items()
            if now - snapshot.occurred_at > _STALE_VOICE_JOIN_AGE
        ]
        for key in stale_keys:
            del self._voice_joins[key]

    # -- scheduled event attendance ------------------------------------------------

    async def on_scheduled_event_user_add(
        self, event: _ScheduledEventSubject, user: _RSVPUserSubject, now: datetime
    ) -> None:
        await self._handle_attendance(event, user, AttendanceAction.ADD, now)

    async def on_scheduled_event_user_remove(
        self, event: _ScheduledEventSubject, user: _RSVPUserSubject, now: datetime
    ) -> None:
        await self._handle_attendance(event, user, AttendanceAction.REMOVE, now)

    async def _handle_attendance(
        self,
        event: _ScheduledEventSubject,
        user: _RSVPUserSubject,
        action: AttendanceAction,
        now: datetime,
    ) -> None:
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        payload = _AttendancePayload(
            guild_id=event.guild_id,
            user_id=user.id,
            scheduled_event_id=event.id,
            action=action,
        )
        extracted = extract_attendance_event(payload, now)
        if extracted is not None:
            await self._ingestion.ingest(extracted)

    # -- membership / roles ---------------------------------------------------------

    async def on_member_join(self, member: _MemberSubject, now: datetime) -> None:
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        await self._ingestion.ingest(
            JoinEvent(guild_id=member.guild.id, member_id=member.id, occurred_at=now)
        )

    async def on_member_remove(self, member: _MemberSubject, now: datetime) -> None:
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        self._voice_joins.pop((member.guild.id, member.id), None)

    async def on_member_update(
        self, before: _MemberWithRolesSubject, after: _MemberWithRolesSubject, now: datetime
    ) -> None:
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        if before_role_ids == after_role_ids:
            return
        guild_id = after.guild.id
        member_id = after.id
        for role_id in sorted(after_role_ids - before_role_ids):
            await self._ingestion.ingest(
                RoleChangeEvent(
                    guild_id=guild_id,
                    member_id=member_id,
                    occurred_at=now,
                    role_id=role_id,
                    action=RoleChangeAction.GRANTED,
                )
            )
        for role_id in sorted(before_role_ids - after_role_ids):
            await self._ingestion.ingest(
                RoleChangeEvent(
                    guild_id=guild_id,
                    member_id=member_id,
                    occurred_at=now,
                    role_id=role_id,
                    action=RoleChangeAction.REMOVED,
                )
            )

    # -- retention sweep --------------------------------------------------------------

    async def sweep_cycle(self, now: datetime) -> None:
        """Prune stale voice-join cache entries and run the retention sweep for every
        guild, isolating one guild's failure from another's -- mirroring
        `WatchdogRuntime.sweep_cycle`'s and `RetentionSweepService.sweep_all_guilds`'s
        own per-guild isolation discipline.
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        self._prune_stale_voice_joins(now)
        for guild_id in self._guild_ids():
            try:
                await self._seed_default_retention_policy(guild_id, now)
                await self._retention_sweep.sweep(guild_id, now)
            except Exception:
                _logger.exception(
                    "ActivityRuntime.sweep_cycle: sweep failed for guild %s; "
                    "continuing with the next guild",
                    guild_id,
                )

    async def _seed_default_retention_policy(self, guild_id: int, now: datetime) -> None:
        """Seed `guild_id`'s default `RetentionPolicy` from `Settings.
        activity_ledger_retention_days` the first time none is configured. See the
        class docstring's "`default_retention_days`: seeding, not overriding" section
        -- this never touches a guild that already has a policy, staff-configured or
        previously seeded.
        """
        if self._default_retention_days is None:
            return
        existing = await self._store.get_retention_policy(guild_id)
        if existing is not None:
            return
        owner_id = self._default_retention_policy_owner_id
        assert owner_id is not None  # enforced together in __init__
        await self._store.save_retention_policy(
            RetentionPolicy(
                guild_id=guild_id,
                max_age_days=self._default_retention_days,
                updated_by=owner_id,
                updated_at=now,
            )
        )
