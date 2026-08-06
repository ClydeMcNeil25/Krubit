# Krubit Phase 4 Member Activity Ledger Design

**Date:** 2026-08-06
**Status:** Approved for implementation planning
**Scope:** Per-member authorized event ledger, activation/retention calculation,
staff and self-service views, and privacy controls (exclusion, retention, deletion,
export) — measurement only, no relationship judgment.

## Purpose

Measure community participation and new-member activation without turning Krubit
into a surveillance or relationship-judgment system. Krubit calculates facts and
trends; it never assigns personality, loyalty, mental-health, or guilt labels, and
it never reads DM content or records voice content (only participation duration).

## Product Decisions

- The ledger records **factual participation events**, not content: a message event
  records that a member posted in a channel at a time (channel ID, timestamp,
  optional reply/thread context) — never the message text. A reaction event records
  which emoji-shaped reaction was added, not any inferred sentiment. A voice event
  records join/leave timestamps and computed duration — never audio, never a
  transcript, never speaking-time-per-word inference.
- DMs are structurally excluded from ingestion at every entry point — the same
  double-gate discipline Phase 3 established for message content (`guild is None`
  checked both at the dispatch site and again inside the consuming service).
- Every event table is guild-scoped with `guild_id` as the leading key, and every
  event additionally carries `member_id`, matching the codebase's established
  `PRIMARY KEY (guild_id, ...)` convention.
- Retention cohorts (7-day, 30-day) and activation calculations are pure functions
  over the stored ledger — deterministic and reproducible from fixtures, the same
  discipline Phase 3 applied to risk-band evaluation. "First meaningful action" is a
  named, explainable rule (not a black-box heuristic): the first non-join event of a
  configured set of "meaningful" event kinds (message, reaction, voice join, event
  RSVP) after the member's join event.
- Channel exclusion is enforced **before storage**, not at query time: an excluded
  channel's events are never written to the ledger at all, so excluding a channel
  after the fact also purges nothing retroactively (exclusion is forward-looking by
  design — a later export/deletion capability handles retroactive removal
  separately, see below).
- Member deletion removes **all derived records**, not just the raw ledger rows —
  cohort membership, milestone records, and any cached aggregate keyed by that
  member must also be removed or recomputed. This is Krubit's first genuine
  data-deletion capability; get the completeness of "derived records" right, since
  an incomplete deletion is worse than an honest inability to delete.
- Export produces a factual, member-scoped data package (their own ledger rows,
  their own milestones) — never another member's data, and never staff-only
  aggregate views.
- Detailed per-member profiles are staff-only (Manage Guild or configured
  moderator/community role); a member's **self view** exposes only their own
  milestones and their own retained activity summary, never comparison to other
  members or guild-wide aggregates.
