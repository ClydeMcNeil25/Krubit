# Phase 4 Command Surface Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Phase 4's orphaned `returning_member_view`, `recognition_candidates`,
member deletion, member export, and channel-exclusion capabilities to six new
`/fetch` commands, per
`docs/superpowers/specs/2026-08-08-phase-4-command-surface-gaps-design.md`.

**Architecture:** New methods on the existing `ActivityCommandService`
(`src/krubit/discord/activity_commands.py`), matching its established
authority/rendering shapes exactly, with new `@app_commands.command` methods
on `FetchCommands` (`src/krubit/discord/bot.py`) wiring them to Discord.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite, pytest.

## Addendum: `ActivityAdminCommands` subgroup (Discord's 25-child cap)

**Found during Task 1 implementation, confirmed and resolved — see
`docs/superpowers/specs/2026-08-08-phase-4-command-surface-gaps-design.md`'s
own addendum for full reasoning.** `FetchCommands` already has 24 direct
children; Discord enforces a hard 25-child-per-group limit. All six
commands in this plan (Tasks 1-4) now nest under one new subgroup instead of
being flat `/fetch <name>` commands, adding exactly one new child to
`FetchCommands` (25/25 total, not over). No existing command's path changes.

**Task 1 must add this class** to `src/krubit/discord/bot.py`, matching
`BackupCommands`' exact delegation shape (`src/krubit/discord/bot.py:712`):

```python
class ActivityAdminCommands(app_commands.Group):
    """`/fetch activity-admin` -- Phase 4 activity-ledger maintenance commands
    (returning members, recognition candidates, member deletion/export,
    channel-exclusion configuration) that don't fit as flat `/fetch <name>`
    commands due to Discord's 25-child-per-group cap on `FetchCommands`
    itself."""

    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(
            name="activity-admin", description="Activity-ledger maintenance commands"
        )
        self._parent = parent
```

Register it once in `FetchCommands.__init__`, alongside the existing
`self.add_command(BackupCommands(self))` etc. calls:
```python
self.add_command(ActivityAdminCommands(self))
```

**Every command method in this plan (Tasks 1-4) is a method on
`ActivityAdminCommands`, not `FetchCommands`.** Every reference to `self.X`
in this plan's original wiring snippets (`self.authorize`, `self.finish`,
`self._activity_commands`, `self._inactivity_threshold`) must be
`self._parent.X` instead, matching `BackupCommands.status`'s exact delegation
pattern (`self._parent.authorize(...)`, `self._parent.snapshots`). The one
exception is `_present_result` (Task 2) — that's a module-level function
imported into `bot.py`, not a `FetchCommands` method, so it's called directly
as `_present_result(...)`, no `self._parent.` prefix, from within
`ActivityAdminCommands` methods too (same module, same import).

Command *names* (`name="returning"`, `name="member-delete"`, etc.) are
unchanged from every snippet below — only the class they're methods on, and
the `self.` → `self._parent.` prefix on delegated calls, change. Full
invocation becomes `/fetch activity-admin returning`,
`/fetch activity-admin member-delete`, etc.

## Global Constraints

- Every staff-only command checks `actor.is_staff` (or, for `member delete`'s
  two-call confirm flow, all authority/existence checks) before any storage
  query — matching the module's own documented "denied before any query"
  discipline.
- `member delete` requires `confirm=True` on a second call to actually
  delete; the first call only returns a preview. The resulting receipt
  contains no table list or row count — only `receipt_id`/`created_at`.
- `member export`'s self-view path writes no audit receipt; its
  staff-on-behalf-of path writes one via `store.record_activity_receipt`
  before returning the export.
- `exclude-channel` records the real invoking staff member's Discord ID as
  `excluded_by` — not the bot's own application ID (the existing gap this
  plan fixes for this one new call site; `ActivityRuntime`'s own seeding
  behavior is untouched).
- List-rendering commands (`returning`, `recognition-candidates`,
  `exclusions`) cap at a fixed entry count and append `"...and N more"`
  rather than risk exceeding Discord's 4096-character embed description
  limit.
- Run `./.venv/Scripts/python.exe -m pytest -q`,
  `./.venv/Scripts/python.exe -m ruff check .`, and
  `./.venv/Scripts/python.exe -m pyright <touched files>` before every
  commit (NOT `uv run` — unreliable in this environment). Must not break any
  of the 1079 currently-passing tests.

