# Krubit Development Log: `/fetch` Command Reorganization

**Date:** August 8, 2026
**Status:** Implementation, fix wave, and final whole-branch review complete.
Branch is **not merged and not pushed** — merging to `main` requires the
project owner present, per standing project rule.

## Scope

The previous night's Phase 4 command-surface work left `FetchCommands` at
Discord's hard 25-direct-child-per-group cap, with a stopgap
`ActivityAdminCommands` subgroup consuming the last slot. This effort is the
deliberate, owner-approved follow-up: restructure the whole `/fetch` command
tree so the cap stops being a live constraint, per
[the design spec](../superpowers/specs/2026-08-08-fetch-command-reorg-design.md)
and [plan](../superpowers/plans/2026-08-08-fetch-command-reorg.md).

Every grouping/naming/authority decision in the design was confirmed
interactively with the project owner before implementation began.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | New `/fetch sniff` group — consolidates the five Watchdog/security commands (`member`, `report`, `incident`, `evidence`, `watchlist`); the old bare `sniff` command renamed to `sniff member` since a group can't contain a same-named subcommand. |
| 2 | New `/fetch admin` group — consolidates 9 general-operational commands and folds in all 5 of `ActivityAdminCommands`' commands (that subgroup is retired entirely); `member-export` relocated to a flat `/fetch member-export` command instead, since it's staff-or-self and nesting it under an admin-gated group would hide it from the regular members who need self-service access. |
| 3 | Opened `/fetch latest` and `/fetch schedule` to all guild members (previously staff-only) via a new, narrower `FoundationService.authorize_member` / `FetchCommands.authorize_public` authority tier that checks only guild-enabled status, not `manage_guild`. Along the way, found and closed a real gap in the original plan: `retention`/`community-pulse` had never been assigned a command home — owner decided to fold both into `/fetch admin`. |

Net result: `FetchCommands` direct children reduced from 25 to 11
(`sniff`, `admin`, `backup`, `live`, `creator`, `notifications`, `activity`,
`milestones`, `member-export`, `latest`, `schedule`) — 14 slots of headroom
recovered. `AdminCommands` holds 16 children, `SniffCommands` holds 5.

## An out-of-brief bug fix, caught and fixed along the way

A pre-existing test (`test_fetch_status_is_staff_only_and_receipts_the_
requesting_actor` in `tests/test_phase_one_commands.py`) looked up
`/fetch status` directly on `FetchCommands.commands`, unaware it was about
to move under `/fetch admin` in Task 2. The implementer caught the failure,
confirmed it was the same command's behavior at a new location (not a scope
change), and fixed it rather than reporting blocked — flagged in the task
report per this project's "no untested Discord-layer paths" convention.

Separately, mid-session the project owner reported a real, unrelated bug:
`/fetch creator add` failing on a YouTube `/channel/<id>` URL. Fixed
directly (not via subagent dispatch, since it was a small scoped change):
added a second `Platform.YOUTUBE` catalog entry recognizing the
`/channel/<id>` URL shape in
[`catalog.py`](../../src/krubit/integrations/catalog.py), with a
regression test in `tests/test_connector_catalog.py`.

## Final review and fix wave

The final whole-branch review found two Important issues (no Critical),
both fixed in one consolidated fix wave (commit `78c22db`) and
independently reverified in a scoped re-review as fully addressed with no
new breakage:

1. **`latest`/`schedule` hardcoded `is_admin=True`** in the `ActorContext`
   passed to the service layer, regardless of the actual caller's
   permissions. Since these commands are now reachable by any member, this
   falsely asserted admin authority — inert today (neither service method
   reads `is_admin`), but a landmine since `is_admin` is a real authority
   discriminator elsewhere in the same service class. Fixed by deriving the
   real value from the caller's `manage_guild`/`administrator` permissions,
   matching the existing `_activity_actor` pattern, with tests proving the
   flag is no longer hardcoded for a non-admin caller of both commands.
2. **Two operator runbooks** (`docs/operations/phase-0-setup.md`,
   `docs/operations/phase-1-operations.md`) referenced pre-reorg flat
   command paths (`/fetch status`, `/fetch test-card`, etc.) that no longer
   exist. Updated both to the correct `/fetch admin ...` paths.

Two optional, zero-risk fixes were also applied: removed orphaned comment
blocks in `bot.py` describing commands that had moved to their own group
classes with their own docstrings, and added exception-handling symmetry to
`authorize_public` (catching `AuthorizationError` alongside
`GuildDisabledError`, matching `authorize()`'s shape) as defense-in-depth.

The re-review also surfaced, as an out-of-scope observation rather than an
open finding, that several other `docs/operations/` files (phase completion
records, not live runbooks) still contain stale flat `/fetch` paths — noted
here for awareness, not blocking this branch.

## Known limitations / follow-ups

- **Several `docs/operations/` phase-completion records** (not the two
  runbooks fixed above) still reference pre-reorg flat command paths. Left
  as-is since they read as historical records rather than live operator
  guides; worth a cleanup pass if they cause confusion later.
- **The staff-only leaderboard command remains deferred** — ranking metric
  not yet decided by the project owner. Location is settled
  (`/fetch admin leaderboard`) but not built.
- **The earlier Phase 4 devlog has been marked superseded** with a pointer
  to this one, since the `ActivityAdminCommands` subgroup and
  `/fetch activity-admin <name>` paths it documents no longer exist.

## Test evidence

Full suite: 1112/1112 passing (1110 baseline for this branch + 2 new
regression tests from the final fix wave). `ruff check .` clean throughout.
`pyright` shows only pre-existing, already-accepted noise categories
(untyped `discord.py` stubs) — confirmed via `git stash` diffing at every
task, no new error category introduced.
