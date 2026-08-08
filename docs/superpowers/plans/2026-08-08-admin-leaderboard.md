# `/fetch admin leaderboard` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a staff-only `/fetch admin leaderboard [year]` command ranking
guild members by meaningful-action count within a calendar year, with an
automatic caveat when the guild's configured retention policy is shorter
than the requested year's elapsed span.

**Architecture:** A new SQL aggregate storage method
(`SQLiteStore.leaderboard_counts`) does the counting in the database rather
than in Python, avoiding the existing 5000-row read cap. A new service
function (`activity_views.leaderboard`) computes the calendar-year boundary
and the retention caveat. A new `ActivityCommandService.leaderboard` method
adds the staff-only authority gate and card rendering, matching every
sibling command in that module. A new `AdminCommands.leaderboard` Discord
command wires it up, matching the `exclusions`/`returning` command pattern
exactly.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite, pytest,
pytest-asyncio.

## Global Constraints

- Metric: count of the four `MEANINGFUL_EVENT_KINDS` events (`MESSAGE`,
  `REACTION`, `VOICE_SESSION`, `EVENT_ATTENDANCE`) — no other event kind
  counts.
- Window: calendar year, half-open interval `[Jan 1 00:00 UTC of `year`,
  Jan 1 00:00 UTC of `year + 1`)`.
- `year` is an optional command parameter (`app_commands.Range[int, 2020,
  <current year>]`), defaulting to the current year when omitted.
- Display: top 10 entries by count descending, ties broken by ascending
  `member_id`; members with zero meaningful actions in the window are
  omitted entirely.
- No numeric "worthiness" or composite score — one plain, named count only.
- The command must append a retention caveat line whenever the guild's
  configured `RetentionPolicy.max_age_days` is shorter than the number of
  days elapsed since the requested year's start (relative to `now`).
- Staff-only: uses the existing `self._parent.authorize(interaction,
  "fetch_admin_leaderboard")` gate, matching every other `AdminCommands`
  method — never self-service.

---

### Task 1: `SQLiteStore.leaderboard_counts` aggregate query

**Files:**
- Modify: `src/krubit/storage/sqlite.py` (add method after
  `list_ledger_events_for_guild`, which ends at line 3946)
- Test: `tests/test_activity_ledger_storage.py`

**Interfaces:**
- Produces: `async def leaderboard_counts(self, guild_id: int, *, start: datetime, end: datetime) -> tuple[tuple[int, int], ...]` —
  returns `(member_id, count)` pairs, sorted by count descending then
  `member_id` ascending. `start`/`end` must be timezone-aware; `end` is
  exclusive (half-open interval, matching `[start, end)`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_activity_ledger_storage.py`, in the "Ledger events"
section (after `test_list_ledger_events_orders_most_recent_first`, before
the "## Milestones" section comment if one exists, otherwise right after
the last ledger-events test):

```python
@pytest.mark.asyncio
async def test_leaderboard_counts_counts_only_meaningful_kinds_and_orders_by_count(
    store: SQLiteStore,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2027, 1, 1, tzinfo=UTC)
    mid = datetime(2026, 6, 1, tzinfo=UTC)

    # Member 1: two meaningful events (message + reaction) -> count 2.
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=1, occurred_at=mid, channel_id=333)
    )
    await store.record_ledger_event(
        ReactionEvent(
            guild_id=111, member_id=1, occurred_at=mid, channel_id=333, emoji="🎉"
        )
    )
    # Member 1's JOIN event must NOT be counted (not a meaningful kind).
    await store.record_ledger_event(JoinEvent(guild_id=111, member_id=1, occurred_at=mid))

    # Member 2: three meaningful events -> count 3, ranks above member 1.
    for _ in range(3):
        await store.record_ledger_event(
            MessageEvent(guild_id=111, member_id=2, occurred_at=mid, channel_id=333)
        )

    # A different guild's events must never contribute.
    await store.record_ledger_event(
        MessageEvent(guild_id=999, member_id=1, occurred_at=mid, channel_id=333)
    )

    counts = await store.leaderboard_counts(111, start=start, end=end)
    assert counts == ((2, 3), (1, 2))


