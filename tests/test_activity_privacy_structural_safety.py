"""Phase 4 Activity Ledger structural privacy proofs (Task 9).

These two tests are, per the design doc's Completion Gate, "the two most important
checks in the whole phase":

1. `test_excluded_channel_events_structurally_cannot_reach_storage` -- proves the
   channel-exclusion check is unconditionally on the real path from every Discord
   gateway entry point (`ActivityRuntime.on_message`/`on_reaction_add`/
   `on_voice_state_update`/`on_scheduled_event_user_add`) through to storage, using
   a real excluded channel and a counting spy store -- not a grep/regex proxy.
2. `test_member_deletion_covers_every_table_the_schema_actually_defines` -- cross-
   checks the deletion-completeness table lists against the LIVE schema's actual
   table set via `sqlite_master`, not merely a hardcoded Python list that could
   silently drift.

## How the ingestion entry-point set below was hand-verified (read before editing)

Phase 3's Task 9 structural test originally covered only 5 of 10 real modules
because it filtered by filename (`glob("**/watchdog*.py")`) rather than by actual
membership in the feature -- a whole-branch review had to catch and fix it. To avoid
repeating that mistake, the entry-point set covered here was built by hand-reading
the real call graph, not by pattern-matching filenames:

- `src/krubit/discord/bot.py`'s `KrubitBot` is the only place gateway callbacks are
  wired to `self._activity_runtime` (grep for `self._activity_runtime\\.` in that
  file): `on_member_join`, `on_message`, `on_member_remove`, `on_member_update`,
  `on_raw_reaction_add`, `on_raw_reaction_remove`, `on_voice_state_update`,
  `on_scheduled_event_user_add`, `on_scheduled_event_user_remove`.
- Of those, only four ever construct a *channel-bearing* `LedgerEvent` kind
  (`MessageEvent`, `ReactionEvent`, `VoiceSessionEvent` -- see
  `krubit.services.activity_ingestion._channel_id`, the only place that decides
  which kinds even have a channel to exclude): `on_message` (message),
  `on_reaction_add` (reaction), and `on_voice_state_update` (voice, via the join/
  leave tracking cache in `ActivityRuntime._close_voice_session`). `on_reaction_
  remove` is a documented no-op (no "reaction removed" ledger kind exists) and
  never calls `ingest` at all -- confirmed by reading
  `ActivityRuntime.on_reaction_remove`'s body, which returns after the enabled
  check with no further statement.
- `on_scheduled_event_user_add`/`_remove` (attendance) are included even though
  `EventAttendanceEvent` carries no channel, specifically to prove a negative: that
  a channel-less kind still flows through the *same single gate*
  (`ActivityIngestionService.ingest`) rather than some second, unguarded path. This
  is the "not just one entry point" requirement from the brief -- attendance is the
  entry point that proves the gate is truly the sole storage doorway, not merely
  the one four message/reaction/voice-shaped kinds happen to use.
- `on_member_join`/`on_member_update` (join / role-change) construct
  `JoinEvent`/`RoleChangeEvent` -- also channel-less kinds, confirmed the same way.
  They are not separately re-tested here beyond the source-level single-call-site
  guard below, since they exercise the identical `ingest()` gate as attendance with
  no new code path.
- Confirmed by direct grep (`grep -rn "record_ledger_event" src/`, run during this
  task) that `krubit.storage.sqlite.SQLiteStore.record_ledger_event` has exactly
  ONE caller anywhere in `src/`: `ActivityIngestionService.ingest`
  (`src/krubit/services/activity_ingestion.py`). No Discord-layer module, no other
  service module, calls it directly. `test_ingest_is_the_only_caller_of_
  record_ledger_event_in_src` below turns that grep into a repeatable, executable
  guard so a future direct call from a new module fails the build instead of
  silently reintroducing a bypass.

This enumeration is a call-graph reading, not a filename glob -- there is no
`glob("**/activity*.py")` anywhere in this file for exactly the reason Phase 3's
post-mortem gives.

## How the deletion-completeness table set was hand-verified

`ACTIVITY_LEDGER_TABLES` (`src/krubit/services/activity_privacy.py`) is a hardcoded
5-name tuple. Rather than trust it blindly, `test_member_deletion_covers_every_
table_the_schema_actually_defines` below queries `sqlite_master` for **every** table
name the live, freshly-`initialize()`-d schema actually creates, and asserts that
set equals `ACTIVITY_LEDGER_TABLES` union an explicit, hand-enumerated list of every
other (pre-Phase-4) table (`_NON_ACTIVITY_LEDGER_TABLES` below, built by reading
every `CREATE TABLE IF NOT EXISTS` statement in `SQLiteStore._initialize` at
`src/krubit/storage/sqlite.py` during this task). This is the staleness guard the
brief permits as an alternative to schema-derived discovery (mirroring
`test_activity_privacy.py::test_all_member_scoped_tables_matches_live_schema`'s own
already-established pattern) -- a future table added to the schema without being
added to *either* list fails this test immediately, closing the gap the existing
`ALL_MEMBER_SCOPED_TABLES` check does not cover (that check only ever scans tables
already inside `ACTIVITY_LEDGER_TABLES`, so it cannot notice a whole table missing
from that list in the first place).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from krubit.discord.activity_runtime import ActivityRuntime
from krubit.domain.activity_ledger import (
    ExclusionEntry,
    LedgerEvent,
    MessageEvent,
    Milestone,
    MilestoneKind,
    RetentionPolicy,
)
from krubit.services.activity_privacy import ACTIVITY_LEDGER_TABLES, ALL_MEMBER_SCOPED_TABLES
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
GUILD_ID = 111
EXCLUDED_CHANNEL_ID = 555
OPEN_CHANNEL_ID = 556
MEMBER_ID = 222

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SQLITE_PY = _REPO_ROOT / "src" / "krubit" / "storage" / "sqlite.py"

# -- Every table that exists in the live schema BEFORE Phase 4's 5 tables, hand-read
# from every `CREATE TABLE IF NOT EXISTS` statement in `SQLiteStore._initialize`
# (`src/krubit/storage/sqlite.py`) during this task. See the module docstring's "How
# the deletion-completeness table set was hand-verified" section.
_NON_ACTIVITY_LEDGER_TABLES: frozenset[str] = frozenset(
    {
        "guild_config",
        "guild_events",
        "action_receipts",
        "configuration_snapshots",
        "daily_summaries",
        "live_signal_config",
        "live_signal_sessions",
        "live_signal_deliveries",
        "live_signal_checks",
        "creator_profiles",
        "creator_accounts",
        "creator_routes",
        "connector_authorizations",
        "oauth_attempts",
        "creator_registry_receipts",
        "content_events",
        "content_cursors",
        "content_deliveries",
        "content_delivery_attempts",
        "content_correlations",
        "mention_budget_state",
        "mention_budget_receipts",
        "content_receipts",
        "scheduled_event_mappings",
        "creator_bootstrap",
        "content_schedule",
        "entry_sniff_assessments",
        "watch_windows",
        "incidents",
        "guild_allow_block_lists",
        "sniff_receipts",
    }
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "krubit.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(db_path)
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


# =====================================================================================
# Structural proof 1: excluded-channel events cannot reach storage, from every real
# Discord gateway entry point.
# =====================================================================================


class _SpyStore:
    """A minimal spy standing in for `SQLiteStore`: counts every `record_ledger_event`
    call and its argument, and answers `list_exclusion_entries` from a fixed,
    caller-supplied set. This is NOT a mock of the exclusion *logic* -- the real
    `ActivityIngestionService`/`ActivityRuntime` production code runs unmodified
    against this spy; only the durable-storage boundary is replaced, exactly the
    boundary the design doc says must never be reached for an excluded channel.
    """

    def __init__(self, *, exclusions: tuple[ExclusionEntry, ...]) -> None:
        self.exclusions = exclusions
        self.recorded: list[LedgerEvent] = []

    async def list_exclusion_entries(self, guild_id: int) -> tuple[ExclusionEntry, ...]:
        return tuple(entry for entry in self.exclusions if entry.guild_id == guild_id)

    async def record_ledger_event(self, event: LedgerEvent) -> None:
        self.recorded.append(event)


def _excluded_entry() -> ExclusionEntry:
    return ExclusionEntry(
        guild_id=GUILD_ID,
        channel_id=EXCLUDED_CHANNEL_ID,
        excluded_by=999,
        reason="staff-configured exclusion for this test",
        excluded_at=NOW,
    )


def _runtime(spy: _SpyStore) -> ActivityRuntime:
    return ActivityRuntime(
        spy,  # type: ignore[arg-type]  # structurally satisfies the methods ActivityRuntime/ActivityIngestionService call
        activity_ledger_enabled=True,
        guild_ids=lambda: (GUILD_ID,),
    )


class _Guild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class _Author:
    def __init__(self, member_id: int, *, bot: bool = False) -> None:
        self.id = member_id
        self.bot = bot


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id


class _Message:
    def __init__(self, *, channel_id: int) -> None:
        self.guild = _Guild(GUILD_ID)
        self.author = _Author(MEMBER_ID)
        self.channel = _Channel(channel_id)


class _ReactionPayload:
    def __init__(self, *, channel_id: int) -> None:
        self.guild_id = GUILD_ID
        self.user_id = MEMBER_ID
        self.channel_id = channel_id
        self.emoji = "\U0001f44d"


class _Member:
    def __init__(self) -> None:
        self.id = MEMBER_ID
        self.guild = _Guild(GUILD_ID)


class _VoiceState:
    def __init__(self, channel_id: int | None) -> None:
        self.channel = _Channel(channel_id) if channel_id is not None else None


class _ScheduledEvent:
    def __init__(self, event_id: int = 777) -> None:
        self.id = event_id
        self.guild_id = GUILD_ID


class _RSVPUser:
    def __init__(self) -> None:
        self.id = MEMBER_ID


@pytest.mark.asyncio
async def test_excluded_channel_events_structurally_cannot_reach_storage() -> None:
    """The core proof: a message, a reaction, and a completed voice session in an
    EXCLUDED channel each produce ZERO `record_ledger_event` calls; the identical
    events in a NON-excluded channel each produce exactly ONE. Attendance (a
    channel-less kind) is exercised too, to prove it flows through the very same
    `ingest()` gate rather than a second, unguarded path -- see the module
    docstring's entry-point enumeration for why attendance belongs in this test even
    though channel exclusion structurally cannot apply to it.
    """
    spy = _SpyStore(exclusions=(_excluded_entry(),))
    runtime = _runtime(spy)

    # -- message: excluded channel -> zero storage calls -----------------------------
    await runtime.on_message(_Message(channel_id=EXCLUDED_CHANNEL_ID), NOW)
    assert len(spy.recorded) == 0

    # -- message: open channel -> exactly one storage call ----------------------------
    await runtime.on_message(_Message(channel_id=OPEN_CHANNEL_ID), NOW)
    assert len(spy.recorded) == 1
    spy.recorded.clear()

    # -- reaction: excluded channel -> zero storage calls ------------------------------
    await runtime.on_reaction_add(_ReactionPayload(channel_id=EXCLUDED_CHANNEL_ID), NOW)
    assert len(spy.recorded) == 0

    # -- reaction: open channel -> exactly one storage call ----------------------------
    await runtime.on_reaction_add(_ReactionPayload(channel_id=OPEN_CHANNEL_ID), NOW)
    assert len(spy.recorded) == 1
    spy.recorded.clear()

    # -- voice: a full join+leave session in the EXCLUDED channel -> zero storage -----
    member = _Member()
    await runtime.on_voice_state_update(
        member, _VoiceState(None), _VoiceState(EXCLUDED_CHANNEL_ID), NOW
    )
    leave_time = datetime(2026, 8, 6, 12, 5, tzinfo=UTC)
    await runtime.on_voice_state_update(
        member, _VoiceState(EXCLUDED_CHANNEL_ID), _VoiceState(None), leave_time
    )
    assert len(spy.recorded) == 0

    # -- voice: a full join+leave session in the OPEN channel -> exactly one ----------
    await runtime.on_voice_state_update(
        member, _VoiceState(None), _VoiceState(OPEN_CHANNEL_ID), NOW
    )
    await runtime.on_voice_state_update(
        member, _VoiceState(OPEN_CHANNEL_ID), _VoiceState(None), leave_time
    )
    assert len(spy.recorded) == 1
    spy.recorded.clear()

    # -- attendance: channel-less kind still reaches storage through the same gate ----
    await runtime.on_scheduled_event_user_add(_ScheduledEvent(), _RSVPUser(), NOW)
    assert len(spy.recorded) == 1
    assert spy.recorded[0].guild_id == GUILD_ID
    assert spy.recorded[0].member_id == MEMBER_ID


@pytest.mark.asyncio
async def test_reaction_remove_never_calls_ingest_at_all() -> None:
    """`on_reaction_remove` is a documented no-op (no ledger kind exists for it) --
    confirm it produces zero storage calls even for a non-excluded channel, so a
    future accidental wiring of a "reaction removed" event does not silently start
    recording without a test noticing.
    """
    spy = _SpyStore(exclusions=())
    runtime = _runtime(spy)

    await runtime.on_reaction_remove(_ReactionPayload(channel_id=OPEN_CHANNEL_ID), NOW)

    assert len(spy.recorded) == 0


@pytest.mark.asyncio
async def test_ingest_is_the_only_caller_of_record_ledger_event_in_src() -> None:
    """Turns the grep run during this task's investigation into a repeatable guard:
    `SQLiteStore.record_ledger_event` must have exactly one call site anywhere in
    `src/krubit` -- `ActivityIngestionService.ingest`
    (`src/krubit/services/activity_ingestion.py`). A second call site anywhere else
    would be a structural bypass of the exclusion gate proved above, regardless of
    whether that new call site happens to also apply the exclusion check correctly.
    """
    pattern = re.compile(r"\.record_ledger_event\(")
    call_sites: list[str] = []
    for path in (_REPO_ROOT / "src" / "krubit").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            call_sites.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{line_no}")

    assert len(call_sites) == 1, f"expected exactly one call site, found: {call_sites}"
    assert "src/krubit/services/activity_ingestion.py" in call_sites[0]


# =====================================================================================
# Structural proof 2: member deletion covers every table the LIVE schema defines.
# =====================================================================================


@pytest.mark.asyncio
async def test_activity_ledger_tables_matches_the_live_schema_exactly(
    store: SQLiteStore, db_path: Path
) -> None:
    """The staleness guard: every table name in the live, freshly-initialized schema
    must be accounted for by either `ACTIVITY_LEDGER_TABLES` or the explicit,
    hand-enumerated `_NON_ACTIVITY_LEDGER_TABLES` list above. If a future task adds a
    new Phase 4 table to `SQLiteStore._initialize` without adding it to
    `ACTIVITY_LEDGER_TABLES`, this test fails immediately instead of the deletion
    silently missing that table forever.
    """
    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        live_tables = {str(row[0]) for row in await cursor.fetchall()}

    accounted_for = set(ACTIVITY_LEDGER_TABLES) | _NON_ACTIVITY_LEDGER_TABLES
    assert live_tables == accounted_for, (
        f"Live schema has tables not accounted for by either ACTIVITY_LEDGER_TABLES "
        f"or _NON_ACTIVITY_LEDGER_TABLES: {live_tables - accounted_for}. "
        f"Accounted-for tables that no longer exist in the live schema: "
        f"{accounted_for - live_tables}."
    )
    # Sanity: the two lists must not overlap either.
    assert set(ACTIVITY_LEDGER_TABLES).isdisjoint(_NON_ACTIVITY_LEDGER_TABLES)


@pytest.mark.asyncio
async def test_member_deletion_covers_every_table_the_schema_actually_defines(
    store: SQLiteStore, db_path: Path
) -> None:
    """Combines the schema-derived member-scoped table set with a real deletion: seed
    one row in every table the LIVE schema (via `PRAGMA table_info`, not a hardcoded
    guess) says carries a `member_id` column, call `delete_member_ledger_data`, and
    assert every one of those tables is empty for that member afterward -- proving
    `ALL_MEMBER_SCOPED_TABLES` and the live schema agree, and that agreement is
    actually sufficient for deletion completeness, not just a listed intention.
    """
    async with aiosqlite.connect(db_path) as connection:
        derived_member_scoped: list[str] = []
        for table in ACTIVITY_LEDGER_TABLES:
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "member_id" in columns:
                derived_member_scoped.append(table)

    assert set(derived_member_scoped) == set(ALL_MEMBER_SCOPED_TABLES)

    await store.record_ledger_event(
        MessageEvent(guild_id=GUILD_ID, member_id=MEMBER_ID, occurred_at=NOW, channel_id=1)
    )
    await store.save_milestone(
        Milestone(
            guild_id=GUILD_ID,
            member_id=MEMBER_ID,
            kind=MilestoneKind.MESSAGE_COUNT,
            reached_at=NOW,
            detail="reached 10 messages",
        )
    )
    await store.record_activity_receipt(
        guild_id=GUILD_ID,
        receipt_id="receipt-structural-1",
        member_id=MEMBER_ID,
        action="milestone_reached",
        detail={"milestone_kind": "message_count"},
        created_at=NOW,
    )
    # Also seed the two channel-less, non-member-scoped tables, to prove deletion
    # does NOT need to (and does not) touch them.
    await store.save_exclusion_entry(_excluded_entry())
    await store.save_retention_policy(
        RetentionPolicy(guild_id=GUILD_ID, max_age_days=30, updated_by=999, updated_at=NOW)
    )

    await store.delete_member_ledger_data(GUILD_ID, MEMBER_ID)

    async with aiosqlite.connect(db_path) as connection:
        for table in derived_member_scoped:
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND member_id = ?",
                (GUILD_ID, MEMBER_ID),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0, f"{table} still has rows for the deleted member"

    # Channel-less, guild-level config is untouched by member deletion.
    exclusions = await store.list_exclusion_entries(GUILD_ID)
    assert len(exclusions) == 1
    policy = await store.get_retention_policy(GUILD_ID)
    assert policy is not None
