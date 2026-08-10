# Real-Time Member-Join Incident Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A member whose Entry Sniff assessment reaches `RiskBand.INCIDENT`
gets a durable `Incident` recorded and a staff notification sent
immediately at join time, not only via the next periodic sweep cycle.

**Architecture:** Extend `WatchdogRuntime.on_member_join` to construct and
persist an `Incident` (kind `MEMBER`) when the assessment's band is
`INCIDENT`, then call the already-existing `notify_staff`. This mirrors
`RaidDetector._record_incident`'s exact construction/persistence/receipt
shape (`src/krubit/services/raid_detection.py:210-237`) — no new
abstraction, matching this codebase's established per-detector pattern.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite, pytest/pytest-asyncio.

## Global Constraints

- No dedup/suppression: every `INCIDENT`-band join gets its own `Incident`
  and its own `notify_staff` call, regardless of how many other members
  were recently flagged. Do not apply `_has_open_incident_of_kind`'s
  guild-wide same-kind cooldown pattern here — that is correct only for
  the four existing guild-scoped detectors' single-continuing-phenomenon
  incidents (raid, spam wave, etc.), not for independent member joins.
- Evidence-packet handling matches the existing placeholder pattern
  exactly (`f"evidence:{uuid4().hex}"`, discarding its arguments) — no new
  storage table, no wiring of the real `build_evidence_packet` function.
  This is a deliberate scope boundary, not an oversight.
- Signals recorded on the `Incident`'s receipt come from
  `assessment.signals` (already computed by `EntrySniffService.
  assess_join`) — not a new synthesized summary signal.
- `WatchdogRuntime.on_member_join`'s existing behavior for every other
  band (`WATCH`/`SUSPICIOUS`/`CLEAR`) — the `open_if_warranted` call — is
  unchanged. This task is strictly additive.
- The existing `watchdog_enabled` early-return guard at the top of
  `on_member_join` continues to gate everything in this task too (it
  already runs before `assess_join` is ever called).
- `notify_staff` itself is not modified — reused exactly as-is.
- `recommended_action` text matches this codebase's established tone
  (plain, factual, "no automatic action has been taken" — matching
  `_RAID_RECOMMENDED_ACTION`'s exact style in
  `src/krubit/services/raid_detection.py:102-105`).

---

### Task 1: Real-time `MEMBER` incident construction in `WatchdogRuntime.on_member_join`

**Files:**
- Modify: `src/krubit/discord/watchdog_runtime.py`
- Test: `tests/test_watchdog_runtime.py`

**Interfaces:**
- Modifies: `WatchdogRuntime.on_member_join` (existing method, no
  signature change) to additionally construct/persist an `Incident` and
  call `self.notify_staff(...)` when `assessment.band is
  RiskBand.INCIDENT`.
- Adds: a new optional `WatchdogRuntime.__init__` parameter,
  `evidence_builder: Callable[[int, tuple[RiskSignal, ...], datetime],
  str] | None = None`, stored as `self._evidence_builder`, defaulting to
  a small local placeholder function (not imported from
  `raid_detection.py`, which keeps its own private one).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_watchdog_runtime.py`, in the "on_member_join" section
(after the existing three tests, which end around line 247). This file's
imports already include `Incident, IncidentKind, RiskBand` from
`krubit.domain.watchdog` (line 27) — add `AllowBlockEntry` to that same
import line for the block-list helper below.

A block-list entry alone clears the `INCIDENT` threshold regardless of
any other signal (see `EntrySniffService`'s "Allow/block-list handling"
module docstring section) — a deterministic way to reach `INCIDENT` in a
test rather than stacking multiple weaker signals. `store.
list_recent_incidents(guild_id, limit=50)` and `store.list_sniff_receipts
(guild_id, member_id=None, limit=50)` (verified against their actual
signatures in `src/krubit/storage/sqlite.py:3670,3793` — neither takes a
`since` parameter; both return newest-first, capped by `limit`) are the
two read-back calls these tests need.

```python
@pytest.mark.asyncio
async def test_on_member_join_records_and_notifies_an_incident_for_incident_band(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=MEMBER_ID,
            list_kind="block",
            reason="test block entry",
            set_by=1,
            set_at=NOW,
        )
    )
    member = FakeMember(MEMBER_ID, guild)

    await rt.on_member_join(cast(discord.Member, member), NOW)

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert len(incidents) == 1
    assert incidents[0].kind is IncidentKind.MEMBER
    assert incidents[0].band is RiskBand.INCIDENT
    assert len(channel.sent) == 1
    assert "embed" in channel.sent[0]