---

## Task 1: `/fetch returning` and `/fetch recognition-candidates`

**Files:**
- Modify: `src/krubit/discord/activity_commands.py` (two new methods)
- Modify: `src/krubit/discord/bot.py` (two new `@app_commands.command` methods)
- Test: `tests/test_activity_commands.py` (extend)

**Interfaces:**
- Consumes: `returning_member_view(store, guild_id, inactivity_threshold, now)`
  (`services/activity_views.py:307`), `recognition_candidates(guild_id,
  events, window, now)` (`services/milestones.py:280`),
  `store.list_ledger_events_for_guild(guild_id)`.
- Produces: `ActivityCommandService.returning(*, actor:
  ActivityActorContext, inactivity_threshold: timedelta) -> CommandResult`,
  `ActivityCommandService.recognition_candidates(*, actor:
  ActivityActorContext) -> CommandResult`.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_activity_commands.py` in full first to match its exact
fixture/import style (the `store` fixture, `NOW` constant, how
`newcomer_view`/`inactive_view` fixtures are seeded) before writing these.

```python
async def test_returning_denies_non_staff(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.returning(actor=actor, inactivity_threshold=timedelta(days=14))
    assert result.status == CommandStatus.DENIED


async def test_returning_renders_real_entries(store):
    # Seed a member whose participation_trend shows returning=True, matching
    # the fixture shape in tests/test_activity_views.py's own returning_member_view
    # test (join, active period, gap, active period again).
    ...
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=99, is_staff=True)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.returning(actor=actor, inactivity_threshold=timedelta(days=14))
    assert result.status == CommandStatus.SUCCEEDED
    assert result.card is not None
    assert f"<@{_RETURNING_MEMBER_ID}>" in result.card.description


async def test_returning_truncates_past_entry_cap(store):
    # Seed more than the cap (see Step 3's _MAX_LIST_ENTRIES constant) of
    # returning members; assert the card description contains "...and" and
    # the count field still reports the true total, not the truncated count.
    ...


async def test_recognition_candidates_denies_non_staff(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.recognition_candidates(actor=actor)
    assert result.status == CommandStatus.DENIED


async def test_recognition_candidates_renders_reasons(store):
    # Reuse tests/test_milestones.py's FIXTURE_EVENTS-style seeding via
    # store.record_ledger_event so recognition_candidates(...) has real
    # candidates with non-empty reasons.
    ...
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=99, is_staff=True)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.recognition_candidates(actor=actor)
    assert result.status == CommandStatus.SUCCEEDED
    assert result.card is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k "returning or recognition"`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Implement the two service methods**

Add to `src/krubit/discord/activity_commands.py`, near `newcomers`/`inactive`:

```python
_MAX_LIST_ENTRIES = 40  # keeps every list-rendering command's embed
# description well under Discord's 4096-character limit even for large
# guilds; no existing /fetch command guards this today, so this is a new,
# deliberately conservative cap rather than an inherited convention.


def _render_capped_lines(lines: list[str], total: int) -> str:
    if not lines:
        return "None found."
    if total > len(lines):
        lines = [*lines[:_MAX_LIST_ENTRIES], f"...and {total - _MAX_LIST_ENTRIES} more."]
    return "\n".join(lines)


# -- returning: staff-only guild-wide returning-member view -------------------

async def returning(
    self, *, actor: ActivityActorContext, inactivity_threshold: timedelta
) -> CommandResult:
    if not actor.is_staff:
        return _denied()
    now = self._now()
    entries = await returning_member_view(
        self._store, actor.guild_id, inactivity_threshold, now
    )
    lines = [
        f"<@{e.member_id}> — {e.trend.active_day_count} active days, "
        f"{e.trend.channel_diversity} channels (trailing "
        f"{cohort_window_days(e.trend.window)} days)"
        for e in entries[:_MAX_LIST_ENTRIES]
    ]
    description = _render_capped_lines(lines, len(entries))
    card = Card(
        kind="fetched",
        title="Fetched: Returning Members",
        description=description,
        fields=(CardField("Count", str(len(entries)), True),),
    )
    return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"count": len(entries)})


# -- recognition-candidates: staff-only guild-wide recognition shortlist ------

