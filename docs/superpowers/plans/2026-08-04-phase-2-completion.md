# Krubit Phase 2 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Krubit's multi-platform Creator Signal and Notification Hub while preserving the accepted Twitch behavior and honestly disabling capabilities that lack credentials or official access.

**Architecture:** Add a platform-neutral creator registry, normalized content ledger, policy/correlation layer, and durable Discord delivery pipeline. Official platform adapters emit shared content events; Discord presence, polling, push/webhook ingestion, Scheduled Events, commands, analytics, and health all consume the same durable contracts.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiohttp 3.14.3, aiosqlite 0.22.1, truststore 0.10.4, pytest 9, pytest-asyncio 1.4.0, Ruff, Pyright strict.

## Global Constraints

- Accept creator profile/channel URLs as the enrollment input; do not require users to choose a platform manually.
- Route verified live content to `#live-notifications` and published social content to `#social-notifications` using stored Discord IDs after bootstrap.
- Preserve the accepted Krubit alien-language Twitch card and safe `Streaming Now` ownership behavior.
- Apply `@everyone` to verified live starts by default; do not apply `@everyone` to social content by default.
- Use only official APIs, push/webhook interfaces, and documented Discord presence data; never scrape or automate a browser.
- Store application/user secrets separately from creator account metadata and never expose token values in Discord, logs, exports, tests, or receipts.
- Keep guild data, creator ownership, routes, budgets, cursors, deliveries, and analytics isolated by `guild_id`.
- Baseline existing content when an account is enrolled; never announce historical items discovered during initial synchronization.
- Ignore replies, comments, reposts, ordinary shares, and ephemeral content by default.
- Represent unavailable capabilities as `unconfigured`, `authorization_required`, `approval_required`, `degraded`, `quota_limited`, or `unsupported`; never report them as operational.
- Fanbase and unsupported live APIs must remain dormant and visibly pending until official access exists.
- Every state mutation and Discord action requires a durable success/failure receipt.
- Run each production connector through disabled, shadow, preview, and canary stages independently.
- Do not enable new live/social delivery flags or issue real mentions during implementation without explicit user authorization.

## File Structure

### New production modules

- `src/krubit/domain/creator_signals.py`: platform, capability, creator-account, content-event, route, and delivery value objects.
- `src/krubit/integrations/catalog.py`: URL recognition, normalization, connector metadata, and capability declarations.
- `src/krubit/integrations/base.py`: connector protocol, safe result types, and shared HTTP error taxonomy.
- `src/krubit/integrations/youtube.py`: YouTube account resolution, uploads, scheduled/live lifecycle, and push payload parsing.
- `src/krubit/integrations/x.py`: X account resolution and original-post polling.
- `src/krubit/integrations/bluesky.py`: public profile resolution and author-feed polling.
- `src/krubit/integrations/meta.py`: Instagram, Facebook Page/profile, and Threads authorization-aware reads.
- `src/krubit/integrations/tiktok.py`: authorized Display API video reads and explicit LIVE capability gating.
- `src/krubit/integrations/fanbase.py`: URL recognition and unsupported capability result.
- `src/krubit/security/credential_vault.py`: AES-GCM encryption/decryption for creator OAuth grants using a versioned environment-held key.
- `src/krubit/services/creator_registry.py`: creator ownership, authorization, activation, pause, route, and transfer rules.
- `src/krubit/services/content_signals.py`: ingestion, baseline, lifecycle, correlation, batching, and durable delivery planning.
- `src/krubit/services/notification_policy.py`: quiet hours, mention budgets, route selection, and retry eligibility.
- `src/krubit/services/creator_analytics.py`: guild/staff and creator-self factual delivery, latency, suppression, and connector summaries.
- `src/krubit/discord/content_cards.py`: platform-neutral live/social embeds and buttons.
- `src/krubit/discord/content_runtime.py`: connector jobs, Discord presence ingestion, durable delivery execution, and recovery.
- `src/krubit/discord/content_commands.py`: creator, latest, live, schedule, notification, integration, and analytics commands.
- `src/krubit/discord/scheduled_events.py`: owned Discord Scheduled Event creation, reconciliation, and lifecycle updates.
- `src/krubit/web/callbacks.py`: minimal aiohttp callback server with pluggable YouTube push and OAuth routes, state validation, body limits, and signature hooks.

### Existing modules to modify

- `src/krubit/config.py`: optional connector credentials, feature flags, routes, and webhook base settings.
- `src/krubit/storage/sqlite.py`: additive Phase 2 schema and guild-scoped persistence methods; move creator row decoding helpers into `src/krubit/storage/creator_rows.py` to limit further growth.
- `src/krubit/discord/bot.py`: install the unified runtime/commands and forward presence/lifecycle events.
- `src/krubit/discord/install.py`: calculate `Create Events`/`Manage Events` only when Scheduled Event sync is enabled.
- `src/krubit/discord/inventory.py`: report creator routes and connector capability facts without secrets.
- `src/krubit/services/health.py`: surface connector, cursor, delivery, quota, and authorization states.
- `src/krubit/discord/live_runtime.py`, `src/krubit/services/live_signals.py`, `src/krubit/domain/live_signals.py`: adapt Twitch Phase 2A behind unified contracts without deleting proven migration paths until parity passes.
- `scripts/invoke-krubit.ps1`, `.env.example`, `README.md`: allowlisted optional settings and operator instructions.

---

### Task 1: Platform, capability, and URL catalog

**Files:**
- Create: `src/krubit/domain/creator_signals.py`
- Create: `src/krubit/integrations/catalog.py`
- Test: `tests/test_creator_signal_domain.py`
- Test: `tests/test_connector_catalog.py`

**Interfaces:**
- Produces: `Platform`, `Capability`, `CapabilityState`, `ContentKind`, `ContentState`, `ConnectorDescriptor`, `RecognizedAccountUrl`, and `recognize_account_url(url: str) -> RecognizedAccountUrl`.
- Consumes: no Phase 2 completion interfaces.

- [ ] **Step 1: Write failing domain and URL-recognition tests**

