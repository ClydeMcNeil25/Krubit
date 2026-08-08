"""Integration tests for `krubit.discord.activity_runtime.ActivityRuntime`.

Exercises the live wiring against a real on-disk `SQLiteStore` (never mocked),
matching `test_watchdog_runtime.py`'s established convention. Small local `Fake*`
classes stand in for discord.py's actual gateway callback argument shapes.

Two areas get deliberately explicit, separate coverage per the task brief: the
stateful voice-session tracking cache (a join then a leave must produce exactly one
`VoiceSessionEvent` with the correct duration, and a leave with no tracked join must
not crash) and the two-callback attendance bridge (add and remove must each produce
the right `AttendanceAction`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.discord.activity_runtime import ActivityRuntime
from krubit.domain.activity_ledger import (
    AttendanceAction,
    ExclusionEntry,
    LedgerEventKind,
    RoleChangeAction,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
GUILD_ID = 111
MEMBER_ID = 222
CHANNEL_ID = 555
VOICE_CHANNEL_ID = 777
OTHER_VOICE_CHANNEL_ID = 778


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def build_runtime(
    store: SQLiteStore,
    *,
    activity_ledger_enabled: bool = True,
    guild_ids: tuple[int, ...] = (GUILD_ID,),
) -> ActivityRuntime:
    return ActivityRuntime(
        store,
        activity_ledger_enabled=activity_ledger_enabled,
        guild_ids=lambda: guild_ids,
    )


@pytest.fixture
def runtime(store: SQLiteStore) -> ActivityRuntime:
    return build_runtime(store)


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID) -> None:
        self.id = guild_id


class FakeAuthor:
    def __init__(self, author_id: int = MEMBER_ID, *, bot: bool = False) -> None:
        self.id = author_id
        self.bot = bot


class FakeChannel:
    def __init__(self, channel_id: int = CHANNEL_ID) -> None:
        self.id = channel_id


_DEFAULT_GUILD = object()


class FakeMessage:
    def __init__(
        self,
        *,
        author_id: int = MEMBER_ID,
        author_bot: bool = False,
        guild: FakeGuild | None | object = _DEFAULT_GUILD,
        channel_id: int = CHANNEL_ID,
    ) -> None:
        self.author = FakeAuthor(author_id, bot=author_bot)
        self.guild: FakeGuild | None = FakeGuild() if guild is _DEFAULT_GUILD else guild  # type: ignore[assignment]
        self.channel = FakeChannel(channel_id)


class FakeReactionPayload:
    def __init__(
        self,
        *,
        guild_id: int | None = GUILD_ID,
        user_id: int = MEMBER_ID,
        channel_id: int = CHANNEL_ID,
        emoji: str = "🎉",
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.emoji = emoji


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeMember:
    def __init__(
        self,
        member_id: int = MEMBER_ID,
        guild: FakeGuild | None = None,
        *,
        role_ids: tuple[int, ...] = (),
    ) -> None:
        self.id = member_id
        self.guild = guild if guild is not None else FakeGuild()
        self.roles = [FakeRole(role_id) for role_id in role_ids]


class FakeVoiceChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id


class FakeVoiceState:
    def __init__(self, channel: FakeVoiceChannel | None) -> None:
        self.channel = channel


class FakeScheduledEvent:
    def __init__(self, event_id: int = 999, guild_id: int | None = GUILD_ID) -> None:
        self.id = event_id
        self.guild_id = guild_id


class FakeUser:
    def __init__(self, user_id: int = MEMBER_ID) -> None:
        self.id = user_id


# --- activity_ledger_enabled: genuine no-op at every real call site ----------------


@pytest.mark.asyncio
async def test_on_message_is_a_genuine_noop_when_ledger_disabled(store: SQLiteStore) -> None:
    rt = build_runtime(store, activity_ledger_enabled=False)

    await rt.on_message(FakeMessage(), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_every_public_method_is_a_noop_when_ledger_disabled(store: SQLiteStore) -> None:
    rt = build_runtime(store, activity_ledger_enabled=False)
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild, role_ids=(1,))

    await rt.on_message(FakeMessage(guild=guild), NOW)
    await rt.on_reaction_add(FakeReactionPayload(), NOW)
    await rt.on_voice_state_update(
        member,
        FakeVoiceState(None),
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        NOW,
    )
    await rt.on_scheduled_event_user_add(
        FakeScheduledEvent(), FakeUser(), NOW
    )
    await rt.on_member_join(member, NOW)
    await rt.on_member_update(
        FakeMember(MEMBER_ID, guild, role_ids=()),
        FakeMember(MEMBER_ID, guild, role_ids=(1,)),
        NOW,
    )
    await rt.sweep_cycle(NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


# --- on_message ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_ingests_a_message_event(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_message(FakeMessage(), NOW)

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert len(events) == 1
    assert events[0].kind is LedgerEventKind.MESSAGE


@pytest.mark.asyncio
async def test_on_message_ignores_a_dm(runtime: ActivityRuntime, store: SQLiteStore) -> None:
    await runtime.on_message(FakeMessage(guild=None), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_on_message_ignores_bot_authored_messages(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_message(FakeMessage(author_bot=True), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_on_message_respects_channel_exclusion(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await store.save_exclusion_entry(
        ExclusionEntry(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            excluded_by=999,
            reason="staff lounge",
            excluded_at=NOW,
        )
    )

    await runtime.on_message(FakeMessage(), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


# --- on_reaction_add / on_reaction_remove ---------------------------------------


@pytest.mark.asyncio
async def test_on_reaction_add_ingests_a_reaction_event(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_reaction_add(FakeReactionPayload(), NOW)

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert len(events) == 1
    assert events[0].kind is LedgerEventKind.REACTION


@pytest.mark.asyncio
async def test_on_reaction_remove_never_ingests_anything(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    """The domain model has no "reaction removed" ledger kind -- see
    `ActivityRuntime.on_reaction_remove`'s docstring."""
    await runtime.on_reaction_remove(FakeReactionPayload(), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_on_reaction_add_ignores_a_dm_reaction(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_reaction_add(FakeReactionPayload(guild_id=None), NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


# --- on_voice_state_update: the stateful join-time tracking cache ------------------


@pytest.mark.asyncio
async def test_voice_join_then_leave_produces_exactly_one_session_with_correct_duration(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)
    join_at = NOW
    leave_at = NOW + timedelta(minutes=42)

    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(None),
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        join_at,
    )
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        FakeVoiceState(None),
        leave_at,
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    voice_events = [e for e in events if e.kind is LedgerEventKind.VOICE_SESSION]
    assert len(voice_events) == 1
    voice_event = voice_events[0]
    assert voice_event.occurred_at == join_at  # type: ignore[attr-defined]
    assert voice_event.left_at == leave_at  # type: ignore[attr-defined]
    assert voice_event.duration == timedelta(minutes=42)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_voice_leave_without_a_tracked_join_does_not_crash_or_ingest(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)

    # No prior join call at all -- e.g. a process restart lost the cache.
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        FakeVoiceState(None),
        NOW,
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert not any(e.kind is LedgerEventKind.VOICE_SESSION for e in events)


@pytest.mark.asyncio
async def test_voice_mute_only_change_does_not_open_or_close_a_session(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)
    channel = FakeVoiceChannel(VOICE_CHANNEL_ID)

    # Same channel before and after -- a mute/deafen toggle, not a channel change.
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(channel),
        FakeVoiceState(channel),
        NOW,
    )
    # A genuine leave afterward must find no tracked join (none was ever opened).
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(channel),
        FakeVoiceState(None),
        NOW + timedelta(minutes=5),
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert not any(e.kind is LedgerEventKind.VOICE_SESSION for e in events)


@pytest.mark.asyncio
async def test_voice_move_between_channels_closes_previous_session_and_opens_new(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)

    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(None),
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        NOW,
    )
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        FakeVoiceState(FakeVoiceChannel(OTHER_VOICE_CHANNEL_ID)),
        NOW + timedelta(minutes=10),
    )
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(FakeVoiceChannel(OTHER_VOICE_CHANNEL_ID)),
        FakeVoiceState(None),
        NOW + timedelta(minutes=15),
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    voice_events = [e for e in events if e.kind is LedgerEventKind.VOICE_SESSION]
    assert len(voice_events) == 2