- Baselines distinguish message spam from varied healthy participation the same way
  Phase 3's message-signal work approached this problem: bounded, named,
  explainable signals (message frequency, channel diversity, reaction-to-message
  ratio) rather than an opaque "engagement score." Phase 4 does not reuse Phase 3's
  spam *detection* (that remains Watchdog's job) — it only needs to avoid counting a
  spam burst as "high engagement" when computing participation trends.
- Voice, reaction, and scheduled-event-attendance intents (`GUILD_MESSAGE_REACTIONS`,
  `GUILD_VOICE_STATES`, `GUILD_SCHEDULED_EVENTS`) are all non-privileged — no
  Discord Developer Portal toggle required, unlike Phase 3's Message Content intent.
  Add them additively via a `phase_four_intents()` following the established
  phase-ladder pattern in `install.py`.
- Shadow-then-enable: the ledger records from day one behind an explicit
  `KRUBIT_ACTIVITY_LEDGER_ENABLED` flag (default off), enforced at every actual
  ingestion call site (not just at construction), learning directly from Phase 2's
  and Phase 3's whole-branch-review findings that a parsed-but-unenforced flag is a
  recurring class of bug in this codebase.

## Architecture

```text
Discord Gateway events (message, reaction, voice, member, role, scheduled event)
                |
   guild-scoped, DM-excluded, channel-exclusion-filtered ingestion
                |
        per-member event ledger (factual, content-free)
                |
   pure activation/retention/trend calculation (deterministic)
                |
  cohort + milestone + recognition-candidate materialization
                |
staff profile views  |  member self view  |  /fetch command surface
                |
      privacy controls: exclusion, retention window, deletion, export
```

### Event Ledger

One append-only, guild-scoped table per event kind (or a single polymorphic table
with a `kind` discriminant — implementation detail for the plan to decide, following
whichever existing convention in `sqlite.py` best fits; the domain model must expose
distinct value objects per kind regardless of storage shape): `join`, `onboarding`
(Rules Screening completion, where observable), `message` (channel + timestamp only),
`reaction` (channel + emoji shape + timestamp), `voice_session` (channel + join/leave
+ computed duration, no content), `event_attendance` (Scheduled Event RSVP add/
remove), `role_change` (role granted/removed, reusing Phase 1's existing role-event
tracking rather than duplicating it), `milestone` (materialized, see below), and
`moderation_receipt` (a redacted pointer to a Watchdog incident/receipt this member
was involved in, not the incident's raw content — reuses Phase 3's existing receipt
records rather than re-deriving them).

### Activation and Retention Calculation

`time_to_activation(member_id, guild_id)` is a pure function: earliest timestamp of
any configured "meaningful action" kind after the member's join event, minus the
join timestamp. Absence of any meaningful action within the retention window means
"not yet activated," not an error.

`cohort_membership(guild_id, window)` for 7-day and 30-day windows computes, per
join-week cohort, the fraction of members who had at least one meaningful-action
event on a day within `window` days of joining. This must reproduce known fixtures
exactly — no floating-point drift, no off-by-one on the window boundary (inclusive
join day, inclusive window-end day, matching Phase 3's quiet-hours half-open-interval
discipline for date boundaries).

`participation_trend(member_id, guild_id, window)` computes active-day count, a
"returning" flag (activity after a configured inactivity gap), and channel/event
diversity — again all deterministic, explainable, and traceable to specific stored
events, never a single opaque trend score.

### Views

- **Newcomer view**: members joined within a configurable recent window, their
  activation status, and their most recent meaningful-action timestamp.
- **Inactive view**: members with no meaningful action within the configured
  inactivity threshold, excluding members who left.
- **Returning-member view**: members who had a gap exceeding the inactivity
  threshold and then resumed activity.
- **Milestone view**: message-count thresholds, join-anniversary dates, and any other
  configured milestone rule, each a named, explainable rule (not a black-box
  "loyalty score").
- **Recognition-candidate view**: a factual shortlist Krubit surfaces to staff/Zariya
  (e.g. "reached 3 milestones in the last 30 days, high channel diversity") — Krubit
  never decides *who deserves recognition* or drafts recognition wording; that
  remains explicitly Zariya's per the rollout doc's ownership table. Krubit surfaces
  facts only.
- **Community-pulse view**: guild-wide factual summary (active-member count,
  cohort retention rates, channel/event contribution) — no sentiment, no member-level
  detail beyond what's already staff-authorized elsewhere.

### Privacy Controls

- **Channel exclusion**: a guild-configurable list of channel IDs whose events are
  never written to the ledger. Enforced at the ingestion boundary, before any
  storage call — an excluded channel's message/reaction/voice events must not reach
  `sqlite.py` at all, not merely be filtered out of queries.
- **Retention window**: a guild-configurable maximum age for raw ledger rows;
  a scheduled sweep (mirroring Phase 3's `sweep_cycle` isolation discipline — one
  guild's/table's sweep failure must never block another's) prunes rows older than
  the configured window. Cohort/milestone *aggregates* already computed from pruned
  raw rows are retained (the raw event, not the fact that a milestone was reached, is
  what ages out) unless the guild's retention policy explicitly says otherwise —
  the plan should make this an explicit, tested decision, not an implicit one.
- **Deletion**: a staff-triggered, member-scoped deletion that removes the member's
  raw ledger rows, milestone records, and any cached aggregate — with a durable
  receipt recording that deletion occurred (not what was deleted, to avoid the
  receipt itself becoming a retained copy of otherwise-deleted data).
- **Export**: a member-triggered (self) or staff-triggered (on a member's behalf,
  audited) export of that member's own ledger rows and milestones as a structured,
  redacted data package.
- **Tracking disclosure**: a guild-configurable, member-visible statement of what is
  and is not tracked — surfaced via a command or pinned reference, not buried in
  documentation only.

## Commands

- `/fetch member <member>` — staff-only detailed profile (full ledger summary,
  activation status, milestones, moderation-receipt pointers).
- `/fetch activity <member>` — staff-only participation trend detail for one member,
  or a member's own self-view when the caller queries themselves (member-accessible
  subset only).
- `/fetch newcomers` — staff-only newcomer view.
- `/fetch inactive` — staff-only inactive-member view.
- `/fetch milestones [member]` — milestone view; self-accessible for one's own
  milestones, staff-only for another member's.
- `/fetch retention` — staff-only cohort retention view.
- `/fetch community-pulse` — staff-only guild-wide factual summary.

Every command enforces authority **before** any query, matching the pattern Phase 2
and Phase 3 both had to fix as review findings when a command skipped this ordering.

## Testing and Rollout

Automated tests must cover: cohort calculations reproducing known fixtures exactly
(the design doc's own worked examples, mirroring Phase 3's threshold-boundary test
discipline), channel exclusion enforced *before* storage (not just filtered at query
time — a positive test proving an excluded channel's event never reaches the
ledger table), member deletion removing every derived record (a test enumerating
every table a member's ID could appear in and asserting zero rows survive deletion,
not just the primary ledger table), detailed profiles genuinely access-controlled
(a non-staff, non-self caller denied on every profile-adjacent command), and that
the system cannot expose private-channel (excluded-channel) activity to any
unauthorized viewer through any command or aggregate. This last property is the
Phase 4 equivalent of Phase 3's structural no-moderation-authority proof — the plan
should include an explicit, structurally-verified proof for it, not just behavioral
tests, learning directly from the fact that Phase 3's original structural test
under-covered its own stated scope and needed a whole-branch-review fix.

## Completion Gate

Phase 4 is complete only when: cohort calculations reproduce known fixtures; channel
exclusions are enforced before storage (verified structurally); member deletion
removes derived records as specified (verified by enumerating affected tables, not
assumed); detailed profiles are access-controlled; and the system cannot expose
private-channel activity to unauthorized viewers. This mirrors the Phase 4 exit gate
in the rollout doc verbatim.

## Explicit Exclusions

- No DM ingestion for analytics, ever.
- No voice content recording — duration only.
- No personality, loyalty, mental-health, or guilt labels assigned to any member.
- No relationship-judgment output — Krubit surfaces facts; Zariya interprets,
  prioritizes, and decides who deserves recognition or outreach.
- No comparison of one member's data to another's in the self-view.
- No retroactive purge of already-stored events merely from adding a channel to the
  exclusion list (exclusion is forward-looking; explicit deletion is the tool for
  retroactive removal).

## Official Capability References

- Discord Gateway intents (`GUILD_MESSAGE_REACTIONS`, `GUILD_VOICE_STATES`,
  `GUILD_SCHEDULED_EVENTS`, all non-privileged): <https://discord.com/developers/docs/events/gateway#gateway-intents>
- Discord voice state updates: <https://discord.com/developers/docs/events/gateway-events#voice-state-update>
- Discord Scheduled Event user add/remove: <https://discord.com/developers/docs/events/gateway-events#guild-scheduled-event-user-add>
- Discord reaction add/remove: <https://discord.com/developers/docs/events/gateway-events#message-reaction-add>