```python
def test_catalog_recognizes_supported_profile_urls() -> None:
    cases = {
        "https://youtube.com/@KrucialStudios": (Platform.YOUTUBE, "KrucialStudios"),
        "https://x.com/KrucialStudios": (Platform.X, "KrucialStudios"),
        "https://www.instagram.com/krucialstudios/": (Platform.INSTAGRAM, "krucialstudios"),
        "https://www.facebook.com/krucialstudios": (Platform.FACEBOOK, "krucialstudios"),
        "https://www.threads.net/@krucialstudios": (Platform.THREADS, "krucialstudios"),
        "https://bsky.app/profile/krucialstudios.bsky.social": (
            Platform.BLUESKY,
            "krucialstudios.bsky.social",
        ),
        "https://www.tiktok.com/@krucialstudios": (Platform.TIKTOK, "krucialstudios"),
        "https://fanbase.app/krucialstudios": (Platform.FANBASE, "krucialstudios"),
    }
    for url, expected in cases.items():
        result = recognize_account_url(url)
        assert (result.platform, result.handle) == expected


def test_catalog_rejects_credentials_fragments_and_lookalike_hosts() -> None:
    for url in (
        "https://user:pass@youtube.com/@safe",
        "https://youtube.com.evil.example/@safe",
        "https://x.com/safe#token=secret",
        "javascript:alert(1)",
    ):
        with pytest.raises(ValueError):
            recognize_account_url(url)
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run: `.venv\Scripts\python.exe -m pytest tests\test_creator_signal_domain.py tests\test_connector_catalog.py -q`

Expected: FAIL during import because the new domain and catalog modules do not exist.

- [ ] **Step 3: Implement frozen enums/value objects and strict host/path normalization**

```python
class Platform(StrEnum):
    TWITCH = "twitch"
    YOUTUBE = "youtube"
    X = "x"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    FACEBOOK_PAGE = "facebook_page"
    THREADS = "threads"
    BLUESKY = "bluesky"
    TIKTOK = "tiktok"
    FANBASE = "fanbase"


class CapabilityState(StrEnum):
    READY = "ready"
    UNCONFIGURED = "unconfigured"
    AUTHORIZATION_REQUIRED = "authorization_required"
    APPROVAL_REQUIRED = "approval_required"
    DEGRADED = "degraded"
    QUOTA_LIMITED = "quota_limited"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class RecognizedAccountUrl:
    platform: Platform
    handle: str
    canonical_url: str
```

Implement an explicit host allowlist and per-platform path parser. Reject non-HTTPS URLs,
userinfo, fragments, ports, IP literals, encoded separators, and unrecognized paths.

- [ ] **Step 4: Run focused tests, Ruff, and Pyright**

Run: `.venv\Scripts\python.exe -m pytest tests\test_creator_signal_domain.py tests\test_connector_catalog.py -q`

Run: `.venv\Scripts\ruff.exe check src\krubit\domain\creator_signals.py src\krubit\integrations\catalog.py tests\test_creator_signal_domain.py tests\test_connector_catalog.py`

Run: `.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the catalog**

```powershell
git add src/krubit/domain/creator_signals.py src/krubit/integrations/catalog.py tests/test_creator_signal_domain.py tests/test_connector_catalog.py
git commit -m "feat: add creator connector catalog"
```

### Task 2: Creator registry persistence and authority

**Files:**
- Modify: `src/krubit/domain/creator_signals.py`
- Create: `src/krubit/storage/creator_rows.py`
- Create: `src/krubit/services/creator_registry.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_creator_registry_storage.py`
- Test: `tests/test_creator_registry_service.py`

**Interfaces:**
- Consumes: `Platform`, `RecognizedAccountUrl`, and `CapabilityState` from Task 1.
- Produces: `CreatorProfile`, `CreatorAccount`, `CreatorRoute`, `CreatorRegistry.add_account(guild_id, actor_member_id, owner_member_id, actor_is_admin, actor_has_creator_role, recognized, resolved_external_id, now)`, `pause_account(guild_id, actor_member_id, account_id, now)`, `resume_account(guild_id, actor_member_id, account_id, now)`, `transfer_account(guild_id, actor_member_id, account_id, new_owner_member_id, now)`, and guild-scoped SQLite methods.

- [ ] **Step 1: Write failing storage tests for isolation, uniqueness, and redaction**

```python
@pytest.mark.asyncio
async def test_creator_accounts_are_guild_scoped_and_stable_id_unique(store: SQLiteStore) -> None:
    first = creator_account(guild_id=111, owner_member_id=222, external_id="UC-one")
    await store.save_creator_account(first)
    await store.save_creator_account(replace(first, guild_id=999, owner_member_id=888))
    assert (await store.get_creator_account(111, first.account_id)).owner_member_id == 222
    assert (await store.get_creator_account(999, first.account_id)).owner_member_id == 888


@pytest.mark.asyncio
async def test_same_platform_identity_cannot_have_two_owners_in_one_guild(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(creator_account(owner_member_id=222))
    with pytest.raises(ValueError, match="already registered"):
        await store.save_creator_account(creator_account(owner_member_id=333))
```

- [ ] **Step 2: Run focused tests and confirm missing persistence methods**

Run: `.venv\Scripts\python.exe -m pytest tests\test_creator_registry_storage.py tests\test_creator_registry_service.py -q`

Expected: FAIL because creator registry tables and service methods are absent.

- [ ] **Step 3: Add additive schema and explicit authority service**

Add tables `creator_profiles`, `creator_accounts`, `creator_routes`,
`connector_authorizations`, and `creator_registry_receipts`. Primary/unique keys include
`guild_id`; authorizations store only opaque secret references and safe expiry/status
facts.

```python
class CreatorRegistry:
    async def add_account(
        self,
        *,
        guild_id: int,
        actor_member_id: int,
        owner_member_id: int,
        actor_is_admin: bool,
        actor_has_creator_role: bool,
        recognized: RecognizedAccountUrl,
        resolved_external_id: str,
        now: datetime,
    ) -> CreatorAccount:
        raise NotImplementedError("implemented by CreatorRegistry in this task")
```

Require `actor_is_admin` for another member's profile. Require
`actor_has_creator_role` for self-service. Store the initial account as paused until its
baseline and required authorization are complete.

- [ ] **Step 4: Run registry tests and the existing storage suite**

Run: `.venv\Scripts\python.exe -m pytest tests\test_creator_registry_storage.py tests\test_creator_registry_service.py tests\test_storage.py tests\test_live_signal_storage.py -q`

