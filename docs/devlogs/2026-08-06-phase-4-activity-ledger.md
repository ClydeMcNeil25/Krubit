# Krubit Development Log: Phase 4 Member Activity Ledger

**Date:** August 6, 2026
**Status:** Automated implementation and verification through Task 9 complete. No
live Discord guild or credentialed environment was available in this development
session; see
[Known limitations](../operations/phase-4-activity-ledger.md#known-limitations-that-change-what-enabling-the-activity-ledger-actually-does)
in the operations guide for exactly what is and is not evidenced.

## Scope

This effort delivers Phase 4 (Member Activity Ledger: per-member factual
participation event ledger, activation/retention/trend calculation, staff and
self-service views, and privacy controls) per the
[Phase 4 Activity Ledger design](../superpowers/specs/2026-08-06-phase-4-activity-ledger-design.md).
Krubit gains deterministic, explainable participation measurement — never a
relationship-judgment, personality, loyalty, mental-health, or guilt label, and
never message content, audio, or DM ingestion.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | Domain model: `LedgerEvent` kinds, `MEANINGFUL_EVENT_KINDS`, deterministic `time_to_activation`/`cohort_membership`/`participation_trend` |
| 2 | Storage: `ledger_events`, `milestones`, `channel_exclusions`, `retention_policies`, `activity_receipts`, including `delete_member_ledger_data` |
| 3 | Structural pre-storage exclusion: `extract_*` functions (pure, content-free) and `ActivityIngestionService` (the sole exclusion gate) |
| 4 | Milestone materialization (`krubit.services.milestones`) |
| 5 | Views: `newcomer_view`, `inactive_view`, `returning_member_view`, `milestone_view`, `recognition_candidates`, `community_pulse_view` |
| 6 | Retention sweep, member deletion with a minimal receipt, member data export |
| 7 | Live gateway wiring: `ActivityRuntime`, `phase_four_intents()`, `Settings` enforcement at every real call site |
| 8 | Staff-only/staff-or-self `/fetch` command surface (`member`, `activity`, `newcomers`, `inactive`, `milestones`, `retention`, `community-pulse`) |
| 9 | Health integration, two structural privacy proofs, documentation (this task) |

## Task 9: this task's changes

- `src/krubit/services/health.py`: added `ActivityLedgerHealthFacts` (a frozen
  dataclass of `enabled`/`retention_configured`) and `activity_ledger=` keyword
  parameters on `HealthService.server_health`/`integration_health`, mirroring
  `WatchdogHealthFacts` exactly. Passing `None` (the default, and what every
  pre-Phase-4 caller still does) reports nothing about the activity ledger,
  preserving every existing test and call site unchanged; passing an explicit
  `ActivityLedgerHealthFacts` surfaces `activity_ledger_disabled` and/or
  `activity_ledger_retention_unconfigured` findings.
- `src/krubit/discord/bot.py`: wired real `Settings` values into
  `ActivityLedgerHealthFacts` at `FetchCommands`' production construction site and
  the daily-summary health-report call site, so `/fetch server-health`,
  `/fetch integrations`, and the once-daily staff-channel health summary all now
  report genuine activity-ledger capability facts rather than the facts existing
  only in tests. This wiring was not in the task brief's stated file list, but was
  added anyway — health facts defined but never passed into a production call site
  would themselves be exactly the "parsed but unenforced" bug pattern the design doc
  explicitly warns against; leaving them unwired would have undermined the honesty
  standard this task otherwise holds documentation to.
- `tests/test_activity_privacy_structural_safety.py`: the two structural privacy
  proofs the Completion Gate requires.
  - `test_excluded_channel_events_structurally_cannot_reach_storage` drives the
    real, unmodified `ActivityRuntime`/`ActivityIngestionService` production code
    against a counting spy store for a message, a reaction, a full voice
    join+leave session, and a Scheduled Event RSVP, all in a real excluded
    channel — asserting zero storage calls for the three channel-bearing kinds and
    confirming the channel-less attendance kind still flows through the identical
    single gate. `test_ingest_is_the_only_caller_of_record_ledger_event_in_src`
    source-scans every file under `src/krubit` and asserts exactly one call site
    exists for `SQLiteStore.record_ledger_event` anywhere in the codebase.
  - `test_activity_ledger_tables_matches_the_live_schema_exactly` queries
    `sqlite_master` for every table the live schema creates and cross-checks it
    against `ACTIVITY_LEDGER_TABLES` union an explicit, hand-enumerated list of
    every pre-Phase-4 table — catching a future Phase 4 table added to the schema
    without being added to `ACTIVITY_LEDGER_TABLES`, a gap the existing (Task 6)
    `test_all_member_scoped_tables_matches_live_schema` cannot see since it only
    scans tables already inside that list.
    `test_member_deletion_covers_every_table_the_schema_actually_defines` then
    derives `ALL_MEMBER_SCOPED_TABLES` from a live `PRAGMA table_info` scan and
    seeds+deletes a real member's data across every discovered table, asserting
    zero rows survive.
  - Both proofs passed on first run in this session, against an ingestion
    entry-point set and table set that were hand-verified by reading
    `KrubitBot`'s `self._activity_runtime.*` call sites and
    `SQLiteStore._initialize`'s schema directly — not derived from a filename
    glob — learning directly from Phase 3's Task 9 post-mortem (its original
    structural test covered only 5 of 10 real modules because it filtered by
    filename).