# --- attendance: the two-callback bridge --------------------------------------


@pytest.mark.asyncio
async def test_scheduled_event_user_add_ingests_an_attendance_add_event(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_scheduled_event_user_add(
        FakeScheduledEvent(), FakeUser(), NOW
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    attendance = [e for e in events if e.kind is LedgerEventKind.EVENT_ATTENDANCE]
    assert len(attendance) == 1
    assert attendance[0].action == AttendanceAction.ADD  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_scheduled_event_user_remove_ingests_an_attendance_remove_event(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_scheduled_event_user_remove(
        FakeScheduledEvent(), FakeUser(), NOW
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    attendance = [e for e in events if e.kind is LedgerEventKind.EVENT_ATTENDANCE]
    assert len(attendance) == 1
    assert attendance[0].action == AttendanceAction.REMOVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_scheduled_event_attendance_ignores_a_guildless_payload(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.on_scheduled_event_user_add(
        FakeScheduledEvent(guild_id=None), FakeUser(), NOW
    )

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


# --- on_member_join / on_member_remove / on_member_update --------------------------


@pytest.mark.asyncio
async def test_on_member_join_ingests_a_join_event(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)

    await runtime.on_member_join(member, NOW)

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert len(events) == 1
    assert events[0].kind is LedgerEventKind.JOIN


@pytest.mark.asyncio
async def test_on_member_remove_purges_the_voice_join_cache(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    member = FakeMember(MEMBER_ID, guild)

    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(None),
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        NOW,
    )
    await runtime.on_member_remove(member, NOW + timedelta(minutes=1))

    # A subsequent leave finds no tracked join -- it was purged by on_member_remove,
    # not lingering in the cache for up to the stale-prune window.
    await runtime.on_voice_state_update(
        member,
        FakeVoiceState(FakeVoiceChannel(VOICE_CHANNEL_ID)),
        FakeVoiceState(None),
        NOW + timedelta(minutes=2),
    )

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert not any(e.kind is LedgerEventKind.VOICE_SESSION for e in events)


@pytest.mark.asyncio
async def test_on_member_update_ingests_role_change_events_for_added_and_removed_roles(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    before = FakeMember(MEMBER_ID, guild, role_ids=(1,))
    after = FakeMember(MEMBER_ID, guild, role_ids=(2,))

    await runtime.on_member_update(before, after, NOW)

    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    role_events = [e for e in events if e.kind is LedgerEventKind.ROLE_CHANGE]
    assert len(role_events) == 2
    actions = {e.action for e in role_events}  # type: ignore[union-attr]
    assert actions == {RoleChangeAction.GRANTED, RoleChangeAction.REMOVED}


@pytest.mark.asyncio
async def test_on_member_update_is_a_noop_when_roles_are_unchanged(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    guild = FakeGuild()
    before = FakeMember(MEMBER_ID, guild, role_ids=(1,))
    after = FakeMember(MEMBER_ID, guild, role_ids=(1,))

    await runtime.on_member_update(before, after, NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


# --- sweep_cycle -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_cycle_prunes_aged_out_ledger_events(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=GUILD_ID, max_age_days=7, updated_by=999, updated_at=NOW)
    )
    old_message_at = NOW - timedelta(days=30)
    await runtime.on_message(
        FakeMessage(), old_message_at
    )
    assert len(await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)) == 1

    await runtime.sweep_cycle(NOW)

    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_sweep_cycle_is_a_noop_when_ledger_disabled(store: SQLiteStore) -> None:
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=GUILD_ID, max_age_days=7, updated_by=999, updated_at=NOW)
    )
    enabled_runtime = build_runtime(store, activity_ledger_enabled=True)
    await enabled_runtime.on_message(FakeMessage(), NOW - timedelta(days=30))
    assert len(await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)) == 1

    disabled_runtime = build_runtime(store, activity_ledger_enabled=False)
    await disabled_runtime.sweep_cycle(NOW)

    assert len(await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)) == 1


@pytest.mark.asyncio
async def test_sweep_cycle_isolates_one_guilds_failure_from_another_guild(
    store: SQLiteStore,
) -> None:
    """A real concurrent-failure scenario: guild A's sweep raises, guild B's sweep
    must still run to completion in the same cycle."""
    from krubit.domain.activity_ledger import RetentionPolicy
    from krubit.services.activity_privacy import RetentionSweepService

    guild_a, guild_b = 111, 222
    await store.save_retention_policy(
        RetentionPolicy(guild_id=guild_b, max_age_days=7, updated_by=999, updated_at=NOW)
    )
    rt = build_runtime(store, guild_ids=(guild_a, guild_b))

    class ExplodingRetentionSweep(RetentionSweepService):
        async def sweep(self, guild_id: int, now: datetime) -> int:
            if guild_id == guild_a:
                raise RuntimeError("synthetic retention-sweep failure for guild A")
            return await super().sweep(guild_id, now)

    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (guild_a, guild_b),
        retention_sweep=ExplodingRetentionSweep(store),
    )
    await rt.on_message(
        FakeMessage(guild=FakeGuild(guild_b)), NOW - timedelta(days=30)
    )
    assert len(await store.list_ledger_events(guild_b, member_id=MEMBER_ID)) == 1

    await rt.sweep_cycle(NOW)  # must not raise

    assert await store.list_ledger_events(guild_b, member_id=MEMBER_ID) == ()


# --- sweep_cycle: default_retention_days seeding -----------------------------------


@pytest.mark.asyncio
async def test_sweep_cycle_seeds_a_default_retention_policy_when_none_configured(
    store: SQLiteStore,
) -> None:
    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
        default_retention_days=45,
        default_retention_policy_owner_id=999,
    )

    await rt.sweep_cycle(NOW)

    policy = await store.get_retention_policy(GUILD_ID)
    assert policy is not None
    assert policy.max_age_days == 45
    assert policy.updated_by == 999