Expected: PASS with no changes to existing live-signal records.

- [ ] **Step 5: Commit registry persistence**

```powershell
git add src/krubit/domain/creator_signals.py src/krubit/storage/creator_rows.py src/krubit/storage/sqlite.py src/krubit/services/creator_registry.py tests/test_creator_registry_storage.py tests/test_creator_registry_service.py
git commit -m "feat: persist creator registry"
```

### Task 3: Connector protocol, encrypted credentials, callback ingress, and settings

**Files:**
- Create: `src/krubit/integrations/base.py`
- Create: `src/krubit/security/credential_vault.py`
- Create: `src/krubit/web/__init__.py`
- Create: `src/krubit/web/callbacks.py`
- Modify: `src/krubit/config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.env.example`
- Modify: `scripts/invoke-krubit.ps1`
- Test: `tests/test_connector_base.py`
- Test: `tests/test_credential_vault.py`
- Test: `tests/test_callback_ingress.py`
- Test: `tests/test_config.py`
- Test: `tests/test_launcher_config.py`

**Interfaces:**
- Consumes: Task 1 platform and capability types.
- Produces: `Connector`, `ConnectorAccount`, `ConnectorPage`, `ConnectorFailure`, `ConnectorHealth`, `CredentialVault.seal/open`, `CallbackServer.start/close`, and optional credential settings without requiring them at bot startup.

- [ ] **Step 1: Write failing tests for dormant connectors and secret-safe errors**

```python
def test_missing_social_credentials_do_not_prevent_bot_startup() -> None:
    settings = Settings.from_env(base_env())
    assert settings.youtube_api_key is None
    assert settings.x_bearer_token is None
    assert settings.meta_app_secret is None
    assert settings.tiktok_client_secret is None


def test_connector_failure_never_renders_secret_values() -> None:
    failure = ConnectorFailure.authorization("token abc-secret-value expired")
    assert "abc-secret-value" not in failure.safe_detail


def test_vault_ciphertext_is_versioned_and_does_not_contain_plaintext() -> None:
    sealed = vault().seal(b"creator-refresh-token")
    assert sealed.startswith("v1:")
    assert "creator-refresh-token" not in sealed
    assert vault().open(sealed) == b"creator-refresh-token"


@pytest.mark.asyncio
async def test_callback_ingress_rejects_oversized_or_unregistered_requests(client) -> None:
    assert (await client.post("/callbacks/unknown", data=b"x")).status == 404
    assert (await client.post("/callbacks/youtube", data=b"x" * 1_048_577)).status == 413
```

- [ ] **Step 2: Run the focused tests and confirm absent interfaces/settings**

Run: `.venv\Scripts\python.exe -m pytest tests\test_connector_base.py tests\test_credential_vault.py tests\test_callback_ingress.py tests\test_config.py tests\test_launcher_config.py -q`

Expected: FAIL for missing connector types and optional settings.

- [ ] **Step 3: Implement connector protocol and optional environment settings**

Run: `uv add cryptography==50.0.0`

```python
class Connector(Protocol):
    descriptor: ConnectorDescriptor

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        raise NotImplementedError("implemented by each concrete connector")
    async def fetch_page(
        self, account: CreatorAccount, *, cursor: str | None
    ) -> ConnectorPage:
        raise NotImplementedError("implemented by each concrete connector")
    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        raise NotImplementedError("implemented by each concrete connector")
```

Add optional settings for YouTube API key/push callback secret, X bearer token, Meta app
ID/secret/callback base, TikTok client key/secret/callback base, and independent
`KRUBIT_CREATOR_SIGNALS_ENABLED`/`KRUBIT_SOCIAL_DELIVERY_ENABLED` flags. The launcher
allowlist loads these names but does not require them. Add `cryptography==50.0.0` as a pinned
runtime dependency and use AES-GCM with a random nonce, authenticated version prefix,
and `KRUBIT_CREDENTIAL_ENCRYPTION_KEY`. The key is required only when storing/reading
creator OAuth grants. `CallbackServer` binds only when a callback base URL and port are
configured; it enforces HTTPS public-base validation, 1 MiB bodies, per-route methods,
timeouts, and redacted errors.

