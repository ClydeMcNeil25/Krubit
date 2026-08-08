# `/fetch` Command Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `/fetch` from 25/25 children to 9 by consolidating Watchdog
commands into `/fetch sniff` and general/activity-admin commands into
`/fetch admin`, and open `latest`/`schedule` to all members, per
`docs/superpowers/specs/2026-08-08-fetch-command-reorg-design.md`.

**Architecture:** Two new `app_commands.Group` subclasses
(`SniffCommands`, `AdminCommands`) in `src/krubit/discord/bot.py`, built with
the exact `parent`-delegation pattern already proven twice in this codebase
(`BackupCommands`, and last night's now-retired `ActivityAdminCommands`).
Every moved command's service-layer call and body is unchanged — only its
class and registered path move. `ActivityAdminCommands` is deleted; its six
methods relocate into `AdminCommands`.

**Tech Stack:** Python 3.13, discord.py 2.7.1, pytest.

## Global Constraints

- **The mechanical relocation rule** (proven twice already in this codebase):
  when a method moves from `FetchCommands` to a new nested
  `app_commands.Group`, every `self.X` reference to a `FetchCommands`
  attribute/method (`self.authorize`, `self.finish`, `self._watchdog_commands`,
  `self._activity_commands`, `self._service`, etc.) becomes `self._parent.X`.
  The command's body, service call, and rendering logic are otherwise
  byte-identical to their current form — this is a relocation, not a rewrite.
- No underlying command behavior changes except `latest`/`schedule`'s
  authority requirement (every other moved command's staff-only gate,
  service call, and rendering stays exactly as it is today).
- `ActivityAdminCommands` must not exist after this plan — its six methods
  live on `AdminCommands` instead, so no guild ever sees two separate
  staff-only groups.
- Run `./.venv/Scripts/python.exe -m pytest -q`,
  `./.venv/Scripts/python.exe -m ruff check .`, and
  `./.venv/Scripts/python.exe -m pyright <touched files>` before every
  commit (NOT `uv run`). Must not break any of the 1103 currently-passing
  tests.

---

## Task 1: `/fetch sniff` group (5 Watchdog commands + rename)

**Files:**
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_cli.py`, plus wherever the existing Watchdog command
  tests live (search for `test_bot_registers` / `sniff` in `tests/` to find
  the right file(s) — likely `tests/test_cli.py` and/or
  `tests/test_phase_one_commands.py`)

**Interfaces:**
- Consumes: `WatchdogCommandService.sniff/sniff_report/incident/evidence/
  watchlist` (unchanged), `WatchdogActorContext` (unchanged).
- Produces: `SniffCommands(app_commands.Group)` registered on
  `FetchCommands` as `self.add_command(SniffCommands(self))`.

- [ ] **Step 1: Read the current five Watchdog command methods in full**

Read `src/krubit/discord/bot.py` lines ~401-517 (the `sniff`, `sniff_report`,
`incident`, `evidence`, `watchlist` methods currently on `FetchCommands`).
These are the exact bodies to relocate.

- [ ] **Step 2: Write the failing tests**

Find and read the existing test(s) that construct `FetchCommands` and assert
its registered command tree (search `tests/test_cli.py` for a test named
something like `test_bot_registers_phase_one_fetch_commands` — this is the
same test last night's Task 1 updated for the `activity-admin` addition).
Add assertions (matching that test's exact style) that:
- `FetchCommands` has a child named `"sniff"` which is itself a group.
- That group's children are exactly `{"member", "report", "incident",
  "evidence", "watchlist"}`.
- `FetchCommands` no longer has direct children named `"sniff"`,
  `"sniff-report"`, `"incident"`, `"evidence"`, or `"watchlist"`.

Also find and adapt any existing behavioral test for the `sniff`/`sniff-report`/
`incident`/`evidence`/`watchlist` commands (search for `WatchdogCommandService`
usage in Discord-layer tests, if any exist beyond the pure service-layer
tests in `tests/test_watchdog_commands.py`, which need NO changes since they
test the framework-independent service directly, not the Discord command
tree).

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py -v -k sniff`
Expected: FAIL — `SniffCommands` doesn't exist yet, old flat commands still present

- [ ] **Step 4: Implement `SniffCommands`**

Add to `src/krubit/discord/bot.py`, following `BackupCommands`' exact
delegation shape:

```python
class SniffCommands(app_commands.Group):
    """`/fetch sniff` -- Watchdog risk-assessment and incident-evidence
    commands, consolidated from five flat `/fetch` commands to free slots
    against Discord's 25-child-per-group cap. The member-assessment command
    is named `member` here (not `sniff`) since a group cannot contain a
    same-named subcommand."""

    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(name="sniff", description="Watchdog risk assessment and incident evidence")
        self._parent = parent

    @app_commands.command(
        name="member", description="Fetch a member's current or most recent Entry Sniff assessment"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_member")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        target = WatchdogActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
        result = await self._parent._watchdog_commands.sniff(actor=actor, target=target)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_member",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="report",
        description="Fetch a guild-wide Watchdog sniff report: high-band joins and open windows",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def report(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_report")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.sniff_report(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_report",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="incident", description="Fetch one Watchdog incident's full evidence packet"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def incident(self, interaction: discord.Interaction, incident_id: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_incident")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.incident(actor=actor, incident_id=incident_id)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_incident",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="evidence", description="Fetch one Watchdog incident's raw, redacted evidence export"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def evidence(self, interaction: discord.Interaction, incident_id: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_evidence")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.evidence(actor=actor, incident_id=incident_id)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_evidence",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )

    @app_commands.command(
        name="watchlist", description="Fetch this guild's open watch windows and allow/block lists"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def watchlist(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_sniff_watchlist")
        if context is None:
            return
        guild, actor_id = context
        actor = WatchdogActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
        result = await self._parent._watchdog_commands.watchlist(actor=actor)
        embed = render_card(result.card) if result.card is not None else discord.Embed(
            title=result.status.value
        )
        await self._parent.finish(
            interaction,
            action="fetch_sniff_watchlist",
            actor_id=actor_id,
            embed=embed,
            detail=_receipt_detail(result.detail),
        )
```

Note the `action=` strings passed to `authorize`/`finish` are changed to
`fetch_sniff_member`/`fetch_sniff_report`/etc. (prefixed, distinct from the
old flat action names) — these are just audit-receipt labels, not
user-visible, but keeping them distinct avoids conflating old and new receipt
history in `/fetch admin changes` history views. Check whether any existing
test asserts a specific literal action-string value for these five commands
(search `tests/` for `"fetch_sniff"`, `"fetch_sniff_report"`,
`"fetch_incident"`, `"fetch_evidence"`, `"fetch_watchlist"`) and update any
such assertion to match the new strings.

**Delete the five old flat methods** (`sniff`, `sniff_report`, `incident`,
`evidence`, `watchlist`) from `FetchCommands` entirely — do not leave them
in place alongside the new group (that would both violate the design's
"only reachable at the new path" requirement and push `FetchCommands` back
toward its cap instead of away from it).

Register in `FetchCommands.__init__`, alongside the existing
`self.add_command(BackupCommands(self))` etc.:
```python
self.add_command(SniffCommands(self))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_watchdog_commands.py -v`
Expected: PASS, including every pre-existing test (note:
`tests/test_watchdog_commands.py` tests the framework-independent service
directly and needs no changes at all — only Discord-layer tests referencing
old command paths need updates)

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/bot.py tests/test_cli.py
git commit -m "feat: consolidate Watchdog commands into /fetch sniff group"
```

---

## Task 2: `/fetch admin` group (14 commands, absorbing and deleting `ActivityAdminCommands`)

**Files:**
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_cli.py`, `tests/test_activity_commands.py` (only if it
  references old Discord-layer paths — the service-layer tests it already
  has need no changes)

**Interfaces:**
- Consumes: every service call already used by the 9 old flat admin
  commands (`status`, `test-card`, `server-health`, `changes`,
  `permissions`, `integrations`, `member`, `newcomers`, `inactive`) and the
  6 methods currently on `ActivityAdminCommands` (`returning`,
  `recognition-candidates`, `member-delete`, `member-export`,
  `exclude-channel`, `exclusions`) — **wait**, per the design spec,
  `member-export` stays flat on `FetchCommands` (it's staff-or-self, not
  admin-only) — only 5 of `ActivityAdminCommands`' 6 methods move into
  `AdminCommands` (`returning`, `recognition-candidates`, `member-delete`,
  `exclude-channel`, `exclusions`); `member-export` relocates to being a
  flat `FetchCommands` method instead (alongside `activity`/`milestones`,
  matching its actual staff-or-self authority shape).
- Produces: `AdminCommands(app_commands.Group)` registered as
  `self.add_command(AdminCommands(self))`; `ActivityAdminCommands` deleted
  entirely.

- [ ] **Step 1: Read every method being moved, in full**

Read `src/krubit/discord/bot.py`'s current `status`, `test-card`,
`server-health`, `changes`, `permissions`, `integrations` methods (lines
~209-345), `member`, `newcomers`, `inactive` methods (lines ~548-643 region
— note `activity`/`milestones` are interleaved with these and must NOT
move), and the current `ActivityAdminCommands` class in full (added across
last night's four tasks) — every one of its six methods.

- [ ] **Step 2: Write the failing tests**

Extend `tests/test_cli.py`'s `FetchCommands` child-set assertion to expect:
- A child named `"admin"` (a group) whose children are exactly
  `{"status", "test-card", "server-health", "changes", "permissions",
  "integrations", "member", "newcomers", "inactive", "returning",
  "recognition-candidates", "member-delete", "exclude-channel",
  "exclusions"}` (14 names).
- No direct `FetchCommands` children named any of the 9 old flat admin
  command names.
- No `ActivityAdminCommands`-named child (`"activity-admin"`) at all.
- A flat `FetchCommands` child still named `"member-export"` (unchanged
  location, per the design spec).

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py -v -k admin`
Expected: FAIL — `AdminCommands` doesn't exist, `activity-admin` still present

- [ ] **Step 4: Implement `AdminCommands`, delete `ActivityAdminCommands`**

Create `AdminCommands(app_commands.Group)` (name="admin") with exactly the
same delegation-pattern shape as `SniffCommands` in Task 1. Move each of the
14 methods listed above into it verbatim, applying the same
`self.X` → `self._parent.X` transformation. **Do not change any command's
leaf `name=` string** — `name="status"` inside `AdminCommands` becomes
`/fetch admin status`, no renaming needed since none of these collide with
"admin" the way `sniff` collided with its own group name.

Delete the `ActivityAdminCommands` class entirely. Move its `member_export`
method (unchanged) to become a flat method directly on `FetchCommands`,
positioned near `activity`/`milestones` (same staff-or-self shape, same
`self._activity_actor`-based resolution — no `self._parent.` translation
needed here since it's now on `FetchCommands` itself, not a nested group;
change every `self._parent.X` in its current body back to plain `self.X`).

Update `FetchCommands.__init__`: remove
`self.add_command(ActivityAdminCommands(self))`, add
`self.add_command(AdminCommands(self))`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_activity_commands.py -v`
Expected: PASS, including every pre-existing test

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/bot.py tests/test_cli.py tests/test_activity_commands.py
git commit -m "feat: consolidate general/activity-admin commands into /fetch admin group"
```

---

## Task 3: Open `latest`/`schedule` to all members, final verification

**Files:**
- Modify: `src/krubit/services/foundation.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_foundation_service.py` (or wherever `FoundationService`
  is tested — check first), `tests/test_cli.py`

**Interfaces:**
- Produces: `FoundationService.authorize_member(guild_id: int, *, action:
  str) -> None` (checks only guild-enabled, not manage_guild).
  `FetchCommands.authorize_public(interaction, action) -> tuple[discord.Guild,
  int] | None` (mirrors `authorize()`'s shape, calls `authorize_member`
  instead of `authorize_manager`).

- [ ] **Step 1: Write the failing tests**

For `FoundationService.authorize_member`: a test proving it succeeds for a
non-manager actor in an enabled guild, and raises `GuildDisabledError` for a
disabled guild regardless of actor. Match `tests/test_foundation_service.py`'s
(or equivalent's) existing conventions for testing `authorize_manager` — read
that test first.

For `latest`/`schedule`: read the existing tests for these two commands
(search `tests/` for `"fetch_latest"`/`"fetch_schedule"` or the command
methods' names) and add a case proving a non-staff `discord.Member` (no
`manage_guild`) can now successfully invoke them, alongside the existing
staff-can-invoke case which must still pass unmodified.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest -k "authorize_member or latest or schedule" -v`
Expected: FAIL — `authorize_member` doesn't exist yet

- [ ] **Step 3: Implement `authorize_member` and `authorize_public`**

Add to `src/krubit/services/foundation.py`, near `authorize_manager`:

```python
    async def authorize_member(self, guild_id: int, *, action: str) -> None:
        """Like `authorize_manager`, but for commands any guild member may use --
        only checks the guild is enabled, never requires Manage Guild."""
        await self._require_enabled(guild_id, action=action)
```

Add to `src/krubit/discord/bot.py`'s `FetchCommands`, near `authorize`:

```python
    async def authorize_public(
        self, interaction: discord.Interaction, action: str
    ) -> tuple[discord.Guild, int] | None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command is server-only.", ephemeral=True)
            return None
        user = interaction.user
        try:
            await self._service.authorize_member(interaction.guild_id, action=action)
        except GuildDisabledError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return None
        await interaction.response.defer(ephemeral=True, thinking=True)
        return interaction.guild, user.id
```

Update the `latest` and `schedule` command methods (now living wherever
Task 2 left them — check, they should still be flat on `FetchCommands` since
they were never part of the admin/sniff consolidation): change their
`context = await self.authorize(interaction, "fetch_latest")` (and
`"fetch_schedule"`) calls to `self.authorize_public(...)` instead, and
**remove the `@app_commands.default_permissions(manage_guild=True)`
decorator from both** — that decorator is what hides a command from
non-managers in Discord's own UI, and leaving it in place would mean
`authorize_public`'s relaxed check is unreachable for exactly the members it
was built for.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest -v -k "authorize_member or latest or schedule"`
Expected: PASS

- [ ] **Step 5: Full-suite, tree-shape, and final verification**

Add one final test to `tests/test_cli.py`: construct a `FetchCommands`
instance directly and assert `len(fetch_commands.commands) == 9` (exact
count), and that the child names are exactly `{"sniff", "admin", "backup",
"live", "creator", "notifications", "activity", "milestones",
"member-export"}` — the Completion Gate's core claim, verified structurally
rather than only by absence-of-old-names checks.

Run the complete suite:
```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m pyright src/krubit/discord/bot.py src/krubit/services/foundation.py
```

Expected: full suite green (1103 baseline + this plan's new tests), ruff
clean, no new pyright error categories in the touched files.

- [ ] **Step 6: Commit**

```bash
git add src/krubit/services/foundation.py src/krubit/discord/bot.py tests/
git commit -m "feat: open /fetch latest and /fetch schedule to all guild members"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers the `sniff` group + rename. Task 2 covers
  the `admin` group, `ActivityAdminCommands` deletion, and correctly
  special-cases `member-export` staying flat (staff-or-self, not
  admin-only) rather than being swept into `admin` along with its five
  former `activity-admin` siblings — this was caught during plan-writing as
  a real risk (naively moving all six `activity-admin` methods into `admin`
  would have hidden `member-export` from the self-service members who need
  it). Task 3 covers the `latest`/`schedule` authority change and the final
  9-child structural proof.
- **Placeholder scan:** Task 1's five command bodies are fully transcribed,
  real code. Task 2 deliberately does NOT re-transcribe all 14+1 moved
  method bodies (they're read-and-relocated verbatim per the stated
  mechanical rule, proven correct twice already in this codebase) — this is
  a "verify against real code, apply a proven transformation" instruction,
  not a placeholder, matching how last night's plan handled the identical
  situation for `ActivityAdminCommands`.
- **Type consistency:** `authorize_member`/`authorize_public` return shapes
  match `authorize_manager`/`authorize`'s exactly (`None` on failure,
  `tuple[discord.Guild, int]` on success), so `latest`/`schedule`'s
  existing `if context is None: return` handling needs no changes beyond
  the one line swapping which method is called.

## Addendum: `retention`/`community-pulse` (found during Task 3)

The original plan never resolved these two -- the design spec's own
conversation history called them "borderline, undecided," and the plan's
command mapping tables never listed them at all. Task 3 correctly caught
this gap rather than guessing. **Resolution, confirmed with the user:** fold
both into `/fetch admin`, exactly like the other 14 staff-facing commands --
`AdminCommands` grows to 16 children, `FetchCommands` ends at **11** direct
children (`sniff`, `admin`, `backup`, `live`, `creator`, `notifications`,
`activity`, `milestones`, `member-export`, `latest`, `schedule`), not the
originally-stated 9.