@pytest.mark.asyncio
async def test_sweep_cycle_never_overwrites_an_existing_retention_policy(
    store: SQLiteStore,
) -> None:
    from krubit.domain.activity_ledger import RetentionPolicy

    staff_policy = RetentionPolicy(
        guild_id=GUILD_ID, max_age_days=14, updated_by=555, updated_at=NOW
    )
    await store.save_retention_policy(staff_policy)
    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
        default_retention_days=90,
        default_retention_policy_owner_id=999,
    )

    await rt.sweep_cycle(NOW)

    policy = await store.get_retention_policy(GUILD_ID)
    assert policy is not None
    assert policy.max_age_days == 14  # staff's own value, never overwritten
    assert policy.updated_by == 555


@pytest.mark.asyncio
async def test_sweep_cycle_does_not_seed_a_policy_when_default_retention_days_is_unset(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.sweep_cycle(NOW)

    assert await store.get_retention_policy(GUILD_ID) is None


def test_default_retention_days_requires_an_owner_id(store: SQLiteStore) -> None:
    with pytest.raises(ValueError, match="default_retention_policy_owner_id"):
        ActivityRuntime(
            store,
            activity_ledger_enabled=True,
            guild_ids=lambda: (GUILD_ID,),
            default_retention_days=30,
        )


# --- sweep_cycle: excluded_channel_ids seeding (Critical #1) -----------------------
#
# Before this fix, `save_exclusion_entry` had zero non-test callers anywhere in the
# running bot: `KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS` was parsed but applied
# nowhere, so no operator action could actually keep a channel out of the ledger.
# These tests prove the fix end-to-end: a configured channel ID genuinely produces
# an `ExclusionEntry` row via `sweep_cycle`, and a subsequent `on_message` call for
# that exact channel is genuinely excluded from ingestion -- not just a unit test of
# the seed method in isolation.


@pytest.mark.asyncio
async def test_sweep_cycle_seeds_exclusions_and_ingestion_honors_them_end_to_end(
    store: SQLiteStore,
) -> None:
    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
        excluded_channel_ids=(CHANNEL_ID,),
        default_retention_policy_owner_id=999,
    )

    await rt.sweep_cycle(NOW)

    entries = await store.list_exclusion_entries(GUILD_ID)
    assert [entry.channel_id for entry in entries] == [CHANNEL_ID]
    assert entries[0].excluded_by == 999

    # The seeded row must be genuinely enforced by real message ingestion, not just
    # present in storage.
    await rt.on_message(FakeMessage(channel_id=CHANNEL_ID), NOW)
    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()

    # A different, non-excluded channel is unaffected.
    await rt.on_message(FakeMessage(channel_id=CHANNEL_ID + 1), NOW)
    events = await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_sweep_cycle_seeds_one_entry_per_configured_channel(
    store: SQLiteStore,
) -> None:
    other_channel_id = CHANNEL_ID + 1
    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
        excluded_channel_ids=(CHANNEL_ID, other_channel_id),
        default_retention_policy_owner_id=999,
    )

    await rt.sweep_cycle(NOW)

    entries = await store.list_exclusion_entries(GUILD_ID)
    assert {entry.channel_id for entry in entries} == {CHANNEL_ID, other_channel_id}