- [ ] **Step 4: Run configuration and connector tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_connector_base.py tests\test_credential_vault.py tests\test_callback_ingress.py tests\test_config.py tests\test_launcher_config.py -q`

Expected: PASS; redaction tests prove safe diagnostic rendering.

- [ ] **Step 5: Commit connector contracts**

```powershell
git add src/krubit/integrations/base.py src/krubit/security/credential_vault.py src/krubit/web/__init__.py src/krubit/web/callbacks.py src/krubit/config.py pyproject.toml uv.lock .env.example scripts/invoke-krubit.ps1 tests/test_connector_base.py tests/test_credential_vault.py tests/test_callback_ingress.py tests/test_config.py tests/test_launcher_config.py
git commit -m "feat: define social connector contracts"
```

### Task 4: Normalized content ledger and baseline ingestion

**Files:**
- Modify: `src/krubit/domain/creator_signals.py`
- Create: `src/krubit/services/content_signals.py`
- Modify: `src/krubit/storage/sqlite.py`
- Modify: `src/krubit/storage/creator_rows.py`
- Test: `tests/test_content_signal_storage.py`
- Test: `tests/test_content_signal_service.py`

**Interfaces:**
- Consumes: `CreatorAccount`, connector pages, and connector failures from Tasks 2-3.
- Produces: `ContentEvent`, `ContentObservation`, `ContentDelivery`, `ContentCursor`, `ContentPlan`, `IngestionResult`, and `ContentSignalService.ingest_page(account, page, now)`.

- [ ] **Step 1: Write failing tests for baseline suppression, lifecycle updates, and cursors**

```python
@pytest.mark.asyncio
async def test_first_page_establishes_baseline_without_delivery(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    result = await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    assert result.plans == ()
    assert (await store.get_content_cursor(111, account().account_id)).value == "c1"


@pytest.mark.asyncio
async def test_new_item_after_baseline_claims_one_delivery(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    result = await service.ingest_page(
        account(), page(items=(video("v2"), video("v1")), cursor="c2"), now=LATER
    )
    assert [plan.event.external_id for plan in result.plans] == ["v2"]
```

- [ ] **Step 2: Run focused tests and confirm missing ledger behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_signal_storage.py tests\test_content_signal_service.py -q`

Expected: FAIL because the ledger schema and ingestion service do not exist.

- [ ] **Step 3: Implement additive ledger tables and atomic ingestion**

Add `content_events`, `content_cursors`, `content_deliveries`,
`content_delivery_attempts`, `content_correlations`, and `content_receipts`. Enforce
`UNIQUE(guild_id, platform, external_id)` and transactional cursor/event/delivery claims.

```python
class ContentSignalService:
    async def ingest_page(
        self,
        account: CreatorAccount,
        page: ConnectorPage,
        *,
        now: datetime,
    ) -> IngestionResult:
        raise NotImplementedError("implemented by ContentSignalService in this task")
```

The first successful page sets `baselined_at` and stores identities without claims.
Later pages upsert lifecycle changes and claim exactly one pending delivery per new
publish/live transition.

- [ ] **Step 4: Run ledger, concurrency, and restart tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_signal_storage.py tests\test_content_signal_service.py -q`

Expected: PASS, including concurrent ingestion and reopened-database fixtures.

- [ ] **Step 5: Commit the content ledger**

```powershell
git add src/krubit/domain/creator_signals.py src/krubit/services/content_signals.py src/krubit/storage/sqlite.py src/krubit/storage/creator_rows.py tests/test_content_signal_storage.py tests/test_content_signal_service.py
git commit -m "feat: add durable creator content ledger"
```

### Task 5: Correlation, quiet hours, batching, routes, and mention budgets

**Files:**
- Create: `src/krubit/services/notification_policy.py`
- Modify: `src/krubit/services/content_signals.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_notification_policy.py`
- Test: `tests/test_content_correlation.py`

**Interfaces:**
- Consumes: durable `ContentEvent` and `CreatorRoute` records.
- Produces: `DeliveryDisposition`, `MentionKind`, `DeliveryDecision`, `MentionDecision`, `CorrelationDecision`, `NotificationTemplate`, `NotificationPolicy.evaluate(event, at)`, `validate_template(template)`, and `ContentCorrelator.correlate(first, second)`.

- [ ] **Step 1: Write failing deterministic policy and correlation tests**

```python
def test_social_event_queues_during_quiet_hours_without_consuming_mention() -> None:
    decision = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), at=aware("2026-08-04T23:00:00-05:00")
    )
    assert decision.disposition is DeliveryDisposition.QUEUE
    assert decision.mention is MentionKind.NONE


def test_live_budget_suppresses_second_everyone_but_not_delivery() -> None:
    first = policy(live_everyone_budget=1).evaluate(live_event("one"), at=NOW)
    second = policy(live_everyone_budget=1, consumed=1).evaluate(live_event("two"), at=LATER)
    assert first.mention is MentionKind.EVERYONE
    assert second.disposition is DeliveryDisposition.DELIVER
    assert second.mention is MentionKind.NONE


def test_ambiguous_crosspost_is_not_merged() -> None:
    assert correlator.correlate(youtube_video(), x_post_without_shared_link()).merge is False


def test_template_allows_bounded_fields_but_cannot_create_mentions() -> None:
    template = validate_template(
        NotificationTemplate(
            headline="{creator} posted on {platform}",
            footer="Fetched by Krubit",
            accent_color=0x8A2BE2,
        )
    )
    assert template.headline == "{creator} posted on {platform}"
    with pytest.raises(ValueError, match="mentions are controlled by notification policy"):
        validate_template(replace(template, headline="@everyone {title}"))
```

- [ ] **Step 2: Run focused tests and confirm missing policy types**

Run: `.venv\Scripts\python.exe -m pytest tests\test_notification_policy.py tests\test_content_correlation.py -q`

Expected: FAIL because policy/correlation modules are absent.

- [ ] **Step 3: Implement deterministic evaluation and receipted budgets**

Use `zoneinfo.ZoneInfo`, half-open quiet intervals, separate live/social budgets, one
approved role mention for social routes, and atomic budget consumption. Exact canonical
IDs/URLs merge immediately. Probabilistic merging requires the same creator, a bounded
time window, and either an identical normalized outbound URL or matching media fingerprint;
title similarity alone never merges. Templates permit only `{creator}`, `{platform}`,
`{title}`, `{content_type}`, and `{url}` placeholders; enforce bounded headline/footer
lengths, a 24-bit accent color, and no Discord mention syntax. The delivery policy—not
template text—supplies every allowed mention.

- [ ] **Step 4: Run policy, DST, concurrency, and correlation tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_notification_policy.py tests\test_content_correlation.py tests\test_content_signal_service.py -q`

Expected: PASS for spring/fall DST boundaries, concurrent mention consumption, queue
release, exact duplicates, strong simulcasts, and ambiguous content.

- [ ] **Step 5: Commit notification policy**

```powershell
git add src/krubit/services/notification_policy.py src/krubit/services/content_signals.py src/krubit/storage/sqlite.py tests/test_notification_policy.py tests/test_content_correlation.py
git commit -m "feat: add creator notification policy"
```

### Task 6: Platform-neutral Discord cards and durable delivery

**Files:**
- Create: `src/krubit/discord/content_cards.py`
- Create: `src/krubit/discord/content_runtime.py`
- Modify: `src/krubit/discord/live_signals.py`
- Test: `tests/test_content_cards.py`
- Test: `tests/test_content_runtime.py`

**Interfaces:**
- Consumes: `ContentPlan`, `DeliveryDecision`, correlation groups, stored route IDs.
- Produces: `build_live_card(group, mention)`, `build_social_card(group, mention)`, `ContentRuntime.apply_plan(guild, plan)`, `recover_pending(guild)`, `retry_delivery(guild, delivery_id)`, and `retract_delivery(guild, delivery_id)`.

- [ ] **Step 1: Write failing card and delivery tests**