async def recognition_candidates(self, *, actor: ActivityActorContext) -> CommandResult:
    if not actor.is_staff:
        return _denied()
    now = self._now()
    events = await self._store.list_ledger_events_for_guild(actor.guild_id)
    candidates = recognition_candidates_fn(
        actor.guild_id, events, _RECOGNITION_WINDOW, now
    )
    lines = [
        f"<@{c.member_id}> — {', '.join(c.reasons)}"
        for c in candidates[:_MAX_LIST_ENTRIES]
    ]
    description = _render_capped_lines(lines, len(candidates))
    card = Card(
        kind="fetched",
        title="Fetched: Recognition Candidates",
        description=description,
        fields=(CardField("Count", str(len(candidates)), True),),
    )
    return CommandResult(
        CommandStatus.SUCCEEDED, card=card, detail={"count": len(candidates)}
    )
```

Add imports: `from krubit.services.activity_views import returning_member_view`
(check the exact existing import block for `newcomer_view`/`inactive_view` and
add alongside it), `from krubit.services.milestones import recognition_candidates
as recognition_candidates_fn` (aliased to avoid shadowing the method name), and
a module constant `_RECOGNITION_WINDOW = CohortWindow.THIRTY_DAY` near the
existing `_ACTIVITY_TREND_WINDOW`/`_COMMUNITY_PULSE_WINDOW` constants.

- [ ] **Step 4: Wire the two Discord-layer commands**

Add to `src/krubit/discord/bot.py`, near `newcomers`/`inactive`:

```python
@app_commands.command(
    name="returning", description="Fetch guild-wide returning members"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def returning(self, interaction: discord.Interaction) -> None:
    context = await self.authorize(interaction, "fetch_returning")
    if context is None:
        return
    guild, actor_id = context
    actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
    result = await self._activity_commands.returning(
        actor=actor, inactivity_threshold=self._inactivity_threshold
    )
    embed = render_card(result.card) if result.card is not None else discord.Embed(
        title=result.status.value
    )
    await self.finish(
        interaction,
        action="fetch_returning",
        actor_id=actor_id,
        embed=embed,
        detail=_receipt_detail(result.detail),
    )


@app_commands.command(
    name="recognition-candidates", description="Fetch guild-wide recognition candidates"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def recognition_candidates(self, interaction: discord.Interaction) -> None:
    context = await self.authorize(interaction, "fetch_recognition_candidates")
    if context is None:
        return
    guild, actor_id = context
    actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
    result = await self._activity_commands.recognition_candidates(actor=actor)
    embed = render_card(result.card) if result.card is not None else discord.Embed(
        title=result.status.value
    )
    await self.finish(
        interaction,
        action="fetch_recognition_candidates",
        actor_id=actor_id,
        embed=embed,
        detail=_receipt_detail(result.detail),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v`
Expected: PASS, including every pre-existing test in the file

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py
git commit -m "feat: add /fetch returning and /fetch recognition-candidates commands"
```

---

## Task 2: `/fetch member delete`

**Files:**
- Modify: `src/krubit/discord/activity_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_activity_commands.py`

**Interfaces:**
- Consumes: `activity_privacy.delete_member(store, guild_id, member_id, *,
  requested_by, now) -> ActivityReceipt` (`services/activity_privacy.py:152`),
  `_confirmation(*, title, description, **fields)` (check its exact import
  path in `content_commands.py` and reuse it, do not redefine).
- Produces: `ActivityCommandService.delete_member(*, actor:
  ActivityActorContext, target: ActivityActorContext, confirm: bool = False)
  -> CommandResult`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_delete_member_denies_non_staff(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.delete_member(actor=actor, target=target)
    assert result.status == CommandStatus.DENIED


async def test_delete_member_first_call_requires_confirmation(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=True)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.delete_member(actor=actor, target=target)
    assert result.status == CommandStatus.CONFIRMATION_REQUIRED
    # nothing deleted yet
    assert await store.list_ledger_events(_GUILD_ID, member_id=2) != ()  # if seeded


async def test_delete_member_confirm_true_deletes_and_returns_minimal_receipt(store):
    # Seed some ledger_events/milestones for member 2 first.
    ...
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=True)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.delete_member(actor=actor, target=target, confirm=True)
    assert result.status == CommandStatus.SUCCEEDED
    assert await store.list_ledger_events(_GUILD_ID, member_id=2) == ()
    assert "receipt_id" in result.detail
    assert "table" not in str(result.detail).lower()
    assert "row" not in str(result.detail).lower()


async def test_delete_member_confirm_true_is_idempotent(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=True)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    first = await service.delete_member(actor=actor, target=target, confirm=True)
    second = await service.delete_member(actor=actor, target=target, confirm=True)
    assert first.status == CommandStatus.SUCCEEDED
    assert second.status == CommandStatus.SUCCEEDED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k delete_member`
Expected: FAIL — method doesn't exist

- [ ] **Step 3: Implement the service method**

First, read `content_commands.py`'s `_confirmation` helper's exact signature
and import it (do not redefine it in `activity_commands.py`). Add:

```python
# -- member delete: staff-only, irreversible, two-call confirm ----------------

async def delete_member(
    self,
    *,
    actor: ActivityActorContext,
    target: ActivityActorContext,
    confirm: bool = False,
) -> CommandResult:
    """Staff-triggered, irreversible deletion of one member's ledger data.

    Per the design spec's Privacy Controls section, deletion is staff-only --
    unlike `activity`/`milestones`, there is no self-view/self-delete path.
    """
    if not actor.is_staff:
        return _denied()
    if not confirm:
        card = _confirmation(
            title="Delete Member Data",
            description=(
                f"Permanently delete all activity-ledger data for "
                f"<@{target.member_id}>? This cannot be undone."
            ),
            Member=f"<@{target.member_id}>",
        )
        return CommandResult(
            CommandStatus.CONFIRMATION_REQUIRED,
            card=card,
            detail={"member_id": target.member_id},
        )
    now = self._now()
    receipt = await delete_member_fn(
        self._store,
        target.guild_id,
        target.member_id,
        requested_by=actor.member_id,
        now=now,
    )
    card = Card(
        kind="fetched",
        title="Fetched: Member Data Deleted",
        description=f"Deleted activity-ledger data for <@{target.member_id}>.",
        fields=(
            CardField("Receipt ID", receipt.receipt_id, True),
            CardField("Deleted At", receipt.created_at.isoformat(), True),
        ),
    )
    return CommandResult(
        CommandStatus.SUCCEEDED,
        card=card,
        detail={"receipt_id": receipt.receipt_id, "member_id": target.member_id},
    )
```

Import `delete_member as delete_member_fn` from
`krubit.services.activity_privacy` (aliased to avoid shadowing the method
name).

- [ ] **Step 4: Wire the Discord-layer command**

Confirmed: the real, reusable confirmation-button helper is
`_present_result(interaction, result, *, confirm=None)`, a module-level
function in `src/krubit/discord/content_commands.py:863`, already documented
as "Shared by `CreatorCommands` and `NotificationCommands` so confirmation
rendering stays identical across every mutating command" — this plan's
`member-delete` command is the third consumer of it, not a new pattern.
**It calls `interaction.followup.send(...)` internally, so the caller must
`await interaction.response.defer(ephemeral=True, thinking=True)` first** —
unlike `self.finish`, which handles its own response lifecycle.

Add `_present_result` to `bot.py`'s existing
`from krubit.discord.content_commands import (...)` block (`bot.py:19`), then:

```python
@app_commands.command(
    name="member-delete", description="Permanently delete a member's activity-ledger data"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def member_delete(
    self, interaction: discord.Interaction, member: discord.Member
) -> None:
    context = await self.authorize(interaction, "fetch_member_delete")
    if context is None:
        return
    guild, actor_id = context
    actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
    target = ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
    result = await self._activity_commands.delete_member(actor=actor, target=target)
    await _present_result(
        interaction,
        result,
        confirm=lambda: self._activity_commands.delete_member(
            actor=actor, target=target, confirm=True
        ),
    )
```

Note this command's authority check happens via `self.authorize(...)`, which
already calls `interaction.response.defer(ephemeral=True, thinking=True)`
internally (confirm this against `authorize`'s real body before relying on
it — it is documented as doing so in the module's own commentary on
`authorize`) — so `_present_result`'s `interaction.followup.send` call has a
deferred response to attach to either way. This command deliberately does
**not** call `self.finish` (which writes its own action receipt via
`ActionReceipt` — a different, coarser receipt than `delete_member`'s own
`ActivityReceipt`, and calling both would double-record the same action
under two different receipt tables) — `_present_result` alone is the
correct, complete response path here, matching how `notification_retract`'s
own Discord-layer caller uses `_present_result` alone rather than combining
it with `self.finish`-equivalent bookkeeping.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v`
Expected: PASS, including every pre-existing test

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py
git commit -m "feat: add /fetch member-delete with confirmation and minimal receipt"
```

---

## Task 3: `/fetch member export`

**Files:**
- Modify: `src/krubit/discord/activity_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_activity_commands.py`

**Interfaces:**
- Consumes: `activity_privacy.export_member_data(store, guild_id, member_id,
  now) -> MemberExportPackage` (`services/activity_privacy.py:198`),
  `store.record_activity_receipt(...)` (`storage/sqlite.py:4091`).
- Produces: `ActivityCommandService.export_member(*, actor:
  ActivityActorContext, target: ActivityActorContext) -> tuple[CommandResult,
  bytes | None]` — returns the JSON bytes separately from `CommandResult`
  since this is the first `/fetch` command whose payload doesn't fit in an
  embed at all; the Discord layer sends the bytes as a file attachment.

- [ ] **Step 1: Write the failing tests**

```python
import json

async def test_export_member_denies_non_staff_non_self(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result, payload = await service.export_member(actor=actor, target=target)
    assert result.status == CommandStatus.DENIED
    assert payload is None


async def test_export_member_self_view_writes_no_receipt(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result, payload = await service.export_member(actor=actor, target=actor)
    assert result.status == CommandStatus.SUCCEEDED
    assert payload is not None
    decoded = json.loads(payload)
    assert decoded["member_id"] == 1
    # No activity_receipts row should exist for this export.
    ...


async def test_export_member_staff_on_behalf_writes_audit_receipt(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=True)
    target = ActivityActorContext(guild_id=_GUILD_ID, member_id=2, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result, payload = await service.export_member(actor=actor, target=target)
    assert result.status == CommandStatus.SUCCEEDED
    assert payload is not None
    # An activity_receipts row for action "member_data_exported" should now exist.
    ...


async def test_export_member_json_never_includes_another_members_data(store):
    # Seed events for members 2 and 3; export member 2; assert member 3's
    # member_id never appears anywhere in the decoded JSON.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k export_member`
Expected: FAIL — method doesn't exist

- [ ] **Step 3: Implement the service method**

```python
import json
from dataclasses import asdict
from enum import Enum

# -- member export: staff-or-self, JSON payload (first non-embed /fetch command) --

def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


async def export_member(
    self, *, actor: ActivityActorContext, target: ActivityActorContext
) -> tuple[CommandResult, bytes | None]:
    self_view = target.member_id == actor.member_id
    if not self_view and not actor.is_staff:
        return _denied_self_or_staff(), None
    now = self._now()
    package = await export_member_data_fn(
        self._store, target.guild_id, target.member_id, now
    )
    if not self_view:
        await self._store.record_activity_receipt(
            guild_id=actor.guild_id,
            member_id=target.member_id,
            action="member_data_exported",
            detail={"requested_by": actor.member_id, "member_id": target.member_id},
            now=now,
        )
    payload_dict = asdict(package)
    payload = json.dumps(payload_dict, default=_json_default, indent=2).encode("utf-8")
    card = Card(
        kind="fetched",
        title="Fetched: Member Data Export",
        description=(
            f"Export generated for <@{target.member_id}> "
            f"({len(package.events)} events, {len(package.milestones)} milestones)."
        ),
        fields=(),
    )
    return (
        CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"self_view": self_view, "event_count": len(package.events)},
        ),
        payload,
    )
```

Import `export_member_data as export_member_data_fn` from
`krubit.services.activity_privacy` (aliased). Check `_denied_self_or_staff`'s
exact existing name/import in this file (used by `activity`/`milestones`
already) and reuse it rather than redefining.

**Before relying on `record_activity_receipt`'s exact signature**, read it at
`storage/sqlite.py:4091` and match its real parameter names/order — the
signature sketched above (`guild_id, member_id, action, detail, now`) is
illustrative and must be verified against the actual method, not assumed.

- [ ] **Step 4: Wire the Discord-layer command with a file attachment**

This is the first `/fetch` command sending a Discord file attachment — there
is no existing precedent in this codebase to copy, so build it using
discord.py's standard `discord.File` API directly:

```python
@app_commands.command(
    name="member-export", description="Export a member's activity-ledger data, or your own"
)
@app_commands.guild_only()
async def member_export(
    self, interaction: discord.Interaction, member: discord.Member | None = None
) -> None:
    resolved = await self._activity_actor(interaction)
    if resolved is None:
        return
    guild, actor = resolved
    target = (
        ActivityActorContext(guild_id=guild.id, member_id=member.id, is_staff=False)
        if member is not None
        else actor
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    result, payload = await self._activity_commands.export_member(actor=actor, target=target)
    embed = render_card(result.card) if result.card is not None else discord.Embed(
        title=result.status.value
    )
    if payload is None:
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    file = discord.File(io.BytesIO(payload), filename=f"krubit-export-{target.member_id}.json")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)
```

Add `import io` to `bot.py`'s imports if not already present.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v`
Expected: PASS, including every pre-existing test

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py
git commit -m "feat: add /fetch member-export with staff-on-behalf audit receipt"
```

---

## Task 4: `/fetch exclude-channel` and `/fetch exclusions`

**Files:**
- Modify: `src/krubit/discord/activity_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_activity_commands.py`

**Interfaces:**
- Consumes: `store.save_exclusion_entry(ExclusionEntry) -> ExclusionEntry`
  (`storage/sqlite.py:3994`), `store.list_exclusion_entries(guild_id) ->
  tuple[ExclusionEntry, ...]` (`storage/sqlite.py:4034`), `ExclusionEntry`
  (`domain/activity_ledger.py:519`).
- Produces: `ActivityCommandService.exclude_channel(*, actor:
  ActivityActorContext, channel_id: int, reason: str) -> CommandResult`,
  `ActivityCommandService.exclusions(*, actor: ActivityActorContext) ->
  CommandResult`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_exclude_channel_denies_non_staff(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.exclude_channel(actor=actor, channel_id=555, reason="mod-only")
    assert result.status == CommandStatus.DENIED


async def test_exclude_channel_records_real_staff_member_not_bot_id(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=42, is_staff=True)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.exclude_channel(actor=actor, channel_id=555, reason="mod-only")
    assert result.status == CommandStatus.SUCCEEDED
    entries = await store.list_exclusion_entries(_GUILD_ID)
    assert entries[0].excluded_by == 42


async def test_exclusions_denies_non_staff(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=False)
    service = ActivityCommandService(store, now=lambda: NOW)
    result = await service.exclusions(actor=actor)
    assert result.status == CommandStatus.DENIED


async def test_exclusions_renders_empty_and_populated_state(store):
    actor = ActivityActorContext(guild_id=_GUILD_ID, member_id=1, is_staff=True)
    service = ActivityCommandService(store, now=lambda: NOW)
    empty_result = await service.exclusions(actor=actor)
    assert "none" in empty_result.card.description.lower()

    await service.exclude_channel(actor=actor, channel_id=555, reason="mod-only")
    populated = await service.exclusions(actor=actor)
    assert "555" in populated.card.description
    assert "mod-only" in populated.card.description
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v -k "exclude"`
Expected: FAIL — methods don't exist

- [ ] **Step 3: Implement the two service methods**

```python
# -- exclude-channel: staff-only, records the real invoking staff member ------

async def exclude_channel(
    self, *, actor: ActivityActorContext, channel_id: int, reason: str
) -> CommandResult:
    if not actor.is_staff:
        return _denied()
    now = self._now()
    entry = ExclusionEntry(
        guild_id=actor.guild_id,
        channel_id=channel_id,
        excluded_by=actor.member_id,
        reason=reason,
        excluded_at=now,
    )
    saved = await self._store.save_exclusion_entry(entry)
    card = Card(
        kind="fetched",
        title="Fetched: Channel Excluded",
        description=f"<#{saved.channel_id}> excluded: {saved.reason}",
        fields=(CardField("Excluded By", f"<@{saved.excluded_by}>", True),),
    )
    return CommandResult(
        CommandStatus.SUCCEEDED, card=card, detail={"channel_id": channel_id}
    )


# -- exclusions: staff-only, read-only companion to exclude-channel -----------

async def exclusions(self, *, actor: ActivityActorContext) -> CommandResult:
    if not actor.is_staff:
        return _denied()
    entries = await self._store.list_exclusion_entries(actor.guild_id)
    lines = [
        f"<#{e.channel_id}> — {e.reason} (excluded by <@{e.excluded_by}> "
        f"at {e.excluded_at.isoformat()})"
        for e in entries[:_MAX_LIST_ENTRIES]
    ]
    description = _render_capped_lines(lines, len(entries))
    card = Card(
        kind="fetched",
        title="Fetched: Channel Exclusions",
        description=description,
        fields=(CardField("Count", str(len(entries)), True),),
    )
    return CommandResult(CommandStatus.SUCCEEDED, card=card, detail={"count": len(entries)})
```

Import `ExclusionEntry` from `krubit.domain.activity_ledger` if not already
imported in this file.

- [ ] **Step 4: Wire the two Discord-layer commands**

```python
@app_commands.command(
    name="exclude-channel", description="Exclude a channel from activity-ledger tracking"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def exclude_channel(
    self, interaction: discord.Interaction, channel: discord.TextChannel, reason: str
) -> None:
    context = await self.authorize(interaction, "fetch_exclude_channel")
    if context is None:
        return
    guild, actor_id = context
    actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
    result = await self._activity_commands.exclude_channel(
        actor=actor, channel_id=channel.id, reason=reason
    )
    embed = render_card(result.card) if result.card is not None else discord.Embed(
        title=result.status.value
    )
    await self.finish(
        interaction,
        action="fetch_exclude_channel",
        actor_id=actor_id,
        embed=embed,
        detail=_receipt_detail(result.detail),
    )


@app_commands.command(
    name="exclusions", description="Fetch guild-wide activity-ledger channel exclusions"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def exclusions(self, interaction: discord.Interaction) -> None:
    context = await self.authorize(interaction, "fetch_exclusions")
    if context is None:
        return
    guild, actor_id = context
    actor = ActivityActorContext(guild_id=guild.id, member_id=actor_id, is_staff=True)
    result = await self._activity_commands.exclusions(actor=actor)
    embed = render_card(result.card) if result.card is not None else discord.Embed(
        title=result.status.value
    )
    await self.finish(
        interaction,
        action="fetch_exclusions",
        actor_id=actor_id,
        embed=embed,
        detail=_receipt_detail(result.detail),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_activity_commands.py -v`
Expected: PASS, including every pre-existing test

- [ ] **Step 6: Run the complete test suite, linter, and type checker**

```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m pyright src/krubit/discord/activity_commands.py src/krubit/discord/bot.py
```

Expected: full suite green (1079 baseline + this plan's new tests), ruff
clean, no new pyright error categories in the touched files.

- [ ] **Step 7: Commit**

```bash
git add src/krubit/discord/activity_commands.py src/krubit/discord/bot.py tests/test_activity_commands.py
git commit -m "feat: add /fetch exclude-channel and /fetch exclusions commands"
```

---

## Self-Review Notes

- **Spec coverage:** all six commands from the design spec are covered —
  Task 1 (returning, recognition-candidates), Task 2 (member-delete), Task 3
  (member-export), Task 4 (exclude-channel, exclusions).
- **Placeholder scan:** every step contains real code. The one deliberate
  exception is Task 2 Step 4's confirmation-button wiring, which explicitly
  instructs reading the real existing helper's name from `content_commands.py`
  rather than guessing it — this is a "verify against real code" instruction,
  not a placeholder, since inventing a plausible-but-wrong helper name would
  be worse than flagging it for the implementer to confirm.
- **Type consistency:** `ActivityActorContext`, `CommandResult`,
  `CommandStatus` are used identically to their existing definitions
  throughout; `export_member`'s `tuple[CommandResult, bytes | None]` return
  shape is a deliberate, documented deviation from every other method's
  bare-`CommandResult` return, justified by it being the first command whose
  payload doesn't fit an embed.

## Execution

Given the scope (4 tasks, no live external dependencies, well-grounded in
existing tested code) and that this plan is being executed without the human
partner present, this will run via subagent-driven-development exactly as
Phase 2's callback server work did: fresh implementer per task, task-scoped
review after each, fix loops as needed, and a final whole-branch review
before considering it done. No merge or push happens without the human
partner explicitly present to approve it.