- `docs/operations/phase-4-activity-ledger.md` (new): the operator guide, modeled
  on `docs/operations/phase-3-watchdog.md`'s structure and honesty standard —
  exact env var names, the three new non-privileged intents (and the
  `phase_four_intents()`-defined-but-not-called code-organization gap), shadow
  mode, and eight documented known limitations, the three most significant given
  equal prominence to Phase 3's biggest gap: `returning_member_view` has zero
  production callers, `recognition_candidates` has the identical defect shape, and
  deletion/export/channel-exclusion configuration have no `/fetch` command surface
  at all.
- `README.md`, `.env.example`, `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`:
  updated to describe Phase 4 capabilities and link the new operations guide/
  devlog, consistently reflecting the same eight known limitations across all four
  documents (README, roadmap, operations guide, `.env.example`) rather than
  omitting them from any one.

## What this build honestly does and does not do

**Does:** records factual, content-free participation events (message, reaction,
voice-join/leave-duration, event RSVP, join, role change) for every guild with
`KRUBIT_ACTIVITY_LEDGER_ENABLED=true`; computes deterministic activation/retention/
trend measures reproducing the design doc's own fixtures exactly; structurally
guarantees an excluded channel's events never reach storage; structurally
guarantees member deletion covers every table the live schema defines; exposes
four of the design doc's six named views through five staff-only `/fetch` commands
plus two staff-or-self commands.

**Does not:** notify anyone proactively (every view in this phase is a pull, not a
push — there is no autonomous-notification path at all in Phase 4, unlike Phase
3's staff-notification flag); give staff any command to view a returning member,
view a recognition-candidate shortlist, delete a member's data, export a member's
data, or configure a channel exclusion (all five capabilities are fully implemented
and tested at the service/storage layer, reachable only via direct database or
Python access today); or apply the
`KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS` env var to storage (parsed and
validated, never seeded — a deliberate Task 7 decision, documented as Gap 3 in the
operations guide).

## Verification run in this session

```powershell
.venv\Scripts\python.exe -m pytest -q            # 993 passed
.venv\Scripts\ruff.exe check .                   # All checks passed
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe
                                                  # 0 errors, 0 warnings, 0 informations
git diff --check                                 # exit 0
```

No live Discord guild, staff channel, or credentialed environment was available in
this session — every claim above is evidenced by automated test output, source
reading, and direct grep, never by manual runtime observation. Nothing in this
devlog or the operations guide describes behavior that was only assumed to work.