```python
def test_live_card_uses_approved_copy_preview_and_multiple_watch_buttons() -> None:
    rendered = build_live_card(live_group(), mention=MentionKind.EVERYONE)
    assert "@everyone" in rendered.content
    assert rendered.embed.image.url.endswith("640x360.jpg")
    assert [button.label for button in rendered.buttons] == ["Watch on Twitch", "Watch on YouTube"]


@pytest.mark.asyncio
async def test_recovery_edits_matching_receipted_message_instead_of_resending(runtime) -> None:
    await runtime.apply_plan(live_plan())
    await runtime.apply_plan(replace(live_plan(), state=ContentState.ENDED))
    assert len(runtime.channel.sent) == 1
    assert len(runtime.channel.messages[1001].edits) == 1
```

- [ ] **Step 2: Run focused tests and confirm absent render/runtime modules**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_cards.py tests\test_content_runtime.py -q`

Expected: FAIL during imports.

- [ ] **Step 3: Implement cards and exact-ID durable actions**

Render bounded excerpts, HTTPS preview URLs, explicit `AllowedMentions`, platform buttons,
and reduced cards when media is unsafe. Delivery uses a Discord-compliant deterministic
nonce, stored channel/message IDs, bounded history recovery, retry attempts, corrections,
and staff-authorized retractions. It never resolves channels by mutable names after
bootstrap.

- [ ] **Step 4: Run content and existing Twitch Discord suites**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_cards.py tests\test_content_runtime.py tests\test_live_signal_discord.py tests\test_live_signal_runtime.py -q`

Expected: PASS; existing approved Twitch rendering assertions remain unchanged.

- [ ] **Step 5: Commit Discord delivery**

```powershell
git add src/krubit/discord/content_cards.py src/krubit/discord/content_runtime.py src/krubit/discord/live_signals.py tests/test_content_cards.py tests/test_content_runtime.py
git commit -m "feat: deliver unified creator notifications"
```

### Task 7: YouTube connector and Discord presence expansion

**Files:**
- Create: `src/krubit/integrations/youtube.py`
- Modify: `src/krubit/web/callbacks.py`
- Modify: `src/krubit/discord/live_runtime.py`
- Modify: `src/krubit/discord/content_runtime.py`
- Test: `tests/test_youtube_client.py`
- Test: `tests/test_youtube_push.py`
- Test: `tests/test_youtube_presence.py`

**Interfaces:**
- Consumes: connector protocol, creator accounts, and content ingestion.
- Produces: `YouTubeConnector`, `parse_youtube_push(atom: bytes)`, and generalized `extract_streaming_observation(member, observed_at)` for Twitch/YouTube.

- [ ] **Step 1: Write failing official-response and presence tests**

```python
def test_youtube_presence_accepts_canonical_watch_and_live_urls() -> None:
    assert extract_streaming_observation(member_with_stream("https://youtube.com/watch?v=abc"))
    assert extract_streaming_observation(member_with_stream("https://youtube.com/live/abc"))


def test_push_entry_becomes_video_identity_but_requires_api_enrichment() -> None:
    event = parse_youtube_push(YOUTUBE_ATOM_FIXTURE)
    assert event.video_id == "video-123"
    assert event.channel_id == "channel-456"
```

- [ ] **Step 2: Run focused tests and confirm Twitch-only/missing connector failures**

Run: `.venv\Scripts\python.exe -m pytest tests\test_youtube_client.py tests\test_youtube_push.py tests\test_youtube_presence.py -q`

Expected: FAIL because YouTube interfaces are absent and presence extraction rejects YouTube.

- [ ] **Step 3: Implement quota-conscious YouTube reads and lifecycle mapping**

Use `channels.list` to resolve the uploads playlist, `playlistItems.list` for low-cost
upload polling, and `videos.list` for content/live metadata. Parse push Atom entries as
triggers and enrich them through the API. Map `upcoming`, `live`, `completed`, deleted,
and unavailable content to shared lifecycle states. Verify push subscription challenges
and shared callback secret before ingestion.

- [ ] **Step 4: Run YouTube and unified ingestion tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_youtube_client.py tests\test_youtube_push.py tests\test_youtube_presence.py tests\test_content_signal_service.py -q`

Expected: PASS for uploads, Shorts, scheduled/live/end transitions, quota failures,
timeouts, malformed payloads, push duplicates, and Discord presence.

- [ ] **Step 5: Commit YouTube support**

```powershell
git add src/krubit/integrations/youtube.py src/krubit/web/callbacks.py src/krubit/discord/live_runtime.py src/krubit/discord/content_runtime.py tests/test_youtube_client.py tests/test_youtube_push.py tests/test_youtube_presence.py
git commit -m "feat: monitor YouTube creator content"
```

### Task 8: X and Bluesky connectors

**Files:**
- Create: `src/krubit/integrations/x.py`
- Create: `src/krubit/integrations/bluesky.py`
- Test: `tests/test_x_connector.py`
- Test: `tests/test_bluesky_connector.py`

**Interfaces:**
- Consumes: connector protocol and normalized events.
- Produces: `XConnector` and `BlueskyConnector`.

- [ ] **Step 1: Write failing fixture tests for original-post filtering and cursors**

```python
@pytest.mark.asyncio
async def test_x_connector_uses_since_id_and_ignores_replies_and_reposts() -> None:
    page = await x_connector(X_TIMELINE_FIXTURE).fetch_page(x_account(), cursor="100")
    assert [event.external_id for event in page.items] == ["103"]
    assert page.next_cursor == "103"


@pytest.mark.asyncio
async def test_bluesky_connector_ignores_reposts_and_replies_by_default() -> None:
    page = await bluesky_connector(BLUESKY_AUTHOR_FEED).fetch_page(bsky_account(), cursor=None)
    assert [event.external_id for event in page.items] == ["at://did/post/original"]
```

- [ ] **Step 2: Run tests and confirm missing adapters**

Run: `.venv\Scripts\python.exe -m pytest tests\test_x_connector.py tests\test_bluesky_connector.py -q`

Expected: FAIL during imports.

- [ ] **Step 3: Implement X bearer-token and Bluesky public-read adapters**

X resolves usernames to stable user IDs, polls `/2/users/{id}/tweets` with `since_id`,
requests only required fields, and maps authorization/rate limits safely. Bluesky resolves
handle to DID and reads `app.bsky.feed.getAuthorFeed` from `public.api.bsky.app`, storing
the newest record identity as the durable watermark.

- [ ] **Step 4: Run connector tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_x_connector.py tests\test_bluesky_connector.py tests\test_connector_base.py -q`

