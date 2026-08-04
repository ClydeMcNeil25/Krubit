# Phase 1 Reliable Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Krubit's read-only Phase 1 monitoring, snapshot, health, backup-preview, staff command, and daily-summary capabilities without overlapping Zariya's human judgment.

**Architecture:** Discord adapters normalize gateway events and guild inventory into framework-independent domain records. Focused snapshot, comparison, and health services persist guild-scoped evidence in SQLite and render factual staff-only cards through thin `/fetch` command adapters. Restore support stops at a computed preview; no production interface can mutate Discord.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite 0.22.1, pytest 9, pytest-asyncio 1.4.0, Ruff, Pyright strict mode.

## Global Constraints

- Krubit is non-conversational and produces functional cards only.
- Zariya owns recommendations, community framing, member interaction, and moderation decisions.
- Every persistent operation is scoped by `guild_id`; no cross-guild reads or writes.
- Phase 1 requests no Administrator, Manage Guild, Manage Roles, Manage Channels, Manage Webhooks, or moderation permissions.
- Hidden Discord resources become explicit coverage limitations rather than silent omissions.
- Restore preview must not contain or call a Discord mutation interface.
- Staff commands require Manage Guild and respond ephemerally.
- Stored payloads use the existing secret-redaction boundary.
- Daily delivery is disabled unless `KRUBIT_STAFF_CHANNEL_ID` is configured.
- No member activity scoring, message-content monitoring, risk classification, creator feeds, or autonomous moderation belongs in Phase 1.

---

## File Map

- `src/krubit/domain/companion.py`: snapshot, diff, coverage, and health value objects.
- `src/krubit/storage/sqlite.py`: guild-scoped Phase 1 schema and persistence.
- `src/krubit/discord/inventory.py`: Discord guild-to-stable-inventory normalization.
- `src/krubit/services/snapshots.py`: snapshot hashing, versioning, comparison, and restore preview.
- `src/krubit/services/health.py`: factual permission, integration, and server-health reports.
- `src/krubit/discord/events.py`: deterministic gateway event conversion.
- `src/krubit/discord/bot.py`: handlers, staff commands, and scheduled-summary adapter.
- `src/krubit/discord/cards.py`: bounded Phase 1 embeds.
- `src/krubit/config.py`: optional staff-channel configuration.
- `src/krubit/discord/install.py`: members intent and View Audit Log install permission.
- `docs/operations/phase-1-operations.md`: portal, migration, smoke-test, and rollback procedure.

### Task 1: Phase 1 domain records and guild-scoped persistence

**Files:**
- Create: `src/krubit/domain/companion.py`
- Modify: `src/krubit/storage/sqlite.py`
- Create: `tests/test_companion_storage.py`

**Interfaces:**
- Produces: `CoverageIssue`, `SnapshotRecord`, `SnapshotDiff`, `HealthFinding`, `HealthReport` dataclasses.
- Produces: `SQLiteStore.save_snapshot(guild_id: int, content: dict[str, JSONValue], coverage: tuple[CoverageIssue, ...], captured_at: datetime) -> SnapshotRecord`.
- Produces: `SQLiteStore.latest_snapshot(guild_id: int) -> SnapshotRecord | None`, `SQLiteStore.get_snapshot(guild_id: int, snapshot_id: str) -> SnapshotRecord | None`, and `SQLiteStore.list_events(guild_id: int, limit: int = 25) -> list[GuildEvent]`.

- [ ] **Step 1: Write failing model and persistence tests**

```python
@pytest.mark.asyncio
async def test_snapshot_versions_are_deduplicated_and_guild_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    captured = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    try:
        first = await store.save_snapshot(111, {"roles": [{"id": "1"}]}, (), captured)
        duplicate = await store.save_snapshot(111, {"roles": [{"id": "1"}]}, (), captured)
        other = await store.save_snapshot(222, {"roles": [{"id": "1"}]}, (), captured)

        assert first.snapshot_id == duplicate.snapshot_id
        assert first.version == duplicate.version == 1
        assert other.snapshot_id != first.snapshot_id
        assert await store.get_snapshot(222, first.snapshot_id) is None
    finally:
        await store.close()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/test_companion_storage.py -q`