@pytest.mark.asyncio
async def test_leaderboard_counts_respects_half_open_year_boundary(
    store: SQLiteStore,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2027, 1, 1, tzinfo=UTC)

    # Exactly at start: counts.
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=1, occurred_at=start, channel_id=333)
    )
    # Exactly at end: must NOT count (exclusive upper bound).
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=1, occurred_at=end, channel_id=333)
    )
    # One second before end: counts.
    await store.record_ledger_event(
        MessageEvent(
            guild_id=111,
            member_id=1,
            occurred_at=end - timedelta(seconds=1),
            channel_id=333,
        )
    )

    counts = await store.leaderboard_counts(111, start=start, end=end)
    assert counts == ((1, 2),)
```

Add `from datetime import timedelta` to the existing `from datetime import
UTC, datetime` import line at the top of the file if `timedelta` is not
already imported (check first — it is not, per the current import list).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_ledger_storage.py -v -k leaderboard_counts`
Expected: both FAIL with `AttributeError: 'SQLiteStore' object has no attribute 'leaderboard_counts'`

- [ ] **Step 3: Implement `leaderboard_counts`**

Add this method to `SQLiteStore` in `src/krubit/storage/sqlite.py`,
immediately after `list_ledger_events_for_guild` (which currently ends at
line 3946, right before `save_milestone`):

```python
    async def leaderboard_counts(
        self, guild_id: int, *, start: datetime, end: datetime
    ) -> tuple[tuple[int, int], ...]:
        """Return `(member_id, count)` pairs for every member with at least one
        `MEANINGFUL_EVENT_KINDS` event in the half-open interval `[start, end)`,
        sorted by count descending then `member_id` ascending.

        Backs `/fetch admin leaderboard`. Aggregates in SQL rather than reading
        raw rows into Python (unlike `list_ledger_events_for_guild`, which caps
        at 5000 rows) since a full calendar year of events can exceed that cap
        for an active guild and the leaderboard only ever needs counts, never
        event detail.
        """
        _require_guild_id(guild_id)
        _require_aware("start", start)
        _require_aware("end", end)
        kind_placeholders = ", ".join("?" for _ in MEANINGFUL_EVENT_KINDS)
        cursor = await self._connection.execute(
            f"""
            SELECT member_id, COUNT(*) AS action_count
            FROM ledger_events
            WHERE guild_id = ?
              AND kind IN ({kind_placeholders})
              AND occurred_at >= ?
              AND occurred_at < ?
            GROUP BY member_id
            ORDER BY action_count DESC, member_id ASC
            """,
            (
                guild_id,
                *(kind.value for kind in MEANINGFUL_EVENT_KINDS),
                start.isoformat(),
                end.isoformat(),
            ),
        )
        rows = await cursor.fetchall()
        return tuple((int(row[0]), int(row[1])) for row in rows)
```

Check whether `_require_aware` already exists in `sqlite.py` (search for
`def _require_aware`). If it does not exist in this file (it currently
lives only in `domain/activity_ledger.py` and `services/activity_views.py`,
each with their own private copy), add a private copy near
`_require_guild_id` in `sqlite.py`:

```python
def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
```

Also check that `MEANINGFUL_EVENT_KINDS` is already imported from
`krubit.domain.activity_ledger` in `sqlite.py` (search the existing import
block near the top of the file, which already imports many names from that
module for `LedgerEvent`/`Milestone` construction) — add it to that import
line if missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_ledger_storage.py -v -k leaderboard_counts`
Expected: both PASS

- [ ] **Step 5: Run the full test file and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_ledger_storage.py -v`
Expected: all pass, no regressions.

```bash
git add src/krubit/storage/sqlite.py tests/test_activity_ledger_storage.py
git commit -m "feat: add leaderboard_counts SQL aggregate query"
```

---

### Task 2: `activity_views.leaderboard` service function

**Files:**
- Modify: `src/krubit/services/activity_views.py`
- Test: create `tests/test_activity_views_leaderboard.py`