Expected: PASS for pagination, filtering, deletions, quotas, outages, and secret redaction.

- [ ] **Step 5: Commit X and Bluesky support**

```powershell
git add src/krubit/integrations/x.py src/krubit/integrations/bluesky.py tests/test_x_connector.py tests/test_bluesky_connector.py
git commit -m "feat: monitor X and Bluesky posts"
```

### Task 9: Meta connectors for Instagram, Facebook, and Threads

**Files:**
- Create: `src/krubit/integrations/meta.py`
- Modify: `src/krubit/services/creator_registry.py`
- Modify: `src/krubit/security/credential_vault.py`
- Modify: `src/krubit/web/callbacks.py`
- Test: `tests/test_meta_connectors.py`
- Test: `tests/test_meta_authorization.py`

**Interfaces:**
- Consumes: OAuth-safe authorization references, connector protocol, and normalized events.
- Produces: `InstagramConnector`, `FacebookPageConnector`, `FacebookProfileConnector`, `ThreadsConnector`, and Meta OAuth state validation.

- [ ] **Step 1: Write failing fixture and authorization tests**

```python
def test_meta_oauth_state_is_guild_member_platform_bound_and_single_use() -> None:
    state = oauth_states.issue(guild_id=111, member_id=222, platform=Platform.INSTAGRAM)
    assert oauth_states.consume(state, guild_id=111, member_id=222) is Platform.INSTAGRAM
    with pytest.raises(ValueError, match="used or expired"):
        oauth_states.consume(state, guild_id=111, member_id=222)


@pytest.mark.asyncio
async def test_instagram_separates_reel_post_and_active_live_media() -> None:
    page = await instagram_connector(INSTAGRAM_MEDIA_FIXTURE).fetch_page(ig_account(), cursor=None)
    assert [item.kind for item in page.items] == [
        ContentKind.REEL,
        ContentKind.POST,
        ContentKind.LIVE,
    ]
```

- [ ] **Step 2: Run focused tests and confirm missing Meta interfaces**

Run: `.venv\Scripts\python.exe -m pytest tests\test_meta_connectors.py tests\test_meta_authorization.py -q`

Expected: FAIL during imports.

- [ ] **Step 3: Implement authorization-aware capability adapters**

Use signed, expiring, single-use OAuth state tied to guild/member/platform. Encrypt or
externalize refresh/user tokens behind opaque references. Implement only official owner/
Page reads granted by the token. Distinguish Instagram posts/Reels/live media, Facebook
Page posts/videos/Reels/live broadcasts, and original Threads posts. Facebook personal
profiles return `approval_required` unless the grant and app review permit the required
owner-content read.

- [ ] **Step 4: Run Meta, registry, redaction, and isolation tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_meta_connectors.py tests\test_meta_authorization.py tests\test_creator_registry_service.py tests\test_redaction.py -q`

Expected: PASS for token expiry, revocation, insufficient scopes, Page/profile separation,
live/social classification, webhook signature rejection, and guild-bound OAuth state.

- [ ] **Step 5: Commit Meta connectors**

```powershell
git add src/krubit/integrations/meta.py src/krubit/services/creator_registry.py src/krubit/security/credential_vault.py src/krubit/web/callbacks.py tests/test_meta_connectors.py tests/test_meta_authorization.py
git commit -m "feat: monitor authorized Meta creator content"
```

### Task 10: TikTok and Fanbase capability adapters

**Files:**
- Create: `src/krubit/integrations/tiktok.py`
- Create: `src/krubit/integrations/fanbase.py`
- Modify: `src/krubit/services/creator_registry.py`
- Modify: `src/krubit/web/callbacks.py`
- Test: `tests/test_tiktok_connector.py`
- Test: `tests/test_fanbase_connector.py`

**Interfaces:**
- Consumes: connector protocol and capability states.
- Produces: `TikTokConnector` for authorized videos and `FanbaseConnector` for explicit unsupported status.

- [ ] **Step 1: Write failing capability and filtering tests**

```python
@pytest.mark.asyncio
async def test_tiktok_reads_authorized_videos_but_does_not_claim_live_detection() -> None:
    connector = tiktok_connector(TIKTOK_VIDEO_LIST)
    page = await connector.fetch_page(tiktok_account(), cursor=None)
    assert [item.kind for item in page.items] == [ContentKind.VIDEO]
    assert connector.descriptor.capability(Capability.LIVE).state is CapabilityState.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_fanbase_is_recognized_but_never_polled() -> None:
    connector = FanbaseConnector()
    assert (await connector.health()).state is CapabilityState.UNSUPPORTED
    with pytest.raises(UnsupportedConnectorError):
        await connector.fetch_page(fanbase_account(), cursor=None)
```

- [ ] **Step 2: Run tests and confirm missing adapters**

Run: `.venv\Scripts\python.exe -m pytest tests\test_tiktok_connector.py tests\test_fanbase_connector.py -q`

Expected: FAIL during imports.

- [ ] **Step 3: Implement authorized TikTok Display reads and dormant Fanbase behavior**

Use TikTok Login Kit tokens and `/v2/video/list/` for the authorized creator's recent
public videos. Do not infer LIVE state from profile HTML or the LIVE embed. Fanbase accepts
recognized account metadata but performs no network request and always reports the safe
unsupported reason.

- [ ] **Step 4: Run TikTok/Fanbase and catalog tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_tiktok_connector.py tests\test_fanbase_connector.py tests\test_connector_catalog.py -q`

Expected: PASS with no unofficial network paths.

- [ ] **Step 5: Commit TikTok and Fanbase adapters**

```powershell
git add src/krubit/integrations/tiktok.py src/krubit/integrations/fanbase.py src/krubit/services/creator_registry.py src/krubit/web/callbacks.py tests/test_tiktok_connector.py tests/test_fanbase_connector.py
git commit -m "feat: add TikTok and Fanbase capabilities"
```

### Task 11: Discord Scheduled Event synchronization

**Files:**
- Create: `src/krubit/discord/scheduled_events.py`
- Modify: `src/krubit/storage/sqlite.py`
- Modify: `src/krubit/discord/install.py`
- Test: `tests/test_scheduled_event_sync.py`
- Test: `tests/test_discord_install.py`
- Test: `tests/test_discord_inventory.py`

