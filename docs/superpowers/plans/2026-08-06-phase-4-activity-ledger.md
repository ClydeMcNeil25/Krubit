# Krubit Phase 4 Member Activity Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Krubit a factual, content-free member activity ledger, deterministic
activation/retention calculation, staff and self-service views, and complete privacy
controls (exclusion, retention, deletion, export) — measurement only, matching the
Phase 4 design spec.

**Architecture:** Guild-scoped, DM-excluded, channel-exclusion-filtered ingestion
writes factual events (never content) to an append-only ledger; pure functions
compute activation/retention/trend/milestone facts from stored events; staff-only and
member-self-service views read those facts; privacy controls (exclusion enforced
before storage, retention sweep, deletion, export) are first-class, tested
capabilities, not afterthoughts.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite 0.22.1, pytest 9,
pytest-asyncio 1.4.0, Ruff, Pyright strict — same stack as Phases 2/3, no new
dependencies expected.

## Global Constraints

- No DM ingestion for analytics, ever — every ingestion entry point checks
  `guild is None` and excludes DMs, matching Phase 3's double-gate discipline.
- No voice content recording — only join/leave timestamps and computed duration.
- No personality, loyalty, mental-health, or guilt labels assigned to any member,
  anywhere in this codebase.
- Channel exclusion is enforced **before storage** — an excluded channel's events
  must never reach a storage call, verified structurally (a test that traces the
  actual ingestion code path), not just behaviorally.
- Every event/aggregate table is guild-scoped with `guild_id` as the leading key,
  matching the established `sqlite.py` convention.
- Cohort/activation/retention calculations are pure functions over stored data —
  deterministic, reproducible from fixtures, with named/explainable rules (no
  black-box scores).
- Member deletion removes every derived record — the plan must enumerate every
  table a member's ID can appear in and prove all are cleared, not just the primary
  ledger table.
- Detailed per-member profiles are staff-only; the self-view exposes only the
  caller's own data, never comparison to others or guild-wide aggregates.
- `KRUBIT_ACTIVITY_LEDGER_ENABLED` must be genuinely enforced at every actual
  ingestion/query/command call site, not just parsed at construction — this exact
  class of bug (a flag parsed but not enforced at the real boundary) was found and
  fixed in both Phase 2's and Phase 3's final whole-branch reviews. Do not repeat it.
- Krubit surfaces facts only — it never decides who deserves recognition, drafts
  recognition wording, or interprets sentiment. That is explicitly Zariya's role.

## File Structure

### New production modules

- `src/krubit/domain/activity_ledger.py`: `LedgerEventKind` (StrEnum), `LedgerEvent`
  value objects per kind (join, onboarding, message, reaction, voice_session,
  event_attendance, role_change, milestone, moderation_receipt), `MilestoneKind`,
  `Milestone`, `CohortWindow`, `CohortResult`, `ParticipationTrend`,
  `ActivationResult`, `ExclusionEntry`, `RetentionPolicy`.
- `src/krubit/services/activity_ingestion.py`: pure extraction functions
  (`extract_message_event`, `extract_reaction_event`, `extract_voice_session_event`,
  `extract_attendance_event`) plus `ActivityIngestionService` — the channel-exclusion
  gate lives here, checked before any storage call.
- `src/krubit/services/activation_retention.py`: pure calculation functions
  (`time_to_activation`, `cohort_membership`, `participation_trend`) — no I/O, no
  Discord objects, testable against fixtures alone.
- `src/krubit/services/milestones.py`: milestone-rule evaluation
  (`evaluate_milestones(member_id, guild_id, now) -> tuple[Milestone, ...]`) and
  recognition-candidate shortlisting.
- `src/krubit/services/activity_views.py`: newcomer/inactive/returning/
  community-pulse view queries — read-side aggregation over the ledger.
- `src/krubit/services/activity_privacy.py`: retention sweep, member deletion
  (enumerates and clears every derived table), export packaging.