Expected: FAIL because `krubit.domain.companion` and `save_snapshot` do not exist.

- [ ] **Step 3: Add validated domain dataclasses and non-destructive schema migration**

```sql
CREATE TABLE IF NOT EXISTS configuration_snapshots (
    guild_id INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, snapshot_id),
    UNIQUE (guild_id, version),
    UNIQUE (guild_id, content_hash)
);
```

Implement SHA-256 over canonical redacted JSON (`sort_keys=True`, compact separators), use `snapshot:{uuid4().hex}` IDs, and return the existing row when `(guild_id, content_hash)` already exists.

- [ ] **Step 4: Add event-list and snapshot retrieval tests, then implement their queries**

```python
events = await store.list_events(111, limit=2)
assert [event.guild_id for event in events] == [111, 111]
assert await store.latest_snapshot(333) is None
```

Run: `uv run pytest tests/test_companion_storage.py tests/test_storage.py -q`

Expected: PASS with tenant isolation and existing Phase 0 storage behavior preserved.

- [ ] **Step 5: Commit the persistence slice**

```powershell
git add src/krubit/domain/companion.py src/krubit/storage/sqlite.py tests/test_companion_storage.py
git commit -m "feat: persist guild-scoped configuration snapshots"
```

### Task 2: Stable Discord inventory capture

**Files:**
- Create: `src/krubit/discord/inventory.py`
- Create: `tests/test_discord_inventory.py`

**Interfaces:**
- Consumes: `CoverageIssue` and `JSONValue`.
- Produces: `InventoryCapture(content: dict[str, JSONValue], coverage: tuple[CoverageIssue, ...])`.
- Produces: `async capture_inventory(guild: discord.Guild, *, required_permissions: discord.Permissions, configured_channel_id: int | None) -> InventoryCapture`.

- [ ] **Step 1: Write a failing normalization test using narrow Discord-boundary fakes**

```python
@pytest.mark.asyncio
async def test_capture_inventory_sorts_resources_and_marks_forbidden_sections() -> None:
    capture = await capture_inventory(
        fake_guild(webhooks_error=discord.Forbidden(fake_response(), "missing access")),
        required_permissions=phase_one_permissions(),
        configured_channel_id=900,
    )

    assert [role["id"] for role in capture.content["roles"]] == ["10", "20"]
    assert capture.content["configured_channel"] == {"id": "900", "present": False}
    assert capture.coverage[0].section == "webhooks"
    assert capture.coverage[0].status == "limited"
```

- [ ] **Step 2: Run the inventory test and verify RED**

Run: `uv run pytest tests/test_discord_inventory.py -q`

Expected: FAIL because `capture_inventory` does not exist.

- [ ] **Step 3: Implement deterministic normalization**

Normalize roles, categories/channels, overwrites, Scheduled Events, visible AutoMod rules, visible webhooks, guild identity, bot permissions, and configured-channel resolution. Store IDs as decimal strings, permission values as decimal strings, timestamps as UTC ISO 8601 strings, and sort every resource list by numeric ID. Webhook records may contain only ID, channel ID, name, type, and application ID; do not persist URLs or tokens.

- [ ] **Step 4: Add partial-failure and stability tests**

```python
first = await capture_inventory(fake_guild(role_order=(20, 10)), required_permissions=perms, configured_channel_id=None)
second = await capture_inventory(fake_guild(role_order=(10, 20)), required_permissions=perms, configured_channel_id=None)
assert first.content == second.content
assert first.coverage == second.coverage
```

Run: `uv run pytest tests/test_discord_inventory.py -q`