**Interfaces:**
- Consumes: scheduled/live content events and stored Discord configuration.
- Produces: `ScheduledEventSynchronizer.apply(event)`, owned event mappings, and permission requirements.

- [ ] **Step 1: Write failing ownership and lifecycle tests**

```python
@pytest.mark.asyncio
async def test_sync_updates_exact_owned_event_and_never_name_matches() -> None:
    created = await sync.apply(scheduled_youtube_event(external_id="yt-1"))
    delayed = await sync.apply(delayed_youtube_event(external_id="yt-1"))
    assert created.discord_event_id == delayed.discord_event_id
    assert guild.created_events == 1


@pytest.mark.asyncio
async def test_sync_refuses_mapping_without_krubit_ownership_receipt() -> None:
    await store.save_scheduled_event_mapping(mapping(owned_by_krubit=False))
    assert await sync.apply(cancelled_event()) is ScheduledEventOutcome.SKIPPED_NOT_OWNED
```

- [ ] **Step 2: Run focused tests and confirm missing synchronizer**

Run: `.venv\Scripts\python.exe -m pytest tests\test_scheduled_event_sync.py tests\test_discord_install.py -q`

Expected: FAIL because sync code/schema are absent.

- [ ] **Step 3: Implement exact-ID external event lifecycle**

Persist `guild_id`, account/content IDs, Discord event ID, ownership, and last applied
revision. Create external events with URL location, bounded description, explicit start/end,
and safe image. Reconcile scheduled → active → completed or scheduled → cancelled using
only valid Discord transitions. Request `create_events` and `manage_events` only when the
feature is enabled.

- [ ] **Step 4: Run Scheduled Event, inventory, and permission tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_scheduled_event_sync.py tests\test_discord_install.py tests\test_discord_inventory.py -q`

Expected: PASS for restart recovery, permission loss, cancellation, invalid transition,
and non-owned event safety.

- [ ] **Step 5: Commit Scheduled Event synchronization**

```powershell
git add src/krubit/discord/scheduled_events.py src/krubit/storage/sqlite.py src/krubit/discord/install.py tests/test_scheduled_event_sync.py tests/test_discord_install.py tests/test_discord_inventory.py
git commit -m "feat: sync creator scheduled events"
```

### Task 12: Unified commands, health, and analytics

**Files:**
- Create: `src/krubit/discord/content_commands.py`
- Create: `src/krubit/services/creator_analytics.py`
- Modify: `src/krubit/services/health.py`
- Modify: `src/krubit/discord/inventory.py`
- Modify: `src/krubit/discord/bot.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_content_commands.py`
- Test: `tests/test_creator_health.py`
- Test: `tests/test_creator_analytics.py`

**Interfaces:**
- Consumes: registry, content ledger, connector health, policy decisions, delivery runtime, and Scheduled Event mappings.
- Produces: `/fetch creator`, `/fetch latest`, `/fetch live`, `/fetch schedule`, `/fetch notifications`, notification preview/retry/retract, and safe integration/analytics renderers.

- [ ] **Step 1: Write failing command authorization and safe-output tests**

```python
@pytest.mark.asyncio
async def test_creator_role_can_add_self_but_not_another_member(commands) -> None:
    own = await commands.creator_add(actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL)
    denied = await commands.creator_add(actor=creator_member(), owner=other_member(), url=YOUTUBE_URL)
    assert own.status is CommandStatus.CONFIRMATION_REQUIRED
    assert denied.status is CommandStatus.DENIED


def test_integration_status_exposes_state_not_token_or_raw_api_body() -> None:
    card = render_connector_health(health_with_secret_in_internal_error())
    assert "authorization_required" in card.description
    assert "secret-token" not in card.description
```

- [ ] **Step 2: Run focused tests and confirm missing command surface**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_commands.py tests\test_creator_health.py tests\test_creator_analytics.py -q`

Expected: FAIL because unified commands and analytics queries are absent.

- [ ] **Step 3: Implement grouped `/fetch` commands and factual analytics**

Use autocomplete/platform choices only for staff filters; enrollment still accepts one
URL. Bootstrap `#social-notifications` and the configured Creator role by exact name once,
then persist and use their IDs; missing/ambiguous resources produce health failures rather
than implicit creation. Mutating commands require private confirmation views. Preview performs no send,
mention, role, or Scheduled Event action. Retry validates current route/policy and attempt
ownership. Retract edits/deletes only a stored Krubit-authored message. Analytics expose
counts, latency, state, quota history, suppression reasons, and own-profile facts without
sentiment/ranking.

- [ ] **Step 4: Run command, bot, health, and existing `/fetch live` tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_commands.py tests\test_creator_health.py tests\test_creator_analytics.py tests\test_live_signal_commands.py tests\test_health_service.py tests\test_cli.py -q`

Expected: PASS with no command-name collisions and no public confirmation output.

- [ ] **Step 5: Commit commands and analytics**

```powershell
git add src/krubit/discord/content_commands.py src/krubit/services/creator_analytics.py src/krubit/services/health.py src/krubit/discord/inventory.py src/krubit/discord/bot.py src/krubit/storage/sqlite.py tests/test_content_commands.py tests/test_creator_health.py tests/test_creator_analytics.py
git commit -m "feat: add creator notification controls"
```

### Task 13: Unified runtime, Twitch migration, and connector scheduling

**Files:**
- Modify: `src/krubit/discord/content_runtime.py`
- Modify: `src/krubit/discord/live_runtime.py`
- Modify: `src/krubit/services/live_signals.py`
- Modify: `src/krubit/discord/bot.py`
- Modify: `src/krubit/__main__.py`
- Test: `tests/test_content_scheduler.py`
- Test: `tests/test_twitch_content_migration.py`
- Test: `tests/test_content_recovery.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: one runtime supervisor for connector polling, queued release, recovery, and Twitch/YouTube presence dispatch.

- [ ] **Step 1: Write failing migration, recovery, and isolation tests**

