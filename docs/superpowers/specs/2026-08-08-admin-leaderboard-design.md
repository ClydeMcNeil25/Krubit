# `/fetch admin leaderboard` Design

**Date:** 2026-08-08
**Status:** Approved for implementation planning
**Scope:** A staff-only leaderboard command ranking guild members by
meaningful-action count within a calendar year, deferred from the
`/fetch` command reorg pending a ranking-metric decision. Confirmed
interactively with the project owner across this conversation.

## Context

The `/fetch` command reorg spec explicitly deferred a leaderboard command,
with its eventual home already settled as `/fetch admin leaderboard`. This
spec closes that gap: what it ranks by, what window it covers, and how it
handles the one real risk a year-long window introduces — retention
pruning.

## Confirmed decisions (from conversation)

1. **Metric: meaningful-action count.** Counts all four "meaningful" event
   kinds together — `MESSAGE`, `REACTION`, `VOICE_SESSION`,
   `EVENT_ATTENDANCE` — using the exact `MEANINGFUL_EVENT_KINDS` constant
   activation/retention calculations already use
   (`src/krubit/domain/activity_ledger.py:102`). Broader picture of
   participation than raw message volume alone.
2. **Window: calendar year, resetting every January 1.** Not a rolling
   365-day window — `[Jan 1 00:00 UTC of `year`, Jan 1 00:00 UTC of
   `year + 1`)`, a half-open interval matching this codebase's existing
   date-boundary convention (see `activity_ledger.py`'s cohort-window
   docstring).
3. **Year is an optional parameter, defaulting to the current year.** Staff
   can request `/fetch admin leaderboard year:2025` to see a past year's
   final standings for year-end recognition; omitting it shows the current
   year to date.
4. **Top 10, non-zero only.** Members with zero meaningful actions in the
   window simply don't appear.
5. **Retention-pruning caveat is surfaced automatically, not just
   documented.** If the guild's currently configured `RetentionPolicy.
   max_age_days` (if any) is shorter than how many days have elapsed since
   the requested year's start, raw events from the early part of that
   window may already be pruned by the scheduled retention sweep — the
   count would then silently undercount. Rather than trust staff to
   remember this, the command checks the policy at call time and appends a
   caveat to its response whenever this condition holds. As of this
   writing no guild has a `RetentionPolicy` configured, so no pruning
   currently happens automatically — but the check must hold regardless of
   today's configuration, since staff can set one later.

## Implementation approach

- **New storage method: `SqliteStore.leaderboard_counts(guild_id, *, start,
  end) -> tuple[tuple[int, int], ...]`** (`member_id`, `count` pairs,
  descending by count) — a SQL `GROUP BY member_id` aggregate over
  `ledger_events` filtered to `kind IN (...)` (the four meaningful kinds)
  and `occurred_at >= start AND occurred_at < end`, sorted `DESC` in SQL.
  This is a deliberate departure from `list_ledger_events_for_guild`'s
  pattern (which caps at 5000 rows and returns full `LedgerEvent` objects)
  — a year-long window can exceed that cap for an active guild, and the
  leaderboard only ever needs counts, never event detail, so aggregating in
  SQL avoids both the cap and the cost of materializing every row in
  Python.
- **New service function: `krubit.services.activity_views.leaderboard(store,
  guild_id, *, year, now) -> LeaderboardResult`** — computes the calendar-year
  boundary, calls `leaderboard_counts`, truncates to the top 10 non-zero
  entries, and separately calls `store.get_retention_policy(guild_id)` to
  compute the caveat flag. Lives alongside the sibling `newcomers`/
  `inactive`/`returning`/`community_pulse` guild-wide read-only views in
  `activity_views.py`, following their existing shape (plain function
  taking a store and returning a frozen result dataclass, no I/O beyond the
  store calls, framework-independent).
- **New result types `LeaderboardEntry`/`LeaderboardResult`** in
  `src/krubit/services/activity_views.py`, matching `CommunityPulse`'s
  existing home (`activity_views.py:143`) — this codebase keeps read-only
  guild-wide view results in the service module, not the pure-domain
  module. `LeaderboardEntry`: `member_id: int`, `count: int`.
  `LeaderboardResult`: `year: int`, `entries: tuple[LeaderboardEntry, ...]`,
  `retention_caveat: bool`. Not a durable record — no `guild_id`-scoped
  persistence, matching `CohortResult`/`ParticipationTrend`'s existing
  precedent as computed-not-stored value objects.
- **New command: `AdminCommands.leaderboard`** in `bot.py`, staff-only via
  `self._parent.authorize(interaction, "fetch_admin_leaderboard")` (the
  existing staff-only pattern every other `admin` command uses — this is
  not a self-service command). Optional `year: app_commands.Range[int,
  2020, <current_year>]` parameter (lower bound reflects that Krubit's
  Activity Ledger didn't exist before this project; upper bound prevents a
  nonsensical future-year request). Renders via the same character-budget
  truncation helper (`_render_capped_lines`/`_MAX_LIST_CHARS`) already
  shared by `exclusions`/`recognition-candidates`, with the retention
  caveat appended as a trailing line when `retention_caveat` is true.

## Explicit Exclusions

- No new `CohortWindow` enum member — calendar-year boundaries are a
  distinct concept (fixed calendar dates, not a rolling N-day span from
  "now") and don't fit that enum's existing rolling-window shape. This is
  a bespoke boundary computation local to the `leaderboard` service
  function, not a reusable window primitive.
- No numeric "worthiness" or engagement score — this ranks by one plain,
  named, auditable count (meaningful-action count), never a composite or
  weighted metric. Consistent with this codebase's existing stance against
  black-box scoring (see `RecognitionCandidate`'s docstring).
- No change to any other command's behavior or authority.

## Testing

- **Storage:** `leaderboard_counts` with multiple members across mixed
  event kinds, proving: only the four meaningful kinds are counted (a
  `ROLE_CHANGE` or `MODERATION_RECEIPT` event must not contribute); an
  event exactly at `start` counts and one exactly at `end` does not (half-open
  boundary correctness); results are sorted descending by count.
- **Service:** calendar-year boundary computed correctly for both the
  current year (bounded by `now`, not full-year-end) and a past year
  (bounded by the full year); top-10 non-zero truncation; retention-caveat
  computed `True` when a configured policy's `max_age_days` is shorter than
  elapsed days since year start, `False` when no policy is configured or
  the policy covers the full elapsed span.
- **Discord layer:** `/fetch admin leaderboard` is staff-only (existing
  `authorize()` denial test pattern); defaults to the current year when
  omitted; accepts an explicit past year; renders the caveat line when the
  service result has `retention_caveat=True`.

## Completion Gate

Complete when: `/fetch admin leaderboard` (optionally `year:<int>`) returns
the top 10 members by meaningful-action count for that calendar year,
correctly excludes non-meaningful event kinds, correctly bounds the
half-open year interval, surfaces the retention caveat when applicable, is
staff-only, and the full test suite passes with no regressions.