Expected: PASS; a forbidden subsection is represented in coverage while other sections remain usable.

- [ ] **Step 5: Commit inventory capture**

```powershell
git add src/krubit/discord/inventory.py tests/test_discord_inventory.py
git commit -m "feat: capture stable Discord server inventory"
```

### Task 3: Snapshot comparison and non-mutating restore preview

**Files:**
- Create: `src/krubit/services/snapshots.py`
- Create: `tests/test_snapshot_service.py`

**Interfaces:**
- Consumes: `SQLiteStore`, `InventoryCapture`, `SnapshotRecord`, and `SnapshotDiff`.
- Produces: `SnapshotService.capture(guild_id: int, inventory: InventoryCapture, captured_at: datetime) -> SnapshotRecord`.
- Produces: `SnapshotService.compare(guild_id: int, older_id: str, newer_id: str) -> SnapshotDiff`.
- Produces: `SnapshotService.preview_restore(guild_id: int, target_id: str, current: InventoryCapture) -> SnapshotDiff`.

- [ ] **Step 1: Write failing ID-aware comparison tests**

```python
diff = compare_inventory(
    {"channels": [{"id": "10", "name": "old-name"}]},
    {"channels": [{"id": "10", "name": "new-name"}, {"id": "20", "name": "new"}]},
)
assert [(item.resource_id, item.change) for item in diff.items] == [
    ("20", "added"),
    ("10", "modified"),
]
assert diff.items[1].fields == {"name": {"before": "old-name", "after": "new-name"}}
```

- [ ] **Step 2: Run the comparison test and verify RED**

Run: `uv run pytest tests/test_snapshot_service.py -q`

Expected: FAIL because comparison services do not exist.

- [ ] **Step 3: Implement generic resource comparison and snapshot orchestration**

Compare `roles`, `channels`, `scheduled_events`, `automod_rules`, `webhooks`, and scalar Krubit settings. Key resource arrays by ID, classify added/removed/modified, include only changed fields, and sort output by section, change, and numeric resource ID.

- [ ] **Step 4: Prove restore preview cannot mutate Discord**

```python
preview = await service.preview_restore(111, target.snapshot_id, current_capture)
assert preview.direction == "current_to_target"
assert not hasattr(service, "apply_restore")
assert await store.latest_snapshot(111) == target
```

Run: `uv run pytest tests/test_snapshot_service.py tests/test_companion_storage.py -q`

Expected: PASS; preview computes current-to-target changes without writing a snapshot or exposing an apply method.

- [ ] **Step 5: Commit snapshot comparison**

```powershell
git add src/krubit/services/snapshots.py tests/test_snapshot_service.py
git commit -m "feat: compare snapshots and preview restores"
```

### Task 4: Factual server, permission, and integration health

**Files:**
- Create: `src/krubit/services/health.py`
- Create: `tests/test_health_service.py`

**Interfaces:**
- Consumes: `SnapshotRecord`, `CoverageIssue`, `HealthFinding`, `HealthReport`.
- Produces: `HealthService.server_health(snapshot: SnapshotRecord | None, *, now: datetime, database_healthy: bool, gateway_ready: bool) -> HealthReport`.
- Produces: `HealthService.permission_health(snapshot: SnapshotRecord) -> HealthReport` and `HealthService.integration_health(snapshot: SnapshotRecord) -> HealthReport`.

- [ ] **Step 1: Write failing health-classification tests**

```python
report = service.server_health(
    snapshot_with(
        bot_permissions={"missing_required": ["view_audit_log"]},
        coverage=[CoverageIssue("webhooks", "limited", "forbidden")],
    ),
    now=datetime(2026, 8, 4, tzinfo=UTC),
    database_healthy=True,
    gateway_ready=True,
)
assert report.status == "warning"
assert [finding.code for finding in report.findings] == [
    "missing_required_permission",
    "limited_webhook_coverage",
]
```

- [ ] **Step 2: Run health tests and verify RED**