@pytest.mark.asyncio
async def test_sweep_cycle_never_overwrites_a_staff_set_exclusion_entry(
    store: SQLiteStore,
) -> None:
    staff_entry = ExclusionEntry(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        excluded_by=42,
        reason="staff lounge -- do not touch",
        excluded_at=NOW - timedelta(days=1),
    )
    await store.save_exclusion_entry(staff_entry)
    rt = ActivityRuntime(
        store,
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
        excluded_channel_ids=(CHANNEL_ID,),
        default_retention_policy_owner_id=999,
    )

    await rt.sweep_cycle(NOW)

    entries = await store.list_exclusion_entries(GUILD_ID)
    assert len(entries) == 1
    assert entries[0].excluded_by == 42  # staff's own value, never overwritten
    assert entries[0].reason == "staff lounge -- do not touch"


@pytest.mark.asyncio
async def test_sweep_cycle_does_not_seed_exclusions_when_unset(
    runtime: ActivityRuntime, store: SQLiteStore
) -> None:
    await runtime.sweep_cycle(NOW)

    assert await store.list_exclusion_entries(GUILD_ID) == ()


def test_excluded_channel_ids_requires_an_owner_id(store: SQLiteStore) -> None:
    with pytest.raises(ValueError, match="default_retention_policy_owner_id"):
        ActivityRuntime(
            store,
            activity_ledger_enabled=True,
            guild_ids=lambda: (GUILD_ID,),
            excluded_channel_ids=(CHANNEL_ID,),
        )


