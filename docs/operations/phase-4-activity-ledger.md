# Phase 4 Member Activity Ledger — Operations Guide

This guide covers the Phase 4 Member Activity Ledger build (Tasks 1-9 of the
[Phase 4 Activity Ledger design](../superpowers/specs/2026-08-06-phase-4-activity-ledger-design.md)):
the per-member factual participation event ledger, activation/retention/trend
calculation, staff and self-service views, the `/fetch` command surface, and
privacy controls (channel exclusion, retention, deletion, export).

> **Read this before enabling anything in this guide.** Krubit records **facts**
> about participation (that a member posted, reacted, joined voice, RSVPed, joined,
> or had a role change — never message content, never audio) and calculates
> deterministic, explainable measures from them. It never assigns a personality,
> loyalty, mental-health, or guilt label, and it never compares one member's data to
> another's in that member's own self-view. Three known gaps change what "enabled"
> actually means for an operator; the most important is that **two of the design
> doc's six named views have zero production caller anywhere in this nine-task
> plan** —
> [returning-member data](#gap-1-returning-member-data-is-invisible-to-staff-through-any-fetch-command-the-single-biggest-functional-gap)
> and
> [recognition-candidate data](#gap-2-recognition-candidate-data-is-invisible-to-staff-through-any-fetch-command--the-identical-defect-shape-as-gap-1)
> are both fully built, fully tested, and invisible to staff through any command —
> and the third is
> [deletion, export, and channel-exclusion configuration have no staff-facing command at all](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only)
> — all three are called out prominently below, not buried.

## What this build adds

- **Event ledger** (`ledger_events`): one append-only, guild-scoped row per
  participation event kind — `join`, `onboarding`, `message` (channel + timestamp
  only, never text), `reaction` (channel + emoji shape, never inferred sentiment),
  `voice_session` (join/leave timestamps + computed duration, never audio),
  `event_attendance` (Scheduled Event RSVP add/remove), `role_change`, `milestone`
  (materialized), and `moderation_receipt` (a redacted pointer into Watchdog's own
  receipts, not a duplicate copy of their content).
- **Activation/retention/trend calculation** (`krubit.services.activation_retention`):
  pure, deterministic functions — `time_to_activation`, `cohort_membership` (7-day
  and 30-day cohorts), `participation_trend` — reproducing the design doc's own
  worked fixtures exactly, with inclusive-both-ends day-boundary discipline
  (matching Phase 3's quiet-hours half-open-interval precedent).
- **Views**: `newcomer_view`, `inactive_view`, `returning_member_view`,
  `milestone_view`, and `community_pulse_view` (`krubit.services.activity_views`),
  plus `recognition_candidates` (`krubit.services.milestones`) — six views total,
  matching the design doc's Views section exactly. See
  [Gap 1](#gap-1-returning-member-data-is-invisible-to-staff-through-any-fetch-command-the-single-biggest-functional-gap)
  and
  [Gap 2](#gap-2-recognition-candidate-data-is-invisible-to-staff-through-any-fetch-command--the-identical-defect-shape-as-gap-1)
  for why two of these six have no command.
- **Staff-only and staff-or-self `/fetch` commands**: `/fetch member <member>`,
  `/fetch activity [member]`, `/fetch newcomers`, `/fetch inactive`,
  `/fetch milestones [member]`, `/fetch retention`, `/fetch community-pulse` — all
  ephemeral. `member`/`newcomers`/`inactive`/`retention`/`community-pulse` are
  staff-only (Manage Guild); `activity`/`milestones` additionally allow a caller to
  view their own data, re-validated against the caller's own ID inside the service
  layer (not merely defaulted in the Discord-layer UI), matching Phase 2 and Phase
  3's "authority checked before any query" review finding.
- **Health integration**: `/fetch server-health` and `/fetch integrations` report
  activity-ledger capability facts (enabled/disabled, default-retention-window
  configured/unconfigured) via `HealthService.server_health`/`integration_health`'s
  new `activity_ledger=` parameter
  (`src/krubit/services/health.py::ActivityLedgerHealthFacts`).
- **Privacy controls, built and tested but not all reachable from a command** — see
  [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only):
  channel exclusion (`channel_exclusions`, enforced structurally before storage —
  see below), a retention sweep (`RetentionSweepService`, prunes only raw
  `ledger_events` rows older than a guild's configured window; materialized
  `milestones`/`activity_receipts` are never pruned by the sweep, matching the
  design doc's explicit "the raw event, not the fact that a milestone was reached,
  ages out" rule), member deletion (`delete_member`, removes every member-scoped
  row across `ledger_events`/`milestones`/`activity_receipts` and writes one
  minimal, content-free deletion receipt — `requested_by` and `deleted_at` only,
  nothing describing what was deleted), and export (`export_member_data`, a
  member-scoped, uncapped package of that member's own events and milestones).

None of this is reachable in production until an operator opts in. One flag gates
it, fully enforced at every real ingestion call site, not just parsed:

```dotenv
KRUBIT_ACTIVITY_LEDGER_ENABLED=false
```

`KRUBIT_ACTIVITY_LEDGER_ENABLED=false` means every public method on
`ActivityRuntime` (`on_message`, `on_reaction_add`, `on_reaction_remove`,
`on_voice_state_update`, `on_scheduled_event_user_add`,
`on_scheduled_event_user_remove`, `on_member_join`, `on_member_remove`,
`on_member_update`, `sweep_cycle`) returns immediately as its first statement
(`src/krubit/discord/activity_runtime.py`) — no extraction, no storage read, no
storage write, no Discord API call. The `/fetch` command surface itself is **not**
gated by this flag — it is always registered and simply reports empty/near-empty
results (no events, no milestones, no cohort data) if the ledger was never enabled
to produce data in the first place, exactly matching Phase 3 Watchdog's
`/fetch sniff`-family precedent.

## Environment variables (exact names, matching `src/krubit/config.py`)

| Variable | Required | Purpose |
|---|---|---|
| `KRUBIT_ACTIVITY_LEDGER_ENABLED` | Optional, default `false` | Master ingestion flag — message/reaction/voice/attendance/join/role-change ingestion and the retention sweep |
| `KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS` | Optional, comma-separated positive snowflakes | **Parsed and validated only — see [Gap 4](#gap-4-the-excluded-channel-ids-env-var-is-parsed-and-validated-but-never-applied-to-storage) below; nothing in this build seeds it into the real, enforced exclusion table** |
| `KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS` | Optional, positive integer | Seeds a guild's default `RetentionPolicy` the first time none is configured (never overrides a guild that already has one, staff-configured or previously seeded) |
| `KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS` | Optional, positive integer | Read once at the Discord layer (`FetchCommands`) and passed as a plain argument to `/fetch inactive`/`/fetch activity` — deliberately never stored or seeded into any table (Task 7's design decision) |

`Settings.from_env` validates every one of these (positive-integer / comma-separated
positive-snowflake-list checks, `SettingsError` on bad input) but a missing or unset
value never blocks startup — all four fall back to safe defaults (disabled, no
exclusions, no retention cap, a documented fallback inactivity threshold). See
`.env.example` in the repository root for the same four names with inline notes.

## Confirmed: voice-session and attendance extraction rely on Task 7's stateful bridge

`extract_voice_session_event`/`extract_attendance_event`
(`src/krubit/discord/activity_events.py`, Task 4) deliberately take a
caller-assembled snapshot/payload rather than a raw `discord.py` object, because
Discord delivers a voice-state change as ONE event (never a pre-paired join+leave)
and a Scheduled Event RSVP as TWO separate callbacks
(`on_scheduled_event_user_add`/`_remove`). Confirmed by reading
`src/krubit/discord/activity_runtime.py`: Task 7 built exactly the required
bridges — `ActivityRuntime` keeps a small in-memory
`{(guild_id, member_id): _VoiceJoinSnapshot}` cache, populated on a channel join and
consulted (then cleared) on the matching leave, with stale entries pruned by age
every `sweep_cycle`/`on_voice_state_update` call (no durable table, matching Phase
3's in-memory detector-cache precedent); and a private `_handle_attendance` helper
that both attendance callbacks funnel through, combining Discord's two
separately-delivered callback arguments into the single payload shape
`extract_attendance_event` expects. A member who leaves voice without a tracked
join (a bot restart lost the cache, or the join predates this process) produces no
fabricated session — matching `extract_voice_session_event`'s own contract.

## Minor, low-severity implementation note: milestone upsert is a linear scan, not a direct query

`SQLiteStore.save_milestone`'s upsert re-reads via `list_milestones` and does a
Python-side linear scan to detect an existing milestone rather than a single
targeted query. Functionally correct and already covered by tests — an O(n) cost
per save relative to a member's milestone count, not a correctness issue. Worth a
future optimization pass if a guild's milestone volume per member grows large, but
not a blocker for this build.

## Discord Developer Portal: no privileged intent required

Unlike Phase 3's Message Content intent, Phase 4's three additional gateway
intents — `guild_reactions`, `voice_states`, `guild_scheduled_events`
(`src/krubit/discord/install.py::phase_four_intents()`) — are all **non-privileged**:
no Developer Portal toggle is required for any of them. `KrubitBot.__init__`
requests all three additively on top of whichever intent set Watchdog's own gating
already selected, but only when `settings.activity_ledger_enabled` is `True`
(`src/krubit/discord/bot.py`), matching every other phase's "request a feature's
intents only when that feature's flag is on" convention. Set
`KRUBIT_ACTIVITY_LEDGER_ENABLED=true` in the master `.env` and restart Krubit
through `scripts/invoke-krubit.ps1 run` — no Portal step is needed first.

**Known code-organization gap, not a functional one:**
`install.py::phase_four_intents()` is fully implemented and unit-tested
(`tests/test_discord_install.py`), but `KrubitBot.__init__` never actually calls
it. Instead, `bot.py` sets the three flags (`guild_reactions`, `voice_states`,
`guild_scheduled_events`) directly on whichever intent set Watchdog's own gating
already produced, because `phase_four_intents()` unconditionally inherits
`phase_three_intents()`'s privileged `message_content` intent, which must stay
gated on `watchdog_enabled` alone — calling `phase_four_intents()` directly from a
context where `watchdog_enabled` is `False` would silently request Message Content
too. The functional outcome (all three activity-ledger intents requested exactly
when `activity_ledger_enabled` is on, `message_content` requested exactly when
`watchdog_enabled` is on) is correct and tested, but it means `bot.py`'s inline
logic and `phase_four_intents()`'s own logic must be kept in sync by hand — a real
maintenance risk if the two ever drift, since only `bot.py`'s inline copy actually
runs.

## Prerequisites (Discord side, in addition to Phase 1/2A/2/3 requirements)

1. No new Discord permission scope is requested for the activity ledger itself —
   ingestion and views never require a mutation permission.
2. No allow/block-list-style UI exists for channel exclusion (see
   [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only)) —
   `channel_exclusions` rows must be inserted directly against the SQLite database
   (via `SQLiteStore.save_exclusion_entry`) until a dedicated command exists, the
   same limitation Phase 3's guide documents for `guild_allow_block_lists`.

## Structural proofs

The design doc's Completion Gate requires two properties to be verified
**structurally**, not just by behavioral test coverage, and calls them out as "the
two most important checks in the whole phase":

### 1. Channel exclusion is enforced before storage, for every real ingestion entry point

`tests/test_activity_privacy_structural_safety.py::test_excluded_channel_events_structurally_cannot_reach_storage`
drives the real, unmodified `ActivityRuntime`/`ActivityIngestionService` production
code (never a regex/grep proxy) against a counting spy store, for a message, a
reaction, a full voice join+leave session, and a Scheduled Event RSVP, in a real
excluded channel — asserting **zero** `record_ledger_event` calls for the three
channel-bearing kinds, and confirming the channel-less attendance kind still flows
through the same single gate rather than a second, unguarded path. A companion test,
`test_ingest_is_the_only_caller_of_record_ledger_event_in_src`, source-scans every
file under `src/krubit` and asserts `SQLiteStore.record_ledger_event` has exactly
one call site anywhere in the codebase —
`ActivityIngestionService.ingest` (`src/krubit/services/activity_ingestion.py`) —
so a future direct call from a new module fails the build instead of silently
reintroducing a bypass.

**How the entry-point set was hand-verified, not assumed:** every Discord gateway
callback Phase 4 wires (`on_message`, `on_reaction_add`, `on_reaction_remove`,
`on_voice_state_update`, `on_scheduled_event_user_add`,
`on_scheduled_event_user_remove`, `on_member_join`, `on_member_remove`,
`on_member_update`) was found by grepping `KrubitBot` in `src/krubit/discord/bot.py`
for every call to `self._activity_runtime.*` — not a filename glob. This
deliberately follows Phase 3's own post-mortem: its original structural safety test
covered only 5 of 10 real modules because it filtered by filename
(`glob("**/watchdog*.py")`) rather than actual feature membership.

### 2. Member deletion covers every table the live schema actually defines

`tests/test_activity_privacy_structural_safety.py::test_activity_ledger_tables_matches_the_live_schema_exactly`
queries `sqlite_master` on a freshly-initialized database for **every** table name
the live schema creates, and asserts that set equals `ACTIVITY_LEDGER_TABLES`
(`src/krubit/services/activity_privacy.py`) union an explicit, hand-enumerated list
of every pre-Phase-4 table — so a future Phase 4 table added to the schema without
being added to `ACTIVITY_LEDGER_TABLES` fails this test immediately, rather than
`delete_member_ledger_data` silently never touching it. A sibling test,
`test_member_deletion_covers_every_table_the_schema_actually_defines`, then derives
`ALL_MEMBER_SCOPED_TABLES` from a live `PRAGMA table_info` scan (which of those
tables actually carries a `member_id` column) rather than trusting the constant, and
seeds+deletes a real member's data across every one of those tables, asserting zero
rows survive. This mirrors the already-established pattern in
`tests/test_activity_privacy.py::test_all_member_scoped_tables_matches_live_schema`
(Task 6) exactly, extended to also catch a whole table missing from
`ACTIVITY_LEDGER_TABLES` in the first place — the gap the Task 6 test alone cannot
see, since it only ever scans tables already inside that list.

Both structural tests passed on first run against the corrected, hand-verified
module/table enumeration in this session — Tasks 1-8 introduced no bypass of either
property. They re-run on every future change to any covered module or table.
**A future task that adds a new activity-ledger ingestion entry point or storage
table must update the hand-enumerated lists in
`tests/test_activity_privacy_structural_safety.py`** — neither the call-graph
reading nor the table enumeration updates itself automatically.

## Known limitations that change what "enabling the activity ledger" actually does

### Gap 1: Returning-member data is invisible to staff through any `/fetch` command (the single biggest functional gap)

`returning_member_view` (`src/krubit/services/activity_views.py`) is fully built,
fully unit-tested, and computes exactly what the design doc's Views section
specifies — a member who had an inactivity gap exceeding the configured threshold
and then resumed activity. **No task in this entire nine-task plan ever allocated a
`/fetch` command to it.** `activity_commands.py` (Task 8) implements `member`,
`activity`, `newcomers`, `inactive`, `milestones`, `retention`, and
`community_pulse` — exactly the seven commands the design doc's own Commands
section lists, none of which is `returning_member_view`. Confirmed by grep: no
file under `src/krubit/discord` references `returning_member_view` at all.

**Practical consequence:** a member who went quiet for longer than the configured
inactivity threshold and then came back — exactly the case a community manager
most wants a proactive signal about, since it is the highest-value moment for staff
or Zariya outreach — produces no distinguishable signal anywhere in the `/fetch`
surface today. That member simply stops appearing in `/fetch inactive` once they
resume activity, indistinguishable from a member who was never inactive at all.
This is comparable in severity and kind to Phase 2's undelivered social platforms
and Phase 3's "a lone `INCIDENT`-band join is never notified in real time" gap — a
fully-functional piece of the system with zero path to a human. A future task must
add a `/fetch returning` command (or fold it into an existing view) before this
capability has any operational value.

### Gap 2: Recognition-candidate data is invisible to staff through any `/fetch` command — the identical defect shape as Gap 1

`recognition_candidates` (`src/krubit/services/milestones.py:266`) is fully built
and fully unit-tested (`tests/test_milestones.py`), and computes exactly what the
design doc's Views section specifies for the "Recognition-candidate view": a
factual shortlist of members with notable, verifiable activity (multiple milestones
reached within a trailing window, high channel/event diversity, a "returning" flag)
— each candidate's `reasons` a non-empty tuple of plain factual statements, never a
numeric score, never generated recognition wording (Krubit surfaces facts only;
deciding who deserves recognition and drafting the words is explicitly Zariya's
role, per the rollout doc). **No task in this entire nine-task plan ever allocated a
`/fetch` command to it, the same gap shape as Gap 1 above.** Confirmed by grep: no
file under `src/krubit/discord` references `recognition_candidates` or
`RecognitionCandidate` at all — the design doc's Views section names six views
(newcomer, inactive, returning-member, milestone, recognition-candidate,
community-pulse) and `activity_commands.py` (Task 8) backs commands with only four
of them (newcomer/inactive/milestone/community-pulse); `returning_member_view` and
`recognition_candidates` are the two views with no command anywhere in this plan.

**Practical consequence:** a factual, code-verified shortlist of "which members
reached multiple milestones recently, showed high channel diversity, or just
returned from an inactivity gap" — exactly the kind of surfaced-facts input the
design doc says Zariya needs to decide who deserves recognition — currently has no
way to reach a human through any command. This was found during this task's own
audit, using the same "check every design-doc-named view/capability against the
actual `/fetch` command set" methodology that found Gap 1 — it should have been
caught alongside Gap 1 in the first pass of this task, and is documented here with
equivalent prominence rather than as an afterthought. A future task must add a
`/fetch recognition-candidates` command (or fold it into an existing staff view)
before this capability has any operational value, ideally alongside the Gap 1 fix
since both are the same missing-command defect.

### Gap 3: Deletion, export, and channel-exclusion configuration have no `/fetch` command — direct database access only

`delete_member` and `export_member_data` (`src/krubit/services/activity_privacy.py`)
and `SQLiteStore.save_exclusion_entry`/`list_exclusion_entries`
(`src/krubit/storage/sqlite.py`) are fully implemented, fully tested against a real
on-disk database (including the deletion-completeness structural proof above), and
directly satisfy three of the design doc's five Privacy Controls (Deletion, Export,
Channel exclusion). **None of the three has a Discord-facing command anywhere in
this nine-task plan.** Confirmed by grep: neither `delete_member(`,
`export_member_data(`, nor `save_exclusion_entry(` is called from any file under
`src/krubit/discord`, and there is no CLI script under `scripts/` that calls them
either. This was a deliberate, documented Task 7 scope decision for the
exclusion-ids setting specifically (see [Gap 4](#gap-4-the-excluded-channel-ids-env-var-is-parsed-and-validated-but-never-applied-to-storage)),
and an artifact of the design doc's own Commands section never listing a
deletion/export/exclusion command for Task 8 to build — not a bug introduced by any
single task, but a real, confirmed plan-level gap all the same, exactly like
[Gap 1](#gap-1-returning-member-data-is-invisible-to-staff-through-any-fetch-command-the-single-biggest-functional-gap)
above.

**Practical consequence:** today, honoring a member's deletion or export request, or
configuring a channel exclusion at all, requires an operator to call these
functions directly (e.g. from a Python REPL against the running `SQLiteStore`) or
write ad hoc SQL against `data/krubit.db` — there is no staff-facing, auditable,
in-Discord path for any of the three. This mirrors Phase 3's own documented gap
("no allow/block-list UI exists yet ... rows must be inserted directly against the
SQLite database"), but is more significant here because Deletion and Channel
exclusion are two of the design doc's five named Privacy Controls, not an
enforcement-detail configuration table. A future task must add staff-facing
(deletion/exclusion) and member-facing (export, staff-on-behalf-of-a-member)
command surfaces before these controls have any operational reach.

### Gap 4: The excluded-channel-ids env var is parsed and validated, but never applied to storage

`Settings.activity_ledger_excluded_channel_ids` (`KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS`)
is parsed, validated (comma-separated positive snowflakes), and unit-tested, but
**no code path in this build ever writes it into the real, enforced
`channel_exclusions` table.** This was a deliberate Task 7 judgment call, not an
oversight: `ExclusionEntry` carries a staff-set `reason` and `excluded_by`
attribution that a flat env-var list cannot express, and `save_exclusion_entry` is
an upsert, so blindly reseeding from this setting on every guild connect would
silently clobber a staff member's own configured `reason` for an overlapping
channel ID. Unlike `KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS` (which genuinely is
enforced — see below), setting this variable today has **zero observable effect**.
Do not configure it expecting channel exclusion to happen automatically; channel
exclusion is fully enforced (see the structural proof above) once a
`channel_exclusions` row exists, but a row must be written directly against
storage per [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only)
today.

`KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS`, by contrast, **is** genuinely enforced:
`ActivityRuntime.sweep_cycle` seeds a guild's default `RetentionPolicy` from this
setting the first time it finds none configured for that guild — a one-time seed,
never an override of an existing (staff-configured or previously-seeded) policy.
`RetentionPolicy` carries no staff-authored field this seeding could clobber
(unlike `ExclusionEntry`'s `reason`), which is exactly why this setting was safe to
auto-wire while the exclusion-ids setting was not.

### Gap 5: `inactive_view`'s left-member detection can miss a member who left long ago in a high-volume guild

`inactive_view` (`src/krubit/services/activity_views.py`) determines whether a
member has left by scanning only the 500 most-recent rows of the pre-existing,
guild-scoped `guild_events` table (`SQLiteStore.list_events`'s inherited cap, the
same limit `webhook_and_permission_risk.py`'s detectors already accept). A member
who left long enough ago that 500+ other guild events have accumulated since could
incorrectly still appear in `/fetch inactive` as if still present. This is a real,
low-probability accuracy gap in high-activity guilds, not a privacy issue — no
excluded or left-member content is exposed, only a stale presence inference.

### Gap 6: No atomicity between deleting a member's data and recording the deletion receipt

`delete_member` (`src/krubit/services/activity_privacy.py`) calls
`SQLiteStore.delete_member_ledger_data` and then, as a separate step, writes the
deletion receipt — deliberately in that order (see the module's own docstring: the
receipt must be written after deletion, or deletion would immediately erase it). A
process crash between those two steps leaves the member's data genuinely deleted
with no durable proof that the deletion occurred. This is a real, accepted
trade-off in this build, not a silent data-loss bug — the deletion itself is never
partial or reversed by the crash, only the audit trail of it having happened.

### Gap 7: `export_member_data` has no upper bound on event volume

`SQLiteStore.list_all_ledger_events_for_member` (used by export, deliberately
uncapped so an export is never silently truncated the way the 500-row interactive
view cap would truncate it) loads every one of a member's events into memory at
once. A guild that never configures a retention policy (see
[Gap 4](#gap-4-the-excluded-channel-ids-env-var-is-parsed-and-validated-but-never-applied-to-storage)
above — retention is opt-in, not a default cap) could accumulate years of
unpruned events for one long-tenured member, and exporting that member loads all of
them into process memory in a single call. A real, low-probability resource risk in
a very old, very active, never-retention-configured guild — not a correctness bug.

### Gap 8: `time_to_activation`/`participation_trend` require the caller to supply an already-windowed, now-anchored event list

`participation_trend` (`src/krubit/domain/activity_ledger.py`) is a pure,
clockless function: it anchors its "active days"/"returning" calculation to the
*latest event actually supplied*, not real wall-clock time. Every production caller
(`activity_views.py`) is responsible for pre-filtering events to a real trailing
window ending at `datetime.now(UTC)` before calling it — documented as a **caller
contract** in the function's own docstring, not enforced by the function itself. A
future caller that passes a stale or improperly-windowed event list would get a
result that is internally consistent but silently wrong relative to actual current
time; this has not happened in this build (every real call site windows correctly,
confirmed in code review), but nothing in the type system prevents a future
regression.

## Shadow mode

1. Set `KRUBIT_ACTIVITY_LEDGER_ENABLED=true`. There is no separate
   notifications flag to hold back in this phase — the ledger has no autonomous
   notification path at all (see the design doc's Views section: every view is a
   pull, staff- or self-queried through `/fetch`, never a push).
2. Events accumulate normally and are queryable via `/fetch member`,
   `/fetch activity`, `/fetch newcomers`, `/fetch inactive`, `/fetch milestones`,
   `/fetch retention`, and `/fetch community-pulse` (all staff-only except the two
   staff-or-self commands, all ephemeral).
3. Use this period to sanity-check cohort/activation numbers against real join and
   participation traffic before relying on `/fetch retention`/`/fetch
   community-pulse` for any operational decision.
4. If a specific channel must never be measured (a staff-only or NSFW channel, for
   example), write a `channel_exclusions` row for it directly against storage
   before enabling ingestion in that guild — see
   [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only).
   Exclusion is enforced before storage but is **not retroactive**: excluding a
   channel after events from it are already stored does not purge those events
   (explicit deletion is the tool for retroactive removal, per the design doc's
   Explicit Exclusions).

## Commands

Staff-only (Manage Guild), all ephemeral:

```text
/fetch member <member>
/fetch newcomers
/fetch inactive
/fetch retention
/fetch community-pulse
```

Staff-or-self (a caller may always view their own data; another member's data
requires staff authority, re-validated against the caller's own ID inside the
service layer):

```text
/fetch activity [member]
/fetch milestones [member]
```

None of these commands can delete, export, or reconfigure anything — they are
read/report surfaces only. See [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only)
for the mutation-capable functions this build implements but does not expose
through any command.

## Rollback

1. Set `KRUBIT_ACTIVITY_LEDGER_ENABLED=false` in the master `.env`.
2. Restart with `& scripts/invoke-krubit.ps1 run`.
3. Confirm `/fetch integrations`/`/fetch server-health` report
   `activity_ledger_disabled` and that no new `ledger_events` rows are written.
4. Keep `ledger_events`, `milestones`, `channel_exclusions`, `retention_policies`,
   and `activity_receipts` — they are additive and safe to retain during rollback,
   matching every prior phase's rollback discipline. Disabling
   `KRUBIT_ACTIVITY_LEDGER_ENABLED` does not run the retention sweep, so already-
   stored rows do not age out while disabled; they resume aging out normally once
   re-enabled.

## Data deletion

Per [Gap 3](#gap-3-deletion-export-and-channel-exclusion-configuration-have-no-fetch-command--direct-database-access-only),
there is no `/fetch` command for member deletion in this build. For a full
data-deletion request (member request or Discord's own deletion requirement),
follow the existing [Privacy Policy](../PRIVACY_POLICY.md) section 10 process: an
operator must call `krubit.services.activity_privacy.delete_member` directly (or,
until that is wrapped in a command, delete the relevant rows from `ledger_events`,
`milestones`, and `activity_receipts` scoped by `guild_id`/`member_id` and record a
receipt by hand) — see that module's docstring for why the receipt must be written
*after* the deletion. Never delete `data/krubit.db`, its WAL, or its SHM file as a
substitute — that destroys unrelated Phase 0/1/2/2A/3 records for every guild.

## Related documents

- [Phase 4 Activity Ledger design](../superpowers/specs/2026-08-06-phase-4-activity-ledger-design.md)
- [Phase 3 Watchdog operations guide](phase-3-watchdog.md) (the honesty/rigor model
  this guide's known-limitations sections follow)
- [Phase 2 completion audit](phase-2-completion-audit.md)
- [Phase 1 operations guide](phase-1-operations.md)
- [Product rollout](../roadmaps/2026-08-03-krubit-phase-rollout.md)
- [Privacy Policy](../PRIVACY_POLICY.md)