- `src/krubit/discord/activity_events.py`: pure Discord-object-to-domain-object
  extraction (mirrors `discord/watchdog_events.py`'s pattern) for reaction/voice/
  scheduled-event-attendance/role-change Discord payloads.
- `src/krubit/discord/activity_runtime.py`: wires gateway events into the ingestion
  service, runs the retention-sweep loop (per-guild/per-table isolation, mirroring
  `ConnectorScheduler`'s and Phase 3's `sweep_cycle` isolation discipline), and
  enforces `activity_ledger_enabled` at every real call site.
- `src/krubit/discord/activity_commands.py`: `/fetch member`, `/fetch activity`,
  `/fetch newcomers`, `/fetch inactive`, `/fetch milestones`, `/fetch retention`,
  `/fetch community-pulse`.

### Existing modules to modify

- `src/krubit/config.py`: add `KRUBIT_ACTIVITY_LEDGER_ENABLED`, a channel-exclusion
  list setting (first list-of-IDs config field in this codebase — establish the
  parsing convention carefully, e.g. comma-separated), retention-window-days,
  inactivity-threshold-days — all optional, safe defaults.
- `src/krubit/discord/install.py`: add `phase_four_intents()` requesting
  `guild_reactions`, `voice_states`, `guild_scheduled_events` (all non-privileged,
  additive to `phase_three_intents()`).
- `src/krubit/storage/sqlite.py`: additive Phase 4 schema and guild-scoped
  persistence methods; move row-decoding helpers into
  `src/krubit/storage/activity_ledger_rows.py`, matching the `creator_rows.py`/
  `watchdog_rows.py` precedent.
- `src/krubit/discord/bot.py`: register new gateway listeners (`on_reaction_add`,
  `on_reaction_remove`, `on_voice_state_update`, `on_scheduled_event_user_add`,
  `on_scheduled_event_user_remove`) and extend the existing `on_message`/
  `on_member_join`/`on_member_remove`/`on_member_update` handlers with a sibling
  call into the new activity runtime, following the exact pattern
  `watchdog_runtime.on_message` already established alongside the existing handler.
- `src/krubit/services/health.py`: surface activity-ledger capability state.
- `README.md`, `.env.example`, `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`:
  operator instructions and roadmap status.

---

### Task 1: Activity ledger domain model and pure calculation functions

**Files:**
- Create: `src/krubit/domain/activity_ledger.py`
- Create: `src/krubit/services/activation_retention.py`
- Test: `tests/test_activity_ledger_domain.py`
- Test: `tests/test_activation_retention_calculation.py`

**Interfaces:**
- Produces: `LedgerEventKind`, `LedgerEvent` variants, `MilestoneKind`, `Milestone`,
  `CohortWindow`, `CohortResult`, `ParticipationTrend`, `ActivationResult`,
  `ExclusionEntry`, `RetentionPolicy`, `time_to_activation(join_at, events) ->
  ActivationResult`, `cohort_membership(joins, events, window) -> CohortResult`,
  `participation_trend(events, window, inactivity_threshold) -> ParticipationTrend`.
- Consumes: no Phase 4 interfaces yet.

- [ ] **Step 1: Write failing domain and calculation tests**

```python
def test_time_to_activation_finds_first_meaningful_action_after_join() -> None:
    join_at = AWARE_NOW
    events = (
        ledger_event(kind=LedgerEventKind.MESSAGE, occurred_at=join_at + timedelta(hours=3)),
        ledger_event(kind=LedgerEventKind.REACTION, occurred_at=join_at + timedelta(hours=1)),
    )
    result = time_to_activation(join_at, events)
    assert result.activated is True
    assert result.time_to_activation == timedelta(hours=1)


def test_time_to_activation_with_no_meaningful_events_reports_not_activated() -> None:
    result = time_to_activation(AWARE_NOW, ())
    assert result.activated is False
    assert result.time_to_activation is None


def test_cohort_membership_reproduces_a_known_fixture() -> None:
    result = cohort_membership(joins=KNOWN_JOIN_FIXTURE, events=KNOWN_EVENT_FIXTURE, window=CohortWindow.SEVEN_DAY)
    assert result.retained_count == 6
    assert result.cohort_size == 10
    assert result.retention_rate == pytest.approx(0.6)
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_ledger_domain.py tests\test_activation_retention_calculation.py -q`

Expected: FAIL during import because the new modules do not exist.

- [ ] **Step 3: Implement frozen enums/value objects and deterministic calculation**

Follow the `@dataclass(frozen=True, slots=True)` + `__post_init__` validation
convention from `domain/watchdog.py`/`domain/creator_signals.py`. Define the
"meaningful action" event-kind set as a named, documented constant (not inline
logic scattered across call sites), matching Phase 3's rigor for documenting
safety/measurement-relevant thresholds. Cohort-window date-boundary handling must
use the same half-open-interval discipline Phase 3's quiet-hours logic used, with
explicit boundary tests (join day inclusive, window-end day inclusive).

- [ ] **Step 4: Run focused tests, Ruff, and Pyright**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_ledger_domain.py tests\test_activation_retention_calculation.py -q`

Run: `.venv\Scripts\ruff.exe check src\krubit\domain\activity_ledger.py src\krubit\services\activation_retention.py tests\test_activity_ledger_domain.py tests\test_activation_retention_calculation.py`

Run: `.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the domain model**

```powershell
git add src/krubit/domain/activity_ledger.py src/krubit/services/activation_retention.py tests/test_activity_ledger_domain.py tests/test_activation_retention_calculation.py
git commit -m "feat: add activity ledger domain model and retention calculation"
```

### Task 2: Activity ledger storage schema and guild-scoped persistence

**Files:**
- Create: `src/krubit/storage/activity_ledger_rows.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_activity_ledger_storage.py`

**Interfaces:**
- Consumes: value objects from Task 1.
- Produces: `SQLiteStore.record_ledger_event`, `.list_ledger_events`,
  `.save_milestone`, `.list_milestones`, `.save_exclusion_entry`,
  `.list_exclusion_entries`, `.save_retention_policy`, `.get_retention_policy`,
  `.delete_member_ledger_data`, `.record_activity_receipt`.

- [ ] **Step 1: Write failing storage tests for isolation and idempotency**

```python
@pytest.mark.asyncio
async def test_ledger_events_are_guild_scoped(store: SQLiteStore) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.record_ledger_event(ledger_event(guild_id=999, member_id=222))
    assert len(await store.list_ledger_events(111, member_id=222)) == 1
    assert len(await store.list_ledger_events(999, member_id=222)) == 1


@pytest.mark.asyncio
async def test_delete_member_ledger_data_removes_events_and_milestones(store: SQLiteStore) -> None:
    await store.record_ledger_event(ledger_event(guild_id=111, member_id=222))
    await store.save_milestone(milestone(guild_id=111, member_id=222))
    await store.delete_member_ledger_data(111, 222)
    assert await store.list_ledger_events(111, member_id=222) == ()
    assert await store.list_milestones(111, member_id=222) == ()
```

- [ ] **Step 2: Run focused tests and confirm missing persistence methods**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_ledger_storage.py -q`

Expected: FAIL because the ledger tables and methods are absent.

- [ ] **Step 3: Add additive schema and guild-scoped methods**

Add tables for each event kind (or a polymorphic table with a `kind` discriminant —
pick whichever fits `sqlite.py`'s existing conventions better; document the choice),
`milestones`, `channel_exclusions`, `retention_policies`, `activity_receipts`. Every
table leads with `guild_id`. `delete_member_ledger_data` must delete from every one
of these tables in a single transaction — list every table explicitly in the
implementation and in a comment, so a future added table is an obvious place to
update this method. Move row-decoding into `storage/activity_ledger_rows.py`.

- [ ] **Step 4: Run storage and existing regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_ledger_storage.py tests\test_storage.py tests\test_watchdog_storage.py -q`

Expected: PASS with no changes to existing Phase 0-3 records.

- [ ] **Step 5: Commit storage**

```powershell
git add src/krubit/storage/activity_ledger_rows.py src/krubit/storage/sqlite.py tests/test_activity_ledger_storage.py
git commit -m "feat: persist activity ledger events, milestones, and privacy settings"
```

### Task 3: Ingestion service with channel-exclusion enforcement

**Files:**
- Create: `src/krubit/discord/activity_events.py`
- Create: `src/krubit/services/activity_ingestion.py`
- Test: `tests/test_activity_event_extraction.py`
- Test: `tests/test_activity_ingestion_service.py`

**Interfaces:**
- Consumes: Task 1/2 interfaces.
- Produces: `extract_message_event(message, now)`, `extract_reaction_event(payload,
  now)`, `extract_voice_session_event(before, after, now)`,
  `extract_attendance_event(payload, now)` (all pure, no I/O), and
  `ActivityIngestionService.ingest(event) -> bool` (returns whether it was actually
  stored — `False` for an excluded channel or DM).

- [ ] **Step 1: Write failing extraction and exclusion-enforcement tests**

```python
def test_extract_message_event_ignores_dms() -> None:
    assert extract_message_event(dm_message(), now=NOW) is None


def test_extract_message_event_carries_no_content() -> None:
    event = extract_message_event(message(content="secret stuff"), now=NOW)
    assert not hasattr(event, "content")


@pytest.mark.asyncio
async def test_excluded_channel_event_never_reaches_storage(store: SQLiteStore) -> None:
    await store.save_exclusion_entry(exclusion_entry(guild_id=111, channel_id=555))
    service = ActivityIngestionService(store)
    stored = await service.ingest(ledger_event(guild_id=111, channel_id=555))
    assert stored is False
    assert await store.list_ledger_events(111) == ()
```

- [ ] **Step 2: Run focused tests and confirm missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_event_extraction.py tests\test_activity_ingestion_service.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement extraction and the pre-storage exclusion gate**

`extract_*` functions read only already-available Discord fields, never message
content, and return `None` for DMs (mirroring `watchdog_events.py`'s pattern).
`ActivityIngestionService.ingest` checks the guild's exclusion list **before**
calling any storage write method — this is the structural property Task 9's final
proof will verify, so get the ordering right here: the exclusion check must happen
in a code path that makes it structurally impossible to reach the storage call for
an excluded channel, not just a behaviorally-tested convention.

- [ ] **Step 4: Run ingestion, extraction, and regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_event_extraction.py tests\test_activity_ingestion_service.py tests\test_activity_ledger_storage.py -q`

Expected: PASS.

- [ ] **Step 5: Commit ingestion**

```powershell
git add src/krubit/discord/activity_events.py src/krubit/services/activity_ingestion.py tests/test_activity_event_extraction.py tests/test_activity_ingestion_service.py
git commit -m "feat: add content-free activity ingestion with pre-storage exclusion"
```

### Task 4: Milestone evaluation and recognition-candidate shortlisting

**Files:**
- Create: `src/krubit/services/milestones.py`
- Modify: `src/krubit/storage/sqlite.py` (if a helper query is genuinely needed)
- Test: `tests/test_milestones.py`

**Interfaces:**
- Consumes: Task 1-3 interfaces.
- Produces: `evaluate_milestones(member_id, guild_id, events, now) -> tuple[Milestone,
  ...]`, `recognition_candidates(guild_id, events, window, now) ->
  tuple[RecognitionCandidate, ...]`.

- [ ] **Step 1: Write failing milestone and recognition tests**

```python
def test_message_count_milestone_fires_at_configured_threshold() -> None:
    events = tuple(ledger_event(kind=LedgerEventKind.MESSAGE) for _ in range(100))
    milestones = evaluate_milestones(member_id=1, guild_id=111, events=events, now=NOW)
    assert any(m.kind is MilestoneKind.MESSAGE_COUNT_100 for m in milestones)


def test_recognition_candidates_are_facts_not_a_score() -> None:
    candidates = recognition_candidates(111, events=FIXTURE_EVENTS, window=CohortWindow.THIRTY_DAY, now=NOW)
    assert all(c.reasons for c in candidates)  # every candidate names its reasons
```

- [ ] **Step 2: Run focused tests and confirm missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_milestones.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement named, explainable milestone rules**

Each milestone rule is a named, documented, pure function of the event stream —
message-count thresholds, join anniversaries, first-voice-session, first-event-
attended, etc. `recognition_candidates` returns factual reasons (which milestones,
which trend facts) — never a numeric "worthiness score" and never drafted
recognition text, per the design doc's explicit boundary with Zariya's role.

- [ ] **Step 4: Run milestone tests and regression suite**

Run: `.venv\Scripts\python.exe -m pytest tests\test_milestones.py tests\test_activation_retention_calculation.py -q`

Expected: PASS.

- [ ] **Step 5: Commit milestones**

```powershell
git add src/krubit/services/milestones.py tests/test_milestones.py
git commit -m "feat: add milestone evaluation and recognition-candidate shortlisting"
```

### Task 5: Newcomer, inactive, returning, and community-pulse views

**Files:**
- Create: `src/krubit/services/activity_views.py`
- Modify: `src/krubit/storage/sqlite.py` (read-side query methods)
- Test: `tests/test_activity_views.py`

**Interfaces:**
- Consumes: Task 1-4 interfaces.
- Produces: `newcomer_view(guild_id, recent_window, now)`, `inactive_view(guild_id,
  inactivity_threshold, now)`, `returning_member_view(guild_id, inactivity_threshold,
  now)`, `community_pulse(guild_id, window, now)`.

- [ ] **Step 1: Write failing view tests against real storage**

```python
@pytest.mark.asyncio
async def test_inactive_view_excludes_members_who_left(store: SQLiteStore) -> None:
    await seed_member(store, guild_id=111, member_id=1, left=True, last_active_days_ago=60)
    await seed_member(store, guild_id=111, member_id=2, left=False, last_active_days_ago=60)
    view = await inactive_view(store, 111, inactivity_threshold=timedelta(days=30), now=NOW)
    assert [m.member_id for m in view] == [2]
```

- [ ] **Step 2: Run focused tests and confirm missing views**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_views.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement guild-scoped, fact-only view queries**

Every view is guild-scoped and reads only stored facts — no sentiment, no
cross-member comparison beyond what the view's stated purpose requires (e.g.
`community_pulse` is guild-wide by design; `newcomer_view` never exposes one
newcomer's data to another).

- [ ] **Step 4: Run view and regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_views.py tests\test_milestones.py -q`

Expected: PASS.

- [ ] **Step 5: Commit views**

```powershell
git add src/krubit/services/activity_views.py src/krubit/storage/sqlite.py tests/test_activity_views.py
git commit -m "feat: add newcomer, inactive, returning, and community-pulse views"
```

### Task 6: Retention sweep, member deletion, and export

**Files:**
- Create: `src/krubit/services/activity_privacy.py`
- Modify: `src/krubit/storage/sqlite.py` (if needed beyond Task 2's
  `delete_member_ledger_data`)
- Test: `tests/test_activity_privacy.py`

**Interfaces:**
- Consumes: Task 1-2 interfaces.
- Produces: `RetentionSweepService.sweep(guild_id, now)`, `delete_member(guild_id,
  member_id, requested_by, now) -> ActivityReceipt`, `export_member_data(guild_id,
  member_id, now) -> MemberExportPackage`.

- [ ] **Step 1: Write failing privacy-control tests**

```python
@pytest.mark.asyncio
async def test_deletion_removes_every_derived_table(store: SQLiteStore) -> None:
    await seed_full_member_footprint(store, guild_id=111, member_id=222)  # events, milestones, everything
    await delete_member(store, 111, 222, requested_by=999, now=NOW)
    for table in ALL_MEMBER_SCOPED_TABLES:
        assert await table_row_count(store, table, guild_id=111, member_id=222) == 0


@pytest.mark.asyncio
async def test_export_never_includes_another_members_data(store: SQLiteStore) -> None:
    await seed_ledger_event(store, guild_id=111, member_id=222)
    await seed_ledger_event(store, guild_id=111, member_id=333)
    package = await export_member_data(store, 111, 222, now=NOW)
    assert all(e.member_id == 222 for e in package.events)
```

- [ ] **Step 2: Run focused tests and confirm missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_privacy.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement deletion completeness and sweep isolation**

`ALL_MEMBER_SCOPED_TABLES` (or equivalent) must be an explicit, tested list matching
Task 2's schema exactly — this list is the single most important artifact in this
task; a test should fail loudly if a future table is added to `sqlite.py` without
being added here (e.g. by cross-checking against the schema's actual table list at
test time, not just a hardcoded Python list that could silently drift). The
retention sweep isolates one guild's/table's failure from another's, mirroring
`ConnectorScheduler`'s and Phase 3's `sweep_cycle` isolation discipline exactly —
including a real concurrent-failure test, not just an assertion.

- [ ] **Step 4: Run privacy-control and regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_privacy.py tests\test_activity_ledger_storage.py -q`

Expected: PASS.

- [ ] **Step 5: Commit privacy controls**

```powershell
git add src/krubit/services/activity_privacy.py src/krubit/storage/sqlite.py tests/test_activity_privacy.py
git commit -m "feat: add retention sweep, member deletion, and data export"
```

### Task 7: Gateway wiring, intents, and settings enforcement

**Files:**
- Modify: `src/krubit/discord/install.py`
- Modify: `src/krubit/config.py`
- Create: `src/krubit/discord/activity_runtime.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_discord_install.py`
- Test: `tests/test_activity_runtime.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: Task 1-6 interfaces.
- Produces: `phase_four_intents()`, `Settings.activity_ledger_enabled` + exclusion/
  retention/inactivity settings, `ActivityRuntime.on_message(message, now)`,
  `.on_reaction_add/remove(payload, now)`, `.on_voice_state_update(member, before,
  after, now)`, `.on_scheduled_event_user_add/remove(payload, now)`,
  `.sweep_cycle(now)`.

- [ ] **Step 1: Write failing intent, settings, and gating tests**

```python
def test_phase_four_intents_adds_reactions_voice_and_events_to_phase_three() -> None:
    intents = phase_four_intents()
    assert intents.guild_reactions is True
    assert intents.voice_states is True
    assert intents.guild_scheduled_events is True
    assert intents.message_content is True  # inherited from phase_three_intents()


def test_activity_ledger_settings_default_disabled() -> None:
    settings = Settings.from_env(base_env())
    assert settings.activity_ledger_enabled is False


@pytest.mark.asyncio
async def test_ingestion_is_a_genuine_noop_when_ledger_disabled(runtime) -> None:
    await runtime.on_message(message(), now=NOW)
    assert runtime.store.ledger_event_count == 0
```

- [ ] **Step 2: Run focused tests and confirm missing settings/gating**

Run: `.venv\Scripts\python.exe -m pytest tests\test_discord_install.py tests\test_activity_runtime.py tests\test_config.py -q`

Expected: FAIL.

- [ ] **Step 3: Wire the runtime with the flag genuinely enforced at every call site**

Every `ActivityRuntime` method checks `activity_ledger_enabled` as its first
statement — matching `WatchdogRuntime`'s established pattern exactly (that pattern
was itself hardened by a Phase 3 final-review finding; do not regress it here).
`bot.py`'s existing handlers (`on_message`, `on_member_join`, `on_member_remove`,
`on_member_update`) each gain a sibling call into the new runtime, following the
exact pattern already used for `watchdog_runtime`'s sibling call in `on_message`.

