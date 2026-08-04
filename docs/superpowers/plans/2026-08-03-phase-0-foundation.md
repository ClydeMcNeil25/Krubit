# Krubit Phase 0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an installable, non-moderating Krubit foundation that isolates Discord guilds, deduplicates authorized events, exposes `/fetch status`, renders test cards, stores redacted receipts, and emits a schema-valid Zariya test signal.

**Architecture:** A Python 3.13 package separates domain contracts, redaction, SQLite persistence, application services, and the Discord adapter. Discord is an external edge; the core is testable without a live gateway. SQLite rows always carry `guild_id`, and repository APIs require guild scope so tenant isolation is structural rather than optional.

**Tech Stack:** Python 3.13, uv, discord.py 2.7.1, aiosqlite 0.22.1, pytest, pytest-asyncio, Ruff, Pyright.

## Global Constraints

- Krubit is non-conversational; outputs are functional cards and command results only.
- Phase 0 performs no moderation, member profiling, creator polling, or server mutation.
- Bot tokens are environment-only and never stored in SQLite, logs, signals, cards, or configuration exports.
- Every stored event payload and receipt detail passes through credential redaction.
- Every persistence query for tenant data requires a Discord `guild_id`.
- Duplicate `(guild_id, event_id)` input produces one stored event and one accepted outcome.
- `/fetch status` is guild-only; `/fetch test-card` requires Manage Guild.
- Use only the `guilds` Gateway intent in Phase 0; privileged intents remain disabled.
- Discord install scopes are `bot` and `applications.commands`; permissions are View Channels, Send Messages, Embed Links, and Read Message History.
- Zariya integration is outbound contract generation only; Phase 0 does not modify KAI-System or KSHQ.

---

## File Structure

```text
src/krubit/
  __init__.py              Package version.
  __main__.py              CLI entry point.
  config.py                Environment-only runtime settings.
  domain/models.py         Guild events, receipts, status, and card values.
  security/redaction.py    Recursive credential redaction.
  storage/sqlite.py        Schema, tenant-scoped persistence, and deduplication.
  contracts/zariya.py      Versioned Zariya signal serializer/validator.
  services/foundation.py   Phase 0 application orchestration.
  discord/cards.py         Functional Discord embed rendering.
  discord/install.py       Least-privilege install URL and intent declarations.
  discord/bot.py           Discord client and `/fetch` command adapter.
tests/
  test_redaction.py
  test_storage.py
  test_zariya_contract.py
  test_foundation_service.py
  test_discord_cards.py
  test_discord_install.py
  test_config.py
  test_cli.py
```

### Task 1: Project scaffold and runtime contract

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `src/krubit/__init__.py`
- Create: `tests/test_config.py`
- Create: `src/krubit/config.py`

**Interfaces:**
- Produces: `Settings.from_env(environ: Mapping[str, str]) -> Settings`
- Produces: `Settings.require_token() -> str`

- [ ] Write tests proving a valid numeric application ID and explicit database path load without reading a token, an invalid application ID is rejected, and `require_token` raises when `DISCORD_KRUBIT_BOT_TOKEN` is absent.
- [ ] Run `uv run pytest tests/test_config.py -q` and verify failure because `krubit.config` does not exist.
- [ ] Add the package metadata, pinned dependencies, tool configuration, environment example, and minimal `Settings` dataclass using only supplied environment mappings.
- [ ] Run `uv sync --all-groups` and `uv run pytest tests/test_config.py -q`; verify all tests pass.
- [ ] Commit with `git commit -m "build: scaffold Krubit foundation"`.

### Task 2: Redacted domain contracts

**Files:**
- Create: `tests/test_redaction.py`
- Create: `src/krubit/domain/__init__.py`
- Create: `src/krubit/domain/models.py`
- Create: `src/krubit/security/__init__.py`
- Create: `src/krubit/security/redaction.py`

**Interfaces:**
- Produces: `redact(value: JSONValue) -> JSONValue`
- Produces immutable `GuildEvent`, `ActionReceipt`, `StatusSnapshot`, and `Card` dataclasses.