**Interfaces:**
- Consumes: `SQLiteStore.leaderboard_counts(guild_id, *, start, end)` (Task 1),
  `SQLiteStore.get_retention_policy(guild_id) -> RetentionPolicy | None`
  (already exists, `src/krubit/storage/sqlite.py:4078`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class LeaderboardEntry`: `member_id: int`, `count: int`.
  - `@dataclass(frozen=True, slots=True) class LeaderboardResult`: `year: int`, `entries: tuple[LeaderboardEntry, ...]`, `retention_caveat: bool`.
  - `async def leaderboard(store: SQLiteStore, guild_id: int, *, year: int, now: datetime) -> LeaderboardResult`

**Leaderboard entry cap:** this function returns at most 10 entries — the
top-10 truncation happens here, not at the Discord layer, so the service
function's contract is "the leaderboard," not "all counts."

- [ ] **Step 1: Write the failing tests**

Create `tests/test_activity_views_leaderboard.py`:

```python
"""Unit tests for `krubit.services.activity_views.leaderboard`.

Uses a real on-disk `SQLiteStore` (never mocked), matching every sibling
`activity_views` test file's convention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.activity_ledger import MessageEvent, RetentionPolicy
from krubit.services.activity_views import leaderboard
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_leaderboard_defaults_to_current_year_and_ranks_by_count(
    store: SQLiteStore,
) -> None:
    inside_year = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(3):
        await store.record_ledger_event(
            MessageEvent(guild_id=111, member_id=1, occurred_at=inside_year, channel_id=333)
        )
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=2, occurred_at=inside_year, channel_id=333)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.year == 2026
    assert result.retention_caveat is False
    assert [entry.member_id for entry in result.entries] == [1, 2]
    assert [entry.count for entry in result.entries] == [3, 1]


@pytest.mark.asyncio
async def test_leaderboard_past_year_uses_full_calendar_year_not_bounded_by_now(
    store: SQLiteStore,
) -> None:
    december_2025 = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=1, occurred_at=december_2025, channel_id=333)
    )

    result = await leaderboard(store, 111, year=2025, now=NOW)

    assert result.year == 2025
    assert [entry.member_id for entry in result.entries] == [1]


@pytest.mark.asyncio
async def test_leaderboard_truncates_to_top_ten_and_omits_zero_activity(
    store: SQLiteStore,
) -> None:
    inside_year = datetime(2026, 3, 1, tzinfo=UTC)
    for member_id in range(1, 13):
        for _ in range(member_id):
            await store.record_ledger_event(
                MessageEvent(
                    guild_id=111, member_id=member_id, occurred_at=inside_year, channel_id=333
                )
            )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert len(result.entries) == 10
    assert [entry.member_id for entry in result.entries] == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_true_when_policy_shorter_than_elapsed_span(
    store: SQLiteStore,
) -> None:
    # NOW is 2026-08-08; year start is 2026-01-01 -> 219 days elapsed.
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=90, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is True


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_false_when_no_policy_configured(
    store: SQLiteStore,
) -> None:
    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is False


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_false_when_policy_covers_full_elapsed_span(
    store: SQLiteStore,
) -> None:
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=365, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is False


@pytest.mark.asyncio
async def test_leaderboard_past_year_retention_caveat_uses_full_year_span(
    store: SQLiteStore,
) -> None:
    # For a fully-elapsed past year, the relevant span is the whole year
    # (365/366 days), not bounded by `now` -- a 90-day policy cannot cover it.
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=90, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2025, now=NOW)

    assert result.retention_caveat is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_views_leaderboard.py -v`
Expected: all FAIL with `ImportError: cannot import name 'leaderboard'`

- [ ] **Step 3: Implement `LeaderboardEntry`, `LeaderboardResult`, and `leaderboard`**

Add to `src/krubit/services/activity_views.py`, after the `CommunityPulse`
class (which currently ends at line 164, right before `_group_events_by_member`):

```python
@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One member's meaningful-action count within a `leaderboard` result."""

    member_id: int
    count: int

    def __post_init__(self) -> None:
        _require_positive_id("member_id", self.member_id)
        if self.count < 0:
            raise ValueError("count must not be negative")


@dataclass(frozen=True, slots=True)
class LeaderboardResult:
    """The outcome of `leaderboard` for one guild and calendar year.

    `retention_caveat` is `True` whenever the guild's currently configured
    `RetentionPolicy.max_age_days` is shorter than the span of days this
    result's `year` actually covers (bounded by `now` for the current year,
    or the full 365/366-day year for a past year) -- meaning raw events from
    the early part of that span may already have been pruned by the
    scheduled retention sweep, so `entries` may undercount. `False` when no
    policy is configured (nothing is pruned) or the policy covers the full
    span.
    """

    year: int
    entries: tuple[LeaderboardEntry, ...]
    retention_caveat: bool
```

Add this constant near the module's other fixed-window constants (after
`_RETURNING_TREND_WINDOW`):

```python
# `leaderboard`'s displayed entry cap -- matches the design doc's confirmed
# "top 10" decision, not a general-purpose list-rendering cap (unlike
# `activity_commands._MAX_LIST_ENTRIES`, which this module does not use).
_LEADERBOARD_ENTRY_LIMIT = 10
```

Add this function at the end of the file, after `community_pulse`:

```python
def _year_boundary(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    return start, end


async def leaderboard(
    store: SQLiteStore, guild_id: int, *, year: int, now: datetime
) -> LeaderboardResult:
    """The top `_LEADERBOARD_ENTRY_LIMIT` members by meaningful-action count
    for one calendar year, per the half-open interval `[Jan 1 00:00 UTC of
    `year`, Jan 1 00:00 UTC of `year + 1`)`.

    For the current year (`year == now.year`), the relevant span for the
    retention caveat is bounded by `now` (days elapsed so far), not the full
    year -- future days cannot yet have been pruned. For a past year, the
    relevant span is the full year length (365 or 366 days), since the
    entire year has already elapsed.
    """
    _require_positive_id("guild_id", guild_id)
    _require_aware("now", now)
    start, end = _year_boundary(year)

    counts = await store.leaderboard_counts(guild_id, start=start, end=min(end, now))
    entries = tuple(
        LeaderboardEntry(member_id=member_id, count=count)
        for member_id, count in counts[:_LEADERBOARD_ENTRY_LIMIT]
    )

    elapsed_end = min(now, end)
    elapsed_days = max((elapsed_end - start).days, 0)
    policy = await store.get_retention_policy(guild_id)
    retention_caveat = policy is not None and policy.max_age_days < elapsed_days

    return LeaderboardResult(year=year, entries=entries, retention_caveat=retention_caveat)
```

Note the `end=min(end, now)` passed to `leaderboard_counts`: for the
current year this bounds the query to "so far," since events after `now`
cannot exist; for a past year `end` (Jan 1 of the following year) is
already earlier than `now`, so `min` has no effect there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_views_leaderboard.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full activity_views test suite and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_views_leaderboard.py tests/test_activity_ledger_storage.py -v`
Expected: all pass.

```bash
git add src/krubit/services/activity_views.py tests/test_activity_views_leaderboard.py
git commit -m "feat: add leaderboard service function with retention caveat"
```

---

### Task 3: `ActivityCommandService.leaderboard` and `/fetch admin leaderboard` command

**Files:**
- Modify: `src/krubit/discord/activity_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_activity_commands.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_phase_one_commands.py`

**Interfaces:**
- Consumes: `activity_views.leaderboard` (Task 2), `ActivityActorContext`
  (already defined in `activity_commands.py:178`), `CommandResult`/
  `CommandStatus` (already imported from `content_commands`), `Card`/
  `CardField` (already imported from `domain.models`).
- Produces: `ActivityCommandService.leaderboard(self, *, actor: ActivityActorContext, year: int) -> CommandResult`

- [ ] **Step 1: Write the failing service-layer test**

Add to `tests/test_activity_commands.py`. First inspect the file's existing
imports and fixture setup (it constructs `ActivityCommandService` against a
real `SQLiteStore`, matching every other test file in this codebase — copy
that fixture pattern exactly rather than reinventing it). Add these tests
near the `exclusions`/`recognition_candidates` tests:

```python
@pytest.mark.asyncio
async def test_leaderboard_denies_non_staff_before_any_query(store: SQLiteStore) -> None:
    service = ActivityCommandService(store, now=lambda: NOW)
    actor = ActivityActorContext(guild_id=111, member_id=1, is_staff=False)

    result = await service.leaderboard(actor=actor, year=2026)

    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_leaderboard_renders_top_entries_for_staff(store: SQLiteStore) -> None:
    inside_year = datetime(2026, 3, 1, tzinfo=UTC)
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=2, occurred_at=inside_year, channel_id=333)
    )
    service = ActivityCommandService(store, now=lambda: NOW)
    actor = ActivityActorContext(guild_id=111, member_id=1, is_staff=True)

    result = await service.leaderboard(actor=actor, year=2026)

    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "<@2>" in result.card.description
    assert result.detail["count"] == 1


@pytest.mark.asyncio
async def test_leaderboard_appends_caveat_when_retention_policy_is_short(
    store: SQLiteStore,
) -> None:
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=1, updated_by=555, updated_at=NOW)
    )
    service = ActivityCommandService(store, now=lambda: NOW)
    actor = ActivityActorContext(guild_id=111, member_id=1, is_staff=True)

    result = await service.leaderboard(actor=actor, year=2026)

    assert result.card is not None
    assert "retention" in result.card.description.lower()
```

Check the top of `tests/test_activity_commands.py` for its existing `NOW`
constant and imports (`MessageEvent`, `RetentionPolicy` from
`krubit.domain.activity_ledger`) — add any missing imports rather than
redefining `NOW` if it already exists in the file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k leaderboard`
Expected: all FAIL with `AttributeError: 'ActivityCommandService' object has no attribute 'leaderboard'`

- [ ] **Step 3: Implement `ActivityCommandService.leaderboard`**

Add to `src/krubit/discord/activity_commands.py`. First add the import
(alongside the existing `from krubit.services.activity_views import
community_pulse as _community_pulse_view` line):

```python
from krubit.services.activity_views import leaderboard as _leaderboard_view
```

Add the method to `ActivityCommandService`, after `exclusions` (which
currently ends the class):

```python
    # -- leaderboard: staff-only guild-wide meaningful-action ranking ------------

    async def leaderboard(
        self, *, actor: ActivityActorContext, year: int
    ) -> CommandResult:
        """Top members by meaningful-action count for one calendar year.

        See `krubit.services.activity_views.leaderboard`'s docstring for the
        half-open year-boundary and retention-caveat semantics this wraps.
        """
        if not actor.is_staff:
            return _denied()
        now = self._now()
        result = await _leaderboard_view(self._store, actor.guild_id, year=year, now=now)
        lines = [f"<@{e.member_id}> — {e.count} actions" for e in result.entries]
        description = "\n".join(lines) or "No activity recorded for this year."
        if result.retention_caveat:
            description += (
                "\n\n⚠️ This guild's retention policy is shorter than this "
                "year's elapsed span — early-year activity may already be "
                "pruned, so this count could be incomplete."
            )
        card = Card(
            kind="fetched",
            title=f"Fetched: Leaderboard {year}",
            description=description,
            fields=(CardField("Count", str(len(result.entries)), True),),
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={
                "year": year,
                "count": len(result.entries),
                "retention_caveat": result.retention_caveat,
            },
        )
```

- [ ] **Step 4: Run the service-layer tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k leaderboard`
Expected: all PASS

- [ ] **Step 5: Write the failing Discord-layer tests**

Add to `tests/test_cli.py`, updating the existing structural test
(`test_fetch_commands_direct_children_match_the_reorg_plans_actual_structure`,
starting at line 166) — change the `AdminCommands` child-count assertion
from 16 to 17, and add `"leaderboard"` to the expected name set. Read the
existing test first to find its exact current set literal before editing
it (it starts at line 201, `assert {command.name for command in
admin_commands.commands} == {`).

Add to `tests/test_phase_one_commands.py`, immediately after
`test_fetch_status_is_staff_only_and_receipts_the_requesting_actor` (lines
48-86). This mirrors that test's exact structure — same
`_FakeInteraction`/`_FakeMember`/`monkeypatch` fixtures already defined at
the top of the file (lines 14-45), reused as-is, no changes needed to
them:

```python
@pytest.mark.asyncio
async def test_fetch_admin_leaderboard_is_staff_only_and_defaults_to_current_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    commands = FetchCommands(FoundationService(store))
    admin = next(command for command in commands.commands if command.name == "admin")
    leaderboard = next(
        command for command in admin.commands if command.name == "leaderboard"  # type: ignore[attr-defined]
    )
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        assert leaderboard.default_permissions is not None
        assert leaderboard.default_permissions.manage_guild is True

        denied = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await leaderboard.callback(admin, denied, None)  # type: ignore[arg-type]

        assert denied.response.sent is not None
        assert denied.response.sent["ephemeral"] is True
        assert denied.edited_embed is None

        allowed = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await leaderboard.callback(admin, allowed, None)  # type: ignore[arg-type]

        assert allowed.response.deferred == {"ephemeral": True, "thinking": True}
        assert allowed.edited_embed is not None

        receipts = await store.list_receipts(111)
        assert [(item.action, item.status, item.actor_id) for item in receipts] == [
            ("fetch_admin_leaderboard", "succeeded", 7),
            ("fetch_admin_leaderboard", "denied", 42),
        ]
    finally:
        await store.close()
```

The `leaderboard.callback(admin, denied, None)` call's third positional
argument is the optional `year` parameter — `None` exercises the
default-to-current-year path. If this codebase's `app_commands.command`
callback signature requires keyword-only invocation for optional
parameters when called directly (verify against how this file already
calls any existing optional-parameter command, if one exists; if none
does, keyword form `year=None` is the safer choice — use whichever the
`callback(...)` signature from Step 7 actually accepts when you run this
test and see whether positional or keyword invocation succeeds).

- [ ] **Step 6: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_phase_one_commands.py -v -k leaderboard`
Expected: FAIL — `AdminCommands` has no `leaderboard` command yet.

- [ ] **Step 7: Wire the Discord command**

Add to `src/krubit/discord/bot.py`'s `AdminCommands` class, after
`exclusions` (the last command currently in the class — find it by
searching for `async def exclusions` around line 800):

```python
    @app_commands.command(
        name="leaderboard", description="Fetch the meaningful-action leaderboard for a year"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        year: app_commands.Range[int, 2020, datetime.now(UTC).year] | None = None,
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_admin_leaderboard")
        if context is None:
            return
        guild, actor_id = context
        actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        resolved_year = year if year is not None else datetime.now(UTC).year
        result = await self._parent._activity_commands.leaderboard(
            actor=actor, year=resolved_year
        )
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_admin_leaderboard",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )
```

**Important:** `app_commands.Range[int, 2020, datetime.now(UTC).year]` is
evaluated once at class-definition/import time, which freezes the upper
bound to whatever year the process started in — this is consistent with
how Discord command option bounds work (they are static metadata sent to
Discord's API at command-sync time, not re-evaluated per interaction) and
matches the design spec's stated bound. This is expected, not a bug: a
long-running bot process spanning a New Year's rollover would need a
restart or resync to raise the bound for the new year, same as any other
Discord slash-command option definition. Do not attempt to make this
dynamic per-interaction — `app_commands.Range` does not support that.

Verify `datetime` and `UTC` are already imported at the top of `bot.py`
(they are, used elsewhere in the file, e.g. `server_health`'s
`datetime.now(UTC)` call at line 541) — no new import needed for those.
`ActivityActorContext` is also already imported (used throughout
`AdminCommands`).

- [ ] **Step 8: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_phase_one_commands.py -v -k leaderboard`
Expected: all PASS

- [ ] **Step 9: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, no regressions (baseline before this task was 1112 passing).

- [ ] **Step 10: Lint and type-check**

Run: `./.venv/Scripts/python.exe -m ruff check .`
Expected: all checks pass — fix any findings before proceeding.

Run: `./.venv/Scripts/python.exe -m pyright src/krubit/discord/bot.py src/krubit/discord/activity_commands.py src/krubit/services/activity_views.py src/krubit/storage/sqlite.py`
Expected: no new error *category* versus this branch's baseline (this
codebase's pyright baseline is non-zero, entirely `discord.py` stub noise —
confirm via `git stash`/`git stash pop` diffing if the count changes, per
every prior task's established verification method in this project).

- [ ] **Step 11: Commit**

```bash
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py tests/test_cli.py tests/test_phase_one_commands.py
git commit -m "feat: add /fetch admin leaderboard command"
```

---

## Final Verification

After all three tasks:

- [ ] Run the full suite once more: `./.venv/Scripts/python.exe -m pytest -q` — must show `1112 + N passed` where `N` is the number of new tests added across all three tasks, zero failures.
- [ ] Manually confirm `/fetch admin leaderboard` (no argument) and
      `/fetch admin leaderboard year:2025` both resolve correctly by
      re-reading the final diff — no live Discord verification is required
      for this plan (matching this project's established convention for
      Discord-layer changes verified by direct code trace plus the
      automated test suite, not a live guild run).
- [ ] Update `docs/superpowers/specs/2026-08-08-fetch-command-reorg-design.md`
      is NOT required — that spec's "Explicit Exclusions" section already
      correctly states the leaderboard was deferred; no edit needed there.