Run: `uv run pytest tests/test_health_service.py -q`

Expected: FAIL because `HealthService` does not exist.

- [ ] **Step 3: Implement deterministic factual findings**

Use severity precedence `critical > warning > limited > healthy`. Produce findings for missing snapshot, snapshot older than 26 hours, database unavailable, gateway unavailable, missing required permissions, missing configured channel, renamed configured channel, limited webhook/AutoMod visibility, and collection errors. Finding text states evidence and timestamp only; it must not prescribe a community or moderation response.

- [ ] **Step 4: Add healthy, stale, and guild-independent report tests**

Run: `uv run pytest tests/test_health_service.py -q`

Expected: PASS with stable finding ordering and no recommendation fields.

- [ ] **Step 5: Commit health services**

```powershell
git add src/krubit/services/health.py tests/test_health_service.py
git commit -m "feat: report factual server and integration health"
```

### Task 5: Gateway event collectors and Phase 1 access surface

**Files:**
- Create: `src/krubit/discord/events.py`
- Modify: `src/krubit/discord/install.py`
- Modify: `src/krubit/discord/bot.py`
- Create: `tests/test_discord_events.py`
- Modify: `tests/test_discord_install.py`

**Interfaces:**
- Consumes: `GuildEvent` and `FoundationService.ingest(event)`.
- Produces: `guild_event(event_type: str, guild_id: int, entity_id: int, occurred_at: datetime, before: Mapping[str, JSONValue] | None, after: Mapping[str, JSONValue] | None) -> GuildEvent`.
- Produces: `phase_one_intents()` and `phase_one_permissions()`.

- [ ] **Step 1: Write failing event-ID and permission tests**

```python
event = guild_event("role_updated", 111, 222, occurred, {"name": "A"}, {"name": "B"})
replay = guild_event("role_updated", 111, 222, occurred, {"name": "A"}, {"name": "B"})
assert event.event_id == replay.event_id
assert phase_one_intents().members is True
assert phase_one_permissions().view_audit_log is True
assert phase_one_permissions().manage_guild is False
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_discord_events.py tests/test_discord_install.py -q`

Expected: FAIL because Phase 1 factories do not exist.

- [ ] **Step 3: Implement stable event conversion and least-privilege access**

Hash canonical event type/entity/time/before/after data into the event ID. Enable Guilds and Guild Members intents. Add View Audit Log to the Phase 0 permission set while keeping all mutation permissions false.

- [ ] **Step 4: Add thin bot handlers**

Add `on_member_join`, `on_member_remove`, `on_member_update` for role changes, `on_guild_role_create/update/delete`, `on_guild_channel_create/update/delete`, `on_guild_update`, `on_scheduled_event_create/update/delete`, `on_automod_rule_create/update/delete`, `on_automod_action`, and `on_webhooks_update`. Each handler converts only factual IDs/fields and calls one shared safe-ingest method that ignores disabled guilds but receipts supported failures.

- [ ] **Step 5: Test handler ingestion and replay deduplication**

```python
await bot.on_guild_role_update(before_role, after_role)
await bot.on_guild_role_update(before_role, after_role)
assert (await service.status(111)).event_count == 1
```

Run: `uv run pytest tests/test_discord_events.py tests/test_discord_install.py tests/test_foundation_service.py -q`

Expected: PASS with one stored outcome for repeated event data.

- [ ] **Step 6: Commit gateway collection**

```powershell
git add src/krubit/discord/events.py src/krubit/discord/install.py src/krubit/discord/bot.py tests/test_discord_events.py tests/test_discord_install.py
git commit -m "feat: collect Phase 1 Discord change events"
```

### Task 6: Staff-only Phase 1 `/fetch` cards and backup commands

**Files:**
- Modify: `src/krubit/discord/bot.py`
- Modify: `src/krubit/discord/cards.py`
- Create: `tests/test_phase_one_commands.py`
- Modify: `tests/test_discord_cards.py`