- [ ] Write tests proving Discord-shaped tokens, assignment secrets, nested dictionary values, and list values are redacted while ordinary text and non-string primitives are preserved.
- [ ] Run `uv run pytest tests/test_redaction.py -q`; verify failure because the module is missing.
- [ ] Implement recursive redaction and validated immutable domain values. Reject blank IDs and nonpositive guild IDs.
- [ ] Run `uv run pytest tests/test_redaction.py -q`; verify all tests pass.
- [ ] Commit with `git commit -m "feat: add redacted domain contracts"`.

### Task 3: Tenant-scoped SQLite event and receipt store

**Files:**
- Create: `tests/test_storage.py`
- Create: `src/krubit/storage/__init__.py`
- Create: `src/krubit/storage/sqlite.py`

**Interfaces:**
- Produces: `SQLiteStore.open(path: Path) -> SQLiteStore`
- Produces: `initialize() -> None`
- Produces: `accept_event(event: GuildEvent) -> bool`
- Produces: `get_event(guild_id: int, event_id: str) -> GuildEvent | None`
- Produces: `record_receipt(receipt: ActionReceipt) -> None`
- Produces: `list_receipts(guild_id: int, limit: int = 50) -> list[ActionReceipt]`
- Produces: `set_guild_enabled(guild_id: int, enabled: bool) -> None`
- Produces: `guild_is_enabled(guild_id: int) -> bool`

- [ ] Write async integration tests with real temporary SQLite files proving schema initialization, duplicate rejection within one guild, acceptance of the same event ID in a different guild, tenant-scoped reads, recursive redaction before storage, and tenant-scoped receipt listing.
- [ ] Run `uv run pytest tests/test_storage.py -q`; verify failure because storage is missing.
- [ ] Implement normalized SQLite tables with primary/unique keys that include `guild_id`, WAL mode, foreign keys, bounded receipt listing, and JSON serialization after redaction.
- [ ] Run `uv run pytest tests/test_storage.py -q`; verify all tests pass.
- [ ] Commit with `git commit -m "feat: add tenant-scoped event store"`.

### Task 4: Versioned Zariya signal contract

**Files:**
- Create: `tests/test_zariya_contract.py`
- Create: `src/krubit/contracts/__init__.py`
- Create: `src/krubit/contracts/zariya.py`

**Interfaces:**
- Produces: `ZariyaSignal.create_test(guild_id: int, source_event_id: str, occurred_at: datetime) -> ZariyaSignal`
- Produces: `ZariyaSignal.to_dict() -> dict[str, JSONValue]`
- Produces: `ZariyaSignal.from_dict(payload: Mapping[str, object]) -> ZariyaSignal`
- Schema identifier: `krubit.zariya-signal.v1`

- [ ] Write tests proving literal schema output, UTC timestamps, guild/source provenance, round-trip validation, rejection of unknown schema versions, and recursive redaction of evidence.
- [ ] Run `uv run pytest tests/test_zariya_contract.py -q`; verify failure because the contract is missing.
- [ ] Implement a deny-by-default signal contract whose Phase 0 test signal carries `severity=info`, `kind=foundation_test`, no member data, and no action request.
- [ ] Run `uv run pytest tests/test_zariya_contract.py -q`; verify all tests pass.
- [ ] Commit with `git commit -m "feat: define Zariya signal contract"`.

### Task 5: Foundation application service

**Files:**
- Create: `tests/test_foundation_service.py`
- Create: `src/krubit/services/__init__.py`
- Create: `src/krubit/services/foundation.py`

**Interfaces:**
- Consumes: `SQLiteStore`, `GuildEvent`, `ActionReceipt`, `ZariyaSignal`
- Produces: `FoundationService.ingest(event: GuildEvent) -> IngestResult`
- Produces: `FoundationService.status(guild_id: int) -> StatusSnapshot`
- Produces: `FoundationService.test_card(guild_id: int, actor_id: int, can_manage_guild: bool) -> Card`
- Produces: `FoundationService.test_signal(guild_id: int, actor_id: int, can_manage_guild: bool) -> ZariyaSignal`

- [ ] Write async tests proving disabled guilds reject ingestion, enabled guilds accept once and report duplicates, status contains only the requested guild's counts, ordinary members cannot create test cards/signals, administrators receive functional cards/signals, and each command outcome stores a redacted receipt.
- [ ] Run `uv run pytest tests/test_foundation_service.py -q`; verify failure because the service is missing.
- [ ] Implement orchestration without importing Discord classes. Use explicit `AuthorizationError` and `GuildDisabledError` failures.
- [ ] Run `uv run pytest tests/test_foundation_service.py -q`; verify all tests pass.
- [ ] Commit with `git commit -m "feat: add Phase 0 foundation service"`.