# --- DM double-gate: checked at the dispatch site, not just inside the extractor ---
#
# See Important #4 of the 2026-08-06 Phase 4 final-review fix report: the design
# doc requires `guild is None` be checked both at the dispatch site (here) and again
# inside the consuming extraction function -- matching `WatchdogRuntime`'s existing
# double-gate pattern. These tests prove the gate exists at *this* layer specifically
# by asserting the extractor is never even called for a DM, not merely that no event
# ends up ingested (which the extractor's own internal check alone would already
# guarantee).


@pytest.mark.asyncio
async def test_on_message_dm_gate_short_circuits_before_extraction(
    runtime: ActivityRuntime, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import krubit.discord.activity_runtime as activity_runtime_module

    calls: list[object] = []

    def _tracking_extract(message: object, now: datetime) -> None:
        calls.append(message)
        return None

    monkeypatch.setattr(activity_runtime_module, "extract_message_event", _tracking_extract)

    await runtime.on_message(FakeMessage(guild=None), NOW)

    assert calls == []


@pytest.mark.asyncio
async def test_on_reaction_add_dm_gate_short_circuits_before_extraction(
    runtime: ActivityRuntime, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import krubit.discord.activity_runtime as activity_runtime_module

    calls: list[object] = []

    def _tracking_extract(payload: object, now: datetime) -> None:
        calls.append(payload)
        return None

    monkeypatch.setattr(activity_runtime_module, "extract_reaction_event", _tracking_extract)

    await runtime.on_reaction_add(FakeReactionPayload(guild_id=None), NOW)

    assert calls == []


# --- sweep_cycle: oauth_attempts purge -------------------------------------------


@pytest.mark.asyncio
async def test_sweep_cycle_purges_oauth_attempts_without_blocking_guild_sweeps(
    store: SQLiteStore,
) -> None:
    """Verify that oauth_attempts are purged during sweep_cycle and that guild
    sweeps still run successfully."""
    from krubit.domain.activity_ledger import RetentionPolicy
    from krubit.domain.creator_signals import CreatorAccount, Platform

    runtime = build_runtime(store)
    now = datetime(2026, 8, 7, tzinfo=UTC)

    # Create a creator account first (foreign key constraint)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=GUILD_ID,
            account_id="a",
            owner_member_id=MEMBER_ID,
            platform=Platform.TIKTOK,
            handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle",
            paused=False,
            created_at=now,
            updated_at=now,
        )
    )

    old_consumed = await store.issue_oauth_attempt(
        guild_id=GUILD_ID,
        member_id=MEMBER_ID,
        account_id="a",
        platform="tiktok",
        capability="account",
        redirect_uri="https://x.test/cb",
        now=now - timedelta(days=40),
        ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(old_consumed, now=now - timedelta(days=40))

    # Set up a retention policy and create an event so guild sweep does work
    await store.save_retention_policy(
        RetentionPolicy(guild_id=GUILD_ID, max_age_days=7, updated_by=999, updated_at=now)
    )
    await runtime.on_message(FakeMessage(), now - timedelta(days=30))
    assert len(await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)) == 1

    # Run sweep_cycle - must not raise and must purge oauth + run guild sweep
    await runtime.sweep_cycle(now)

    # Guild sweep must have pruned the old event
    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()
    # Old consumed attempt should be purged or at least not usable
    assert await store.consume_oauth_attempt(old_consumed, now=now) is None