**Interfaces:**
- Consumes: `SnapshotService`, `HealthService`, `capture_inventory`, and `FoundationService` authorization/receipts.
- Produces: staff-only `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations`, `/fetch backup status`, `/fetch backup create`, and `/fetch backup preview`.
- Produces: `render_health_card(report: HealthReport) -> discord.Embed` and `render_diff_card(diff: SnapshotDiff, *, title: str) -> discord.Embed`.

- [ ] **Step 1: Write failing card-boundary tests**

```python
embed = render_diff_card(diff_with(30), title="Fetched: Server Changes")
assert len(embed.fields) <= 25
assert "5 additional changes" in (embed.footer.text or "")
assert len(embed.description or "") <= 4096
```

- [ ] **Step 2: Run card tests and verify RED**

Run: `uv run pytest tests/test_discord_cards.py tests/test_phase_one_commands.py -q`

Expected: FAIL because Phase 1 renderers and commands do not exist.

- [ ] **Step 3: Implement bounded factual cards and command service methods**

Every command validates guild context, enabled guild state, and Manage Guild authority; records succeeded/denied/failed receipts; and sends an ephemeral response. Defer the interaction before inventory/API work, then edit the original ephemeral response. `backup preview` defaults to the latest saved snapshot and rejects an unknown snapshot ID visibly.

- [ ] **Step 4: Test authorization, ephemerality, receipts, and no mutation path**

```python
assert command.callback.__discord_app_commands_default_permissions__.manage_guild is True
assert response.ephemeral is True
assert latest_receipt.action == "fetch_backup_preview"
assert not any(name.startswith("apply") for name in dir(snapshot_service))
```

Run: `uv run pytest tests/test_phase_one_commands.py tests/test_discord_cards.py -q`

Expected: PASS for all seven new commands and embed constraints.

- [ ] **Step 5: Commit the staff command surface**

```powershell
git add src/krubit/discord/bot.py src/krubit/discord/cards.py tests/test_phase_one_commands.py tests/test_discord_cards.py
git commit -m "feat: add Phase 1 staff fetch commands"
```

### Task 7: Once-daily summary generation and optional delivery

**Files:**
- Modify: `src/krubit/config.py`
- Modify: `src/krubit/storage/sqlite.py`
- Create: `src/krubit/services/daily_summary.py`
- Modify: `src/krubit/discord/bot.py`
- Modify: `.env.example`
- Create: `tests/test_daily_summary.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.staff_channel_id: int | None` parsed from `KRUBIT_STAFF_CHANNEL_ID`.
- Produces: `DailySummaryService.claim(guild_id: int, summary_date: date) -> bool` backed by a `(guild_id, summary_date)` primary key.
- Produces: `DailySummaryService.generate(guild_id: int, report: HealthReport, summary_date: date) -> DailySummaryResult`.

- [ ] **Step 1: Write failing optional-config and uniqueness tests**

```python
assert settings_from({"KRUBIT_STAFF_CHANNEL_ID": ""}).staff_channel_id is None
assert settings_from({"KRUBIT_STAFF_CHANNEL_ID": "123"}).staff_channel_id == 123
assert await service.claim(111, date(2026, 8, 4)) is True
assert await service.claim(111, date(2026, 8, 4)) is False
assert await service.claim(222, date(2026, 8, 4)) is True
```

- [ ] **Step 2: Run daily-summary tests and verify RED**

Run: `uv run pytest tests/test_daily_summary.py tests/test_config.py -q`

Expected: FAIL because staff-channel settings and summary claims do not exist.

- [ ] **Step 3: Implement claim persistence and delivery outcomes**

Add `daily_summaries(guild_id, summary_date, status, channel_id, created_at)` with a composite primary key. Outcomes are `delivery_disabled`, `channel_missing`, `permission_missing`, `sent`, or `failed`, and each outcome creates an action receipt without leaking exception secrets.