### Task 6: Discord cards, intents, and install permissions

**Files:**
- Create: `tests/test_discord_cards.py`
- Create: `tests/test_discord_install.py`
- Create: `src/krubit/discord/__init__.py`
- Create: `src/krubit/discord/cards.py`
- Create: `src/krubit/discord/install.py`

**Interfaces:**
- Produces: `render_card(card: Card) -> discord.Embed`
- Produces: `phase_zero_intents() -> discord.Intents`
- Produces: `phase_zero_permissions() -> discord.Permissions`
- Produces: `install_url(application_id: int) -> str`

- [ ] Write tests proving card title/description/fields and brand color, only the guilds intent is enabled, only four Phase 0 permissions are requested, and the install URL contains the application ID plus bot/application-command scopes.
- [ ] Run both test files and verify failure because the Discord adapter modules are missing.
- [ ] Implement pure rendering and least-privilege declarations using discord.py 2.7.1.
- [ ] Run both test files and verify all tests pass.
- [ ] Commit with `git commit -m "feat: add Discord presentation boundary"`.

### Task 7: Discord bot and `/fetch` command adapter

**Files:**
- Create: `src/krubit/discord/bot.py`
- Create: `tests/test_cli.py`
- Create: `src/krubit/__main__.py`

**Interfaces:**
- Produces: `KrubitBot(settings: Settings, service: FoundationService)`
- Registers guild-only `/fetch status` and Manage-Guild-only `/fetch test-card`.
- Produces CLI commands `run`, `init-db`, `install-url`, `enable-guild`, `status`, and `emit-test-signal`.

- [ ] Write CLI tests invoking the real parser/service against a temporary database to prove database initialization, guild enablement, JSON status output, install URL output, and redacted JSON test-signal output without requiring a Discord connection.
- [ ] Run `uv run pytest tests/test_cli.py -q`; verify failure because the entry point is missing.
- [ ] Implement the Discord client as a thin adapter over `FoundationService`, ephemeral failures, safe command synchronization, and CLI operations. Refuse `run` when the token is absent.
- [ ] Run `uv run pytest tests/test_cli.py -q`; verify all tests pass.
- [ ] Run `uv run python -m krubit --help` and verify the six commands are listed.
- [ ] Commit with `git commit -m "feat: expose Krubit bot and CLI"`.

### Task 8: Operational documentation and full verification

**Files:**
- Modify: `README.md`
- Create: `docs/operations/phase-0-setup.md`
- Create: `docs/contracts/krubit-zariya-signal-v1.md`

**Interfaces:**
- Documents exact Developer Portal settings, environment variables, install steps, test-guild enablement, commands, data locations, backup expectations, and Phase 0 exclusions.

- [ ] Document creation of a server-installable Discord application, default install scopes and permissions, non-privileged `guilds` intent, token handling, `uv sync`, database initialization, guild enablement, install URL generation, bot startup, and smoke checks.
- [ ] Document the Zariya signal fields, redaction guarantee, example test payload, and the fact that transport to KSHQ is not active in Phase 0.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest -q`; fix every error and warning.
- [ ] Run a two-guild CLI smoke test against a temporary database and verify status/event/receipt isolation.
- [ ] Run `git diff --check` and `git status --short`; verify only intended files remain.
- [ ] Commit with `git commit -m "docs: add Phase 0 operations guide"`.

## Completion Criteria

- All automated tests pass on Python 3.13.
- Static analysis and formatting checks pass without warnings.
- Krubit can generate a least-privilege install URL and start when a valid token is supplied.
- `/fetch status` and `/fetch test-card` are registered through the Discord adapter.
- Two guilds remain isolated in configuration, events, counts, and receipts.
- Duplicate events are idempotent.
- Stored and emitted content is redacted.
- A schema-valid test signal can be generated for Zariya without modifying KAI-System.
- No Phase 1+ behavior exists: no moderation, member profiling, external creator monitoring, or Discord mutation.