- [ ] **Step 4: Run runtime, install, config, and full regression suite**

Run: `.venv\Scripts\python.exe -m pytest tests\test_discord_install.py tests\test_activity_runtime.py tests\test_config.py tests\test_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit runtime wiring**

```powershell
git add src/krubit/discord/install.py src/krubit/config.py src/krubit/discord/activity_runtime.py src/krubit/discord/bot.py tests/test_discord_install.py tests/test_activity_runtime.py tests/test_config.py
git commit -m "feat: wire activity ledger runtime behind explicit settings flag"
```

### Task 8: `/fetch` command surface

**Files:**
- Create: `src/krubit/discord/activity_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_activity_commands.py`

**Interfaces:**
- Consumes: Task 1-7 interfaces.
- Produces: `/fetch member`, `/fetch activity`, `/fetch newcomers`, `/fetch
  inactive`, `/fetch milestones`, `/fetch retention`, `/fetch community-pulse` —
  staff-only except `/fetch activity`/`/fetch milestones` self-view for the calling
  member's own data.

- [ ] **Step 1: Write failing command authorization and self-view tests**

```python
@pytest.mark.asyncio
async def test_non_staff_member_is_denied_another_members_profile(commands) -> None:
    result = await commands.member(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_member_can_view_their_own_activity_self_view(commands) -> None:
    result = await commands.activity(actor=regular_member(), target=regular_member())
    assert result.status is CommandStatus.OK
    assert "other member" not in result.card.description  # no cross-member comparison
```

- [ ] **Step 2: Run focused tests and confirm missing command surface**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_commands.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement authority-before-query commands**

Every command checks authority BEFORE any query/render, matching the pattern Phase
2 and Phase 3 both had to fix as review findings when a command skipped this
ordering. Self-view commands (`/fetch activity`, `/fetch milestones` when the
target is the caller) render a genuinely reduced view — no guild-wide comparison,
no other member's data reachable even by argument manipulation (verify the target
member ID is re-validated against the caller's own ID for the self-view path, not
merely defaulted to it in the UI).

- [ ] **Step 4: Run command, bot, and existing `/fetch` regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_commands.py tests\test_watchdog_commands.py tests\test_content_commands.py tests\test_live_signal_commands.py -q`

Expected: PASS with no command-name collisions.

- [ ] **Step 5: Commit commands**

```powershell
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py
git commit -m "feat: add member activity and retention commands"
```

### Task 9: Health integration, structural privacy proof, and documentation

**Files:**
- Modify: `src/krubit/services/health.py`
- Create: `tests/test_activity_privacy_structural_safety.py`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`
- Create: `docs/operations/phase-4-activity-ledger.md`
- Create: `docs/devlogs/2026-08-06-phase-4-activity-ledger.md`
- Test: `tests/test_health_service.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: activity-ledger capability facts in health output, and the structural
  proof the Completion Gate requires.

- [ ] **Step 1: Write the failing structural privacy tests first**

```python
def test_excluded_channel_events_structurally_cannot_reach_storage() -> None:
    # Trace the actual call graph from every Discord event handler through to
    # storage; assert the exclusion check is unconditionally on that path — not a
    # regex/grep proxy test, but one that exercises the real code with a real
    # excluded channel and asserts zero storage calls occurred (e.g. via a
    # spy/counting fake store), covering every ingestion entry point (message,
    # reaction, voice, attendance), not just one.


def test_member_deletion_covers_every_table_the_schema_actually_defines() -> None:
    # Cross-check ALL_MEMBER_SCOPED_TABLES (Task 6) against the live schema's
    # actual table list (e.g. via sqlite_master or an equivalent introspection),
    # not a hardcoded Python list that could silently drift from the real schema.
```

Learn directly from Phase 3's final-review finding: its original structural
safety test only covered 5 of 10 real modules because it filtered by filename
rather than by actual membership in the feature. Do not repeat that mistake here —
verify coverage against the real, current set of ingestion entry points and the
real, current schema, not an assumed/hardcoded list, or if a hardcoded list is
used, add a staleness guard proving the list still matches reality.

- [ ] **Step 2: Run the structural tests and confirm they currently pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_activity_privacy_structural_safety.py -q`

Expected: PASS if Tasks 1-8 were built correctly; if either FAILS, stop and fix the
underlying gap before proceeding — these are the two most important checks in the
whole phase.

- [ ] **Step 3: Add health facts and write the operator guide**

Follow `docs/operations/phase-3-watchdog.md`'s structure and honesty standard: exact
env var names, the non-privileged intents this phase adds, shadow-mode explanation,
and an explicit, code-verified statement of what the enable flag actually gates.
Document any known limitation honestly (e.g. if the retention-sweep aggregate-vs-
raw-row distinction from the design doc ends up simplified during implementation,
say so plainly rather than implying full fidelity).

- [ ] **Step 4: Run full automated verification**

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\ruff.exe check .`

Run: `.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe`

Run: `git diff --check`

Expected: all tests pass; Ruff and Pyright report zero findings; diff check exits 0.

- [ ] **Step 5: Commit documentation and health integration**

```powershell
git add src/krubit/services/health.py tests/test_activity_privacy_structural_safety.py tests/test_health_service.py README.md .env.example docs/roadmaps/2026-08-03-krubit-phase-rollout.md docs/operations/phase-4-activity-ledger.md docs/devlogs/2026-08-06-phase-4-activity-ledger.md
git commit -m "docs: close Phase 4 activity ledger"
```

## Final Integration Gate

- [ ] Re-read `docs/superpowers/specs/2026-08-06-phase-4-activity-ledger-design.md`
  line by line and confirm every Completion Gate item has direct evidence,
  especially the two structural proofs from Task 9.
- [ ] Confirm `git status --short` contains no unintended or secret-bearing files.
- [ ] Confirm exactly one Krubit process tree is running from the reviewed build.
- [ ] Use `superpowers:verification-before-completion` before any completion claim.
- [ ] Use `superpowers:finishing-a-development-branch` and let the user choose local
  merge, PR, or branch preservation.
- [ ] Devlog, commit, and push only when explicitly requested.