- [ ] **Step 4: Attach a UTC scheduler without enabling unsolicited delivery**

Use `discord.ext.tasks.loop(time=datetime.time(hour=12, tzinfo=UTC))`. Start it in `setup_hook`, cancel it in `close`, iterate enabled connected guilds, build the same health report used by `/fetch server-health`, and send only when `staff_channel_id` resolves to a writable guild text channel.

- [ ] **Step 5: Test disabled delivery and duplicate prevention**

```python
first = await runner.run_for_guild(guild, date(2026, 8, 4))
second = await runner.run_for_guild(guild, date(2026, 8, 4))
assert first.status == "delivery_disabled"
assert second.status == "already_claimed"
assert fake_channel.sent == []
```

Run: `uv run pytest tests/test_daily_summary.py tests/test_config.py -q`

Expected: PASS; absent configuration causes no Discord message.

- [ ] **Step 6: Commit daily summaries**

```powershell
git add .env.example src/krubit/config.py src/krubit/storage/sqlite.py src/krubit/services/daily_summary.py src/krubit/discord/bot.py tests/test_config.py tests/test_daily_summary.py
git commit -m "feat: generate deduplicated daily health summaries"
```

### Task 8: Operations documentation, full verification, and live-safe rollout

**Files:**
- Modify: `README.md`
- Create: `docs/operations/phase-1-operations.md`
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`

**Interfaces:**
- Consumes: all Phase 1 commands, settings, migration behavior, and Discord access requirements.
- Produces: exact operator procedures and rollback criteria; no new runtime API.

- [ ] **Step 1: Update version labels and write the operations guide**

Document database migration, Server Members Intent enablement, View Audit Log permission check, command smoke tests, initial snapshot, Zariya inventory comparison, staff-channel opt-in, canary observations, and rollback by stopping the Phase 1 runtime and restarting the prior commit without deleting the forward-compatible database.

- [ ] **Step 2: Run all automated verification**

Run:

```powershell
uv run pytest -q
uv run ruff check .
uv run pyright
git diff --check
```

Expected: all tests pass; Ruff reports `All checks passed!`; Pyright reports zero errors; diff check is empty.

- [ ] **Step 3: Inspect the migration against a temporary copy of the live database**

Copy `data/krubit.db` to a task-specific temporary directory, run `krubit init-db` against the copy, and verify Phase 0 counts plus new tables remain intact. Never edit or delete the live database during this check.

- [ ] **Step 4: Perform the live-safe Discord rollout**

Enable Server Members Intent in the Developer Portal, verify/install View Audit Log permission, stop the single verified Krubit runtime chain, start the new runtime through `scripts/invoke-krubit.ps1`, and confirm exactly one launcher-managed worker chain with an empty stderr log.

- [ ] **Step 5: Run live staff smoke checks**

In Krucial Town, run `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations`, `/fetch backup status`, `/fetch backup create`, and `/fetch backup preview`. Confirm each response is ephemeral, snapshots remain integrity-valid, previews make no Discord changes, and receipts increase exactly once per command.

- [ ] **Step 6: Compare factual inventory with Zariya**

Compare category, channel, role, Scheduled Event, AutoMod, and access-limitation counts with `D:/Dropbox/05 Software Development/KAI-System/agents/zariya_kessari/runtime_memory/community_manager/server_audit_latest.json`. Record differences as coverage notes only; do not change Zariya or Discord.

- [ ] **Step 7: Commit documentation and rollout state**

```powershell
git add README.md docs/operations/phase-1-operations.md docs/roadmaps/2026-08-03-krubit-phase-rollout.md
git commit -m "docs: add Phase 1 operations and canary guide"
```

- [ ] **Step 8: Apply completion verification**

Confirm all eight task commits exist, `git status --short` is empty, one live worker chain remains, stderr is empty, database status is healthy, and every acceptance criterion in the design has direct test or smoke evidence.
