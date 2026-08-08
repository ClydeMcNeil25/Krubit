# `/fetch` Command Reorganization Design

**Date:** 2026-08-08
**Status:** Approved for implementation planning
**Scope:** Reduce `/fetch`'s child-command count from 25/25 (Discord's hard cap)
down to single digits by consolidating Watchdog and general-operational
commands into two new subgroups, and open two low-sensitivity commands to
regular members. Confirmed interactively with the user across this
conversation — every grouping/naming decision below was explicitly agreed,
not invented.

## Context

Last night's Phase 4 command-surface work discovered `FetchCommands` sits at
Discord's hard 25-direct-child limit and worked around it by adding one new
subgroup (`activity-admin`) for six new commands — a stopgap that consumed
the group's last slot. This spec is the deliberate, owner-approved follow-up:
restructure the whole command tree properly instead of leaving `/fetch` full.

## Confirmed decisions (from conversation)

1. **`/fetch sniff`** — new subgroup for the five Watchdog/security commands.
   The existing bare `sniff` (member risk assessment) command's name
   collides with the group name it's moving into (Discord doesn't allow a
   group and a same-named subcommand at the same node), so it's renamed to
   `member` inside the group.
2. **`/fetch admin`** — new subgroup for fourteen general-operational and
   activity-ledger-admin commands, replacing both the flat commands they
   came from and folding in all six of last night's `activity-admin`
   commands (that group is retired — its contents move into `admin`
   instead, so the branch doesn't end up with two separate
   staff-only-command groups sitting side by side for no reason).
3. **`backup`, `live`, `creator`, `notifications`** stay exactly as they are
   — organized by feature area, not by permission, and not part of this
   consolidation.
4. **`activity`, `milestones`, `member-export`** stay flat on `/fetch`
   directly — these are staff-or-self commands; nesting them under an
   admin-gated group would make Discord hide them from the regular members
   who need self-service access.
5. **`latest` and `schedule`** open to all members — content-discovery
   commands with no personal/sensitive data, no reason to stay staff-only.
   Every other currently-admin-only command stays admin-only (see the
   conversation's per-command sensitivity review — Watchdog data, member
   PII, destructive actions, and account-level mutations all have a real
   reason to stay restricted).
6. **Leaderboard command** — explicitly deferred, not part of this spec.
   Revisit later with a chosen ranking metric.

## Resulting `/fetch` child count

| Before | After |
|---|---|
| 20 flat + 5 groups = **25** | 3 flat (`activity`, `milestones`, `member-export`) + 6 groups (`sniff`, `admin`, `backup`, `live`, `creator`, `notifications`) = **9** |

16 slots of headroom recovered.

## Full command mapping

**New `/fetch sniff` group:**

| New path | Old path |
|---|---|
| `/fetch sniff member <member>` | `/fetch sniff <member>` |
| `/fetch sniff report` | `/fetch sniff-report` |
| `/fetch sniff incident <incident_id>` | `/fetch incident <incident_id>` |
| `/fetch sniff evidence <incident_id>` | `/fetch evidence <incident_id>` |
| `/fetch sniff watchlist` | `/fetch watchlist` |

**New `/fetch admin` group:**

| New path | Old path |
|---|---|
| `/fetch admin status` | `/fetch status` |
| `/fetch admin test-card` | `/fetch test-card` |
| `/fetch admin server-health` | `/fetch server-health` |
| `/fetch admin changes` | `/fetch changes` |
| `/fetch admin permissions` | `/fetch permissions` |
| `/fetch admin integrations` | `/fetch integrations` |
| `/fetch admin member <member>` | `/fetch member <member>` |
| `/fetch admin newcomers` | `/fetch newcomers` |
| `/fetch admin inactive` | `/fetch inactive` |
| `/fetch admin returning` | `/fetch activity-admin returning` |
| `/fetch admin recognition-candidates` | `/fetch activity-admin recognition-candidates` |
| `/fetch admin member-delete <member>` | `/fetch activity-admin member-delete <member>` |
| `/fetch admin exclude-channel <channel> <reason>` | `/fetch activity-admin exclude-channel <channel> <reason>` |
| `/fetch admin exclusions` | `/fetch activity-admin exclusions` |

Note: `admin member` and `sniff member` are different commands in different
groups (detailed activity profile vs. risk assessment) — no collision, since
they live at different tree nodes.

**Opened to all members (no longer require `manage_guild`):**

- `/fetch latest`
- `/fetch schedule`

**Unchanged:** `backup`, `live`, `creator`, `notifications` groups;
`activity`, `milestones`, `member-export` flat commands.

## Implementation approach

- **`ActivityAdminCommands` is deleted, not kept alongside `admin`** — its six
  methods move into the new `AdminCommands` class. No guild ever sees two
  separate staff-only groups.
- **Authority for `latest`/`schedule`:** these currently call
  `self.authorize(interaction, action)`, which hard-requires `manage_guild`
  via `FoundationService.authorize_manager`. A new, narrower
  `FoundationService.authorize_member(guild_id, *, action)` is added —
  checks only that the guild is enabled (reusing the existing private
  `_require_enabled`), not `manage_guild`. A matching `FetchCommands.
  authorize_public(interaction, action)` mirrors `authorize()`'s shape
  (defer, error-message-on-`GuildDisabledError`, return `(guild, actor_id)`)
  but calls `authorize_member` instead of `authorize_manager` — and both
  `latest` and `schedule` drop their `@app_commands.default_permissions(
  manage_guild=True)` decorator (that decorator is what hides a command from
  non-managers in Discord's UI in the first place).
- **Every renamed/moved command keeps its existing service-layer method and
  `CommandStatus`/authority logic untouched** — this is purely Discord-layer
  reorganization (which class a method lives on, what `@app_commands.command`
  name/path it's registered under) plus the one narrower-authority addition
  above. No underlying command behavior changes.
- **`tests/test_cli.py`'s `FetchCommands` child-set assertion** must be
  updated to the new 9-child set. Any other test referencing an old flat
  command path (e.g. constructing a `FetchCommands` and calling
  `.sniff.callback(...)` directly, if any exist) must be updated to the new
  nested path.

## Explicit Exclusions

- No leaderboard command (deferred, per confirmed decision #6).
- No change to `backup`/`live`/`creator`/`notifications`.
- No change to any command's actual authority requirement except `latest`/
  `schedule` (every other admin-only command stays admin-only, exactly as
  confirmed in conversation).
- No change to any command's underlying service logic, receipt behavior, or
  test-layer (`ActivityActorContext`, `CommandResult`) shape — this is a
  Discord-layer move, not a behavior change, for every command except the
  two being opened up.

## Testing

For every moved command: a test confirming it's reachable at its new path
and NOT reachable at its old path (the old flat method no longer exists on
`FetchCommands`). For `sniff member` specifically: confirm the rename didn't
break its existing behavior (reuse its existing test, just against the new
call site). For `latest`/`schedule`: a test confirming a non-staff member
(no `manage_guild`) can now successfully invoke them in an enabled guild,
and a test confirming a disabled guild still rejects both regardless of
staff status (the guild-enabled check must still apply). A final full-tree
construction test confirming `FetchCommands` has exactly 9 direct children
after the reorg.

## Completion Gate

Complete when: `/fetch` has exactly 9 direct children; every command listed
above is reachable only at its new path; `latest`/`schedule` work for any
guild member (not just staff) while still respecting the guild-enabled gate;
every other command's authority requirement is unchanged; the full test
suite passes with no regressions; `ActivityAdminCommands` no longer exists
as a separate class.