```python
@pytest.mark.asyncio
async def test_existing_twitch_delivery_is_linked_without_reannouncement(store) -> None:
    await seed_phase_2a_live_session(store, message_id=1001, stream_id="stream-1")
    await migrate_twitch_content(store, guild_id=111)
    delivery = await store.get_content_delivery(111, Platform.TWITCH, "stream-1")
    assert delivery.discord_message_id == 1001
    assert await store.list_pending_content_deliveries(111) == []


@pytest.mark.asyncio
async def test_one_connector_failure_does_not_cancel_other_guild_or_platform_jobs(supervisor) -> None:
    await supervisor.run_cycle()
    assert supervisor.result(111, Platform.X).state is CapabilityState.DEGRADED
    assert supervisor.result(222, Platform.BLUESKY).state is CapabilityState.READY
```

- [ ] **Step 2: Run focused tests and confirm absent supervisor/migration**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_scheduler.py tests\test_twitch_content_migration.py tests\test_content_recovery.py -q`

Expected: FAIL for missing supervisor and migration path.

- [ ] **Step 3: Implement per-account scheduling and idempotent Twitch migration**

Use bounded connector-specific semaphores, durable next-poll times, backoff/jitter, and
separate exception boundaries. On startup, migrate Phase 2A sessions/deliveries into
shared identities transactionally while retaining old tables as rollback evidence. Route
Twitch and YouTube presence into unified observations, then keep Twitch role application
through the proven exact-owned-role adapter. Default healthy fallback polling intervals
are YouTube 5 minutes, X 15 minutes, Bluesky 2 minutes, Meta 5 minutes, and TikTok 5
minutes; connector rate-limit headers and retry times override those defaults without
creating a faster loop. Fanbase never receives a scheduled network job.

- [ ] **Step 4: Run runtime, migration, and all Phase 2A regression suites**

Run: `.venv\Scripts\python.exe -m pytest tests\test_content_scheduler.py tests\test_twitch_content_migration.py tests\test_content_recovery.py tests\test_live_signal_runtime.py tests\test_live_signal_service.py tests\test_live_signal_storage.py -q`

Expected: PASS for multiple guilds, multiple platforms, restarts, queued release, failed
connectors, existing announcement reuse, and role cleanup.

- [ ] **Step 5: Commit unified runtime**

```powershell
git add src/krubit/discord/content_runtime.py src/krubit/discord/live_runtime.py src/krubit/services/live_signals.py src/krubit/discord/bot.py src/krubit/__main__.py tests/test_content_scheduler.py tests/test_twitch_content_migration.py tests/test_content_recovery.py
git commit -m "feat: run unified creator signal hub"
```

### Task 14: Operations documentation, rollout controls, and completion audit

**Files:**
- Modify: `README.md`
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`
- Create: `docs/operations/phase-2-creator-signal-hub.md`
- Create: `docs/devlogs/2026-08-04-phase-2-completion.md`
- Create: `docs/operations/phase-2-completion-audit.md`
- Modify: `.env.example`
- Test: `tests/test_phase_2_rollout.py`

**Interfaces:**
- Consumes: completed software and verification evidence.
- Produces: operator setup/runbook, credential checklist, shadow/preview/canary controls, and requirement-by-requirement completion evidence.

- [ ] **Step 1: Write failing rollout contract tests**

```python
def test_new_connectors_default_disabled_and_can_be_enabled_independently() -> None:
    settings = Settings.from_env(base_env())
    assert settings.creator_signals_enabled is False
    assert settings.social_delivery_enabled is False


def test_every_catalog_capability_appears_in_health_even_when_unconfigured() -> None:
    facts = build_creator_health(empty_credentials())
    assert {(fact.platform, fact.capability) for fact in facts} == expected_catalog_capabilities()
```

- [ ] **Step 2: Run rollout tests and identify any missing health/config facts**

Run: `.venv\Scripts\python.exe -m pytest tests\test_phase_2_rollout.py -q`

Expected: FAIL until the final rollout inventory is complete.

- [ ] **Step 3: Write the operator guide, migration procedure, and audit matrix**

Document exact environment variable names, platform developer setup, OAuth callback
requirements, Discord channel/role/event permissions, URL enrollment, shadow/preview/
enable commands, quota/expiry remediation, rollback, data deletion, and connector-specific
limitations. The audit matrix maps every design completion-gate line to a test, command,
database query, or controlled canary receipt.

- [ ] **Step 4: Run full automated verification**

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\ruff.exe check .`

Run: `.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe`

Run: `git diff --check`

Expected: all tests pass; Ruff and Pyright report zero findings; diff check exits 0.

- [ ] **Step 5: Run credential-independent acceptance checks**

Run: `scripts\invoke-krubit.ps1 doctor`

Run in the configured Discord guild while delivery flags remain false:

```text
/fetch integrations
/fetch creator add <approved test URL>
/fetch notification preview
/fetch live
/fetch latest
/fetch schedule
```

Expected: capability states are accurate; enrollment/preview are ephemeral; no role,
public card, Scheduled Event, or mention occurs.

- [ ] **Step 6: Run authorized connector canaries as credentials become available**

For each credentialed connector, enable only its shadow flag, baseline an approved test
account, publish or schedule one controlled item, verify one normalized event and zero
public deliveries, then authorize preview and finally one production delivery. Record
external content ID, detection time, route ID, Discord message/event ID, mention decision,
delivery receipt, lifecycle edit, and cleanup result without recording secrets or full
private payloads.

- [ ] **Step 7: Commit documentation and audit evidence**

```powershell
git add README.md .env.example docs/roadmaps/2026-08-03-krubit-phase-rollout.md docs/operations/phase-2-creator-signal-hub.md docs/devlogs/2026-08-04-phase-2-completion.md docs/operations/phase-2-completion-audit.md tests/test_phase_2_rollout.py
git commit -m "docs: close Phase 2 creator signal hub"
```

## Final Integration Gate

- [ ] Re-read `docs/superpowers/specs/2026-08-04-phase-2-completion-design.md` line by line and update the completion audit with direct evidence for every requirement.
- [ ] Confirm `git status --short` contains no unintended or secret-bearing files.
- [ ] Confirm the master `.env` contains only credential slots/values and was never staged.
- [ ] Confirm exactly one Krubit process tree is running from the reviewed build.
- [ ] Use `superpowers:verification-before-completion` before any completion claim.
- [ ] Use `superpowers:finishing-a-development-branch` and let the user choose local merge, PR, or branch preservation.
- [ ] After the user's integration choice, devlog, commit, and push only when explicitly requested.