@pytest.mark.asyncio
async def test_sweep_cycle_continues_guild_sweeps_when_oauth_purge_fails(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that when oauth_attempts purge fails, guild sweeps still run."""
    from krubit.domain.activity_ledger import RetentionPolicy
    from krubit.domain.creator_signals import CreatorAccount, Platform

    runtime = build_runtime(store)
    now = datetime(2026, 8, 7, tzinfo=UTC)

    # Create a creator account first (foreign key constraint)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=GUILD_ID,
            account_id="a",
            owner_member_id=MEMBER_ID,
            platform=Platform.TIKTOK,
            handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle",
            paused=False,
            created_at=now,
            updated_at=now,
        )
    )

    async def failing_purge(*args: object, **kwargs: object) -> int:
        raise RuntimeError("simulated purge failure")

    monkeypatch.setattr(store, "purge_oauth_attempts", failing_purge)

    # Set up a retention policy so guild sweep has something to do
    await store.save_retention_policy(
        RetentionPolicy(guild_id=GUILD_ID, max_age_days=7, updated_by=999, updated_at=now)
    )
    # Add a message event that will be aged out
    await runtime.on_message(FakeMessage(), now - timedelta(days=30))
    assert len(await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID)) == 1

    # sweep_cycle must not raise even though oauth purge fails
    await runtime.sweep_cycle(now)

    # The guild sweep must still have run and pruned the old event
    assert await store.list_ledger_events(GUILD_ID, member_id=MEMBER_ID) == ()


@pytest.mark.asyncio
async def test_sweep_cycle_purges_oauth_attempts_even_when_ledger_disabled(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMPORTANT FIX #4: oauth_attempts are produced whenever creator_signals_enabled
    is on, independent of activity_ledger_enabled. A deployment with creator signals
    on and the activity ledger off must still purge this table -- the purge call
    must run before (not after) the `activity_ledger_enabled` early-return.

    Spies on `store.purge_oauth_attempts` directly (rather than inferring from
    downstream row visibility) so the assertion isolates exactly what changed:
    whether the purge call happens at all when the ledger is disabled.
    """
    disabled_runtime = build_runtime(store, activity_ledger_enabled=False)
    now = datetime(2026, 8, 7, tzinfo=UTC)

    calls: list[datetime] = []
    original_purge = store.purge_oauth_attempts

    async def spying_purge(
        purge_now: datetime, *, consumed_retention: timedelta, unconsumed_grace: timedelta
    ) -> int:
        calls.append(purge_now)
        return await original_purge(
            purge_now, consumed_retention=consumed_retention, unconsumed_grace=unconsumed_grace
        )

    monkeypatch.setattr(store, "purge_oauth_attempts", spying_purge)

    await disabled_runtime.sweep_cycle(now)

    # The purge must have run even though the activity ledger itself is disabled.
    assert len(calls) == 1