@pytest.mark.asyncio
async def test_on_member_join_records_no_incident_for_a_watch_band(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)  # WATCH band, not INCIDENT

    await rt.on_member_join(cast(discord.Member, member), NOW)

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert incidents == ()
    assert channel.sent == ()


@pytest.mark.asyncio
async def test_on_member_join_incident_reflects_the_assessments_own_signals(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=MEMBER_ID,
            list_kind="block",
            reason="test block entry",
            set_by=1,
            set_at=NOW,
        )
    )
    member = FakeMember(MEMBER_ID, guild)

    await rt.on_member_join(cast(discord.Member, member), NOW)

    assessment = await store.get_entry_sniff_assessment(GUILD_ID, MEMBER_ID)
    assert assessment is not None
    receipts = await store.list_sniff_receipts(GUILD_ID)
    incident_receipt = next(r for r in receipts if r.action == "incident_recorded")
    signal_names = incident_receipt.detail["signal_names"]
    assert signal_names == [s.name for s in assessment.signals]


@pytest.mark.asyncio
async def test_two_separate_incident_band_joins_each_get_their_own_incident(
    store: SQLiteStore,
) -> None:
    """Proves the no-dedup Global Constraint: a second INCIDENT-band join
    while a first is still fresh is NOT suppressed, unlike the four
    existing guild-scoped detectors' same-kind cooldown."""
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    other_member_id = MEMBER_ID + 1
    for mid in (MEMBER_ID, other_member_id):
        await store.save_allow_block_entry(
            AllowBlockEntry(
                guild_id=GUILD_ID,
                discord_user_id=mid,
                list_kind="block",
                reason="test block entry",
                set_by=1,
                set_at=NOW,
            )
        )

    await rt.on_member_join(cast(discord.Member, FakeMember(MEMBER_ID, guild)), NOW)
    await rt.on_member_join(
        cast(discord.Member, FakeMember(other_member_id, guild)), NOW + timedelta(seconds=1)
    )

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert len(incidents) == 2
    assert len(channel.sent) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_watchdog_runtime.py -v -k "incident_band or own_incident or own_signals"`
Expected: FAIL — either an `AttributeError` if the storage lookup methods
don't exist as named (fix the test first per Step 1's note) or an
assertion failure (`incidents == []`, `channel.sent == ()`) since
`on_member_join` doesn't yet construct any `Incident` for an
`INCIDENT`-band assessment.

- [ ] **Step 3: Implement the change in `watchdog_runtime.py`**

Update the import line (currently `from krubit.domain.watchdog import
Incident, RiskBand`) to:

```python
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand, RiskSignal
```

Add this constant near the top of the file, after the existing
module-level constants (or create a constants section if none exists yet
— check the file for where `GuildLookup`/`GuildIds` type aliases sit,
around line 89-90, and add below those):

```python
_MEMBER_INCIDENT_RECOMMENDED_ACTION = (
    "Review this member's Entry Sniff assessment and signals; consider "
    "manual verification, timeout, or removal if warranted. No automatic "
    "action has been taken."
)


def _default_evidence_builder(
    guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
) -> str:
    del guild_id, signals, now
    return f"evidence:{uuid4().hex}"
```

Add the new constructor parameter. In `WatchdogRuntime.__init__`'s
signature (around line 124-141), add after the existing `now: Callable[
[], datetime] | None = None,` parameter:

```python
        evidence_builder: Callable[[int, tuple[RiskSignal, ...], datetime], str]
        | None = None,
```

And in the body (after `self._now = now or (lambda: datetime.now(UTC))`,
around line 157):

```python
        self._evidence_builder = evidence_builder or _default_evidence_builder
```

Modify `on_member_join` (currently lines 179-197):

```python
    async def on_member_join(self, member: discord.Member, now: datetime) -> None:
        """Assess a fresh join, open a bounded watch window when warranted, and --
        new in this task -- record a durable `Incident` and notify staff
        immediately when the assessment reaches `RiskBand.INCIDENT`.

        Exactly Task 4's original two calls, in order, unchanged: `EntrySniffService.
        assess_join` produces the one durable per-join assessment, then
        `WatchWindowService.open_if_warranted` opens a window for anything above
        `RiskBand.CLEAR`. The new third step is strictly additive and never skips or
        reorders either original call. No same-kind dedup is applied here (unlike the
        four sweep-cycle detectors' `_has_open_incident_of_kind` cooldown) -- each
        `INCIDENT`-band join is an independent member and independent evidence, so
        every one gets its own `Incident` and its own notification.
        """
        if not self._watchdog_enabled:
            return
        _require_aware("now", now)
        assessment = await self._entry_sniff.assess_join(member, now)
        window = await self._watch_window.open_if_warranted(assessment, now)
        if window is not None:
            self._watched_members[window.guild_id].add(window.member_id)
        if assessment.band is RiskBand.INCIDENT:
            await self._record_member_incident(assessment, now)

    async def _record_member_incident(self, assessment: EntrySniffAssessment, now: datetime) -> None:
        evidence_packet_id = self._evidence_builder(
            assessment.guild_id, assessment.signals, now
        )
        incident = Incident(
            guild_id=assessment.guild_id,
            incident_id=f"member:{uuid4().hex}",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=now,
            evidence_packet_id=evidence_packet_id,
            recommended_action=_MEMBER_INCIDENT_RECOMMENDED_ACTION,
            acknowledged_by=None,
        )
        saved = await self._store.record_incident(incident)
        detail: dict[str, JSONValue] = {
            "kind": saved.kind.value,
            "signal_names": [signal.name for signal in assessment.signals],
        }
        await self._store.record_sniff_receipt(
            guild_id=saved.guild_id,
            receipt_id=f"incident:{saved.incident_id}",
            member_id=assessment.member_id,
            action="incident_recorded",
            detail=detail,
            created_at=now,
        )
        await self.notify_staff(saved)
```

Note the `member_id=assessment.member_id` on the receipt: `RaidDetector.
_record_incident`'s equivalent receipt passes `member_id=None` (a raid is
guild-scoped, no single member). This is a deliberate, correct difference
for a member-kind incident, not an inconsistency to "fix."

You will need two more imports: `EntrySniffAssessment` (from
`krubit.domain.watchdog`, alongside the other watchdog domain imports)
and `JSONValue` (from `krubit.domain.models`, matching
`raid_detection.py`'s own import of it for the identical `detail: dict[
str, JSONValue]` annotation). Check whether `uuid4` is already imported
at the top of the file (it is, line 71) — no new import needed for that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_watchdog_runtime.py -v`
Expected: all PASS, including the pre-existing tests in this file
(confirm no regression to the three existing `on_member_join` tests or
any `notify_staff` test).

- [ ] **Step 5: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, no regressions (baseline before this task: 1164
passing).

- [ ] **Step 6: Lint and type-check**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/discord/watchdog_runtime.py tests/test_watchdog_runtime.py`
Expected: clean.

Run: `./.venv/Scripts/python.exe -m pyright src/krubit/discord/watchdog_runtime.py`
Expected: no new error category versus this file's existing baseline
(confirm via `git stash`/`git stash pop` diffing, per this project's
established verification convention for every prior task).

- [ ] **Step 7: Commit**

```bash
git add src/krubit/discord/watchdog_runtime.py tests/test_watchdog_runtime.py
git commit -m "feat: notify staff in real time for INCIDENT-band member joins"
```

---

## Final Verification

- [ ] Run the full suite once more: `./.venv/Scripts/python.exe -m pytest -q` — must show `1164 + N passed` where `N` is the number of new tests, zero failures.
- [ ] No live Discord verification is required for this plan to be
      considered complete — matching this project's established
      convention for Discord-layer changes verified by direct code trace
      plus the automated test suite.
- [ ] Confirm `docs/devlogs/2026-08-05-phase-3-watchdog.md`'s "Known
      limitations" item #1 ("A lone `INCIDENT`-band join is never
      notified in real time") is now stale — a follow-up devlog note (not
      required by this plan, but worth doing when this branch's own
      devlog is written) should reference this work as closing that gap.
