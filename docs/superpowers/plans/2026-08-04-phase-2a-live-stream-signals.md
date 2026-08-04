# Phase 2A Live Stream Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect Discord members exposing a Twitch Streaming presence, post one approved Krubit creature-signal announcement, and safely maintain their `Streaming Now` role.

**Architecture:** Discord presence is the discovery trigger, a framework-independent live-signal service owns state and idempotency, Twitch Helix enriches and reconciles active streams, and narrow Discord adapters perform cards, mentions, and role mutations. Guild-scoped SQLite records survive reconnects and restarts; Twitch or Discord failures become receipts and degraded state instead of duplicate public actions.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiohttp 3.14.3, aiosqlite 0.22.1, pytest 9, pytest-asyncio 1.4.0, Ruff, Pyright strict, PowerShell launcher.

## Global Constraints

- Discord streaming presence is the only automatic discovery path in Phase 2A; do not inspect private Discord Connections or add creator registration.
- Accept only Discord activity type `Streaming` with a normalized `twitch.tv` or `www.twitch.tv` channel URL; defer YouTube and every other platform.
- Post in the configured `#live-notifications` channel and assign only the configured `Streaming Now` role.
- The public content format is `⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone {twitch_display_name} ⌇⊑⏃ ⌰⟟⎐⏃!`.
- Permit only the intended `@everyone` mention; external creator text must never create user or role mentions.
- Wait at most five seconds for initial Twitch enrichment, poll active sessions every 60 seconds, and use a five-minute missing-evidence recovery window.
- Never remove a pre-existing `Streaming Now` role unless a durable receipt proves Krubit assigned it for an active session.
- Preserve all other Discord roles without modification.
- Store no Twitch password, stream key, creator OAuth token, Discord connection list, unrelated presence activity, or message content.
- Keep the Twitch Client Secret only in environment memory; redact it from logs, errors, receipts, and SQLite.
- Every database API and durable identity remains guild-scoped.
- `/fetch live test` is ephemeral and cannot assign a role or create a real mass mention.
- Phase 2A excludes YouTube feeds, scheduled streams, new videos, quiet hours, analytics, and cross-platform campaign deduplication.

---

## File Structure

- `src/krubit/domain/live_signals.py`: immutable live-signal models, URL normalization, identities, states, and action plans.
- `src/krubit/integrations/twitch.py`: Twitch app-token lifecycle and typed Helix stream lookups.
- `src/krubit/services/live_signals.py`: state transitions, enrichment budget, reconciliation, and durable action outcomes.
- `src/krubit/discord/live_signals.py`: Discord presence extraction and approved safe card/message rendering.
- `src/krubit/discord/live_runtime.py`: guild resource resolution and execution of role/message plans.
- `src/krubit/discord/live_commands.py`: staff-only `/fetch live` commands.
- `src/krubit/storage/sqlite.py`: Phase 2A schema and guild-scoped persistence methods, following the existing single-store pattern.
- `src/krubit/config.py`, `src/krubit/discord/install.py`, `src/krubit/discord/bot.py`, `src/krubit/__main__.py`: runtime wiring and lifecycle.
- `src/krubit/discord/inventory.py`, `src/krubit/services/health.py`: Phase 2A permission and integration-health facts.
- `scripts/invoke-krubit.ps1`, `.env.example`, `pyproject.toml`, `uv.lock`: secure launch and dependency configuration.
- `docs/operations/phase-2a-live-stream-signals.md`, `README.md`, `docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md`: operator and project documentation.

---

### Task 1: Phase 2A Configuration and Discord Access Surface

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_discord_install.py`
- Create: `tests/test_launcher_config.py`
- Modify: `src/krubit/config.py`
- Modify: `src/krubit/discord/install.py`
- Modify: `scripts/invoke-krubit.ps1`
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `Settings.twitch_client_id`, `Settings.twitch_client_secret`, `Settings.live_signals_enabled`, and `Settings.require_twitch_credentials() -> tuple[str, str]`.
- Produces: `phase_two_intents() -> discord.Intents` and `phase_two_permissions() -> discord.Permissions`.
- Consumed by: Tasks 4, 7, 8, and 10.

- [ ] **Step 1: Write failing settings, launcher, intent, and permission tests**

```python
def test_phase_two_settings_parse_twitch_and_default_disabled() -> None:
    settings = Settings.from_env({
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "TWITCH_KRUBIT_CLIENT_ID": "client-id",
        "TWITCH_KRUBIT_CLIENT_SECRET": "client-secret",
    })
    assert settings.require_twitch_credentials() == ("client-id", "client-secret")
    assert settings.live_signals_enabled is False


def test_phase_two_enables_presence_and_required_mutations() -> None:
    intents = phase_two_intents()
    permissions = phase_two_permissions()
    assert intents.guilds and intents.members and intents.presences
    assert permissions.manage_roles
    assert permissions.mention_everyone
    assert permissions.administrator is False
```

In `tests/test_launcher_config.py`, read `scripts/invoke-krubit.ps1` and assert all three new names appear in its allowed-name block. Keep them out of the launcher's unconditional required-name block because administrative CLI commands do not need Twitch; `Settings.require_twitch_credentials()` performs the conditional runtime check when live signals are enabled:

```python
def test_launcher_allows_phase_two_environment_names() -> None:
    text = Path("scripts/invoke-krubit.ps1").read_text(encoding="utf-8")
    for name in (
        "TWITCH_KRUBIT_CLIENT_ID",
        "TWITCH_KRUBIT_CLIENT_SECRET",
        "KRUBIT_LIVE_SIGNALS_ENABLED",
    ):
        assert f'"{name}"' in text
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest tests/test_config.py tests/test_discord_install.py tests/test_launcher_config.py -q`

Expected: FAIL because Phase 2 settings and access helpers do not exist.

- [ ] **Step 3: Implement minimal configuration and access helpers**

Add optional credentials and an opt-in Boolean to `Settings`; reject any Boolean other than `true`, `false`, `1`, or `0`:

```python
@dataclass(frozen=True, slots=True)
class Settings:
    application_id: int
    database_path: Path
    bot_token: str | None = None
    staff_channel_id: int | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    live_signals_enabled: bool = False

    def require_twitch_credentials(self) -> tuple[str, str]:
        if self.twitch_client_id is None or self.twitch_client_secret is None:
            raise SettingsError(
                "TWITCH_KRUBIT_CLIENT_ID and TWITCH_KRUBIT_CLIENT_SECRET are required "
                "when KRUBIT_LIVE_SIGNALS_ENABLED=true"
            )
        return self.twitch_client_id, self.twitch_client_secret
```

Create `phase_two_intents()` from `phase_one_intents()`, add `presences = True`, create `phase_two_permissions()` from Phase 1, and add only `manage_roles` and `mention_everyone`. Make `install_url()` use Phase 2 permissions.

Update the launcher allowlist with all three environment names. Keep them optional at the PowerShell layer so administrative CLI commands still work; `krubit run` enforces them when live signals are enabled. Add blank credential examples plus `KRUBIT_LIVE_SIGNALS_ENABLED=false` to `.env.example`.

Run `uv add aiohttp==3.14.3` so direct Twitch HTTP usage is declared and `uv.lock` stays synchronized.

- [ ] **Step 4: Run tests and static checks**

Run: `uv run pytest tests/test_config.py tests/test_discord_install.py tests/test_launcher_config.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/config.py src/krubit/discord/install.py tests/test_config.py tests/test_discord_install.py tests/test_launcher_config.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add .env.example pyproject.toml uv.lock scripts/invoke-krubit.ps1 src/krubit/config.py src/krubit/discord/install.py tests/test_config.py tests/test_discord_install.py tests/test_launcher_config.py
git commit -m "feat: configure Phase 2A live signal access"
```

---

### Task 2: Live-Signal Domain Models and Twitch URL Normalization

**Files:**
- Create: `src/krubit/domain/live_signals.py`
- Create: `tests/test_live_signal_domain.py`

**Interfaces:**
- Produces: `StreamingObservation`, `TwitchStream`, `TwitchLookup`, `LiveSignalSession`, `LiveSignalConfig`, `LiveSignalAction`, `LiveSignalPlan`, `normalize_twitch_channel()`, and `provisional_session_key()`.
- Consumed by: Tasks 3 through 8.

- [ ] **Step 1: Write failing pure-domain tests**

```python
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://twitch.tv/KrucialStudios", "krucialstudios"),
        ("https://www.twitch.tv/krucialstudios/", "krucialstudios"),
        ("https://youtube.com/live/example", None),
        ("https://evil.example/twitch.tv/krucialstudios", None),
        ("https://twitch.tv/directory", None),
    ],
)
def test_normalize_twitch_channel(url: str, expected: str | None) -> None:
    assert normalize_twitch_channel(url) == expected


def test_provisional_identity_is_stable_for_replayed_presence() -> None:
    observation = StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=datetime(2026, 8, 4, 20, 12, tzinfo=UTC),
        observed_at=datetime(2026, 8, 4, 20, 14, tzinfo=UTC),
    )
    assert provisional_session_key(observation) == provisional_session_key(observation)
```

Also test positive IDs, timezone-aware timestamps, immutable dataclasses, supported status values, and that external strings are length-bounded when constructing `TwitchStream`.

- [ ] **Step 2: Run the test and verify failure**

Run: `uv run pytest tests/test_live_signal_domain.py -q`

Expected: FAIL with `ModuleNotFoundError: krubit.domain.live_signals`.

- [ ] **Step 3: Implement the pure domain module**

Use `StrEnum` values rather than free-form strings:

```python
class LiveSignalStatus(StrEnum):
    DETECTED = "detected"
    LIVE = "live"
    ENDING = "ending"
    ENDED = "ended"
    FAILED = "failed"


class LiveSignalAction(StrEnum):
    ENSURE_ROLE = "ensure_role"
    ANNOUNCE = "announce"
    EDIT_ANNOUNCEMENT = "edit_announcement"
    REMOVE_ROLE = "remove_role"


class TwitchLookupKind(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"
```

Define all timestamps as aware `datetime` values. `normalize_twitch_channel()` must use `urllib.parse.urlsplit`, require HTTPS, allow only `twitch.tv` and `www.twitch.tv`, require one valid login path segment matching `[a-zA-Z0-9_]{1,25}`, and reject reserved paths such as `directory`, `downloads`, `jobs`, `p`, `settings`, and `videos`.

Hash `guild_id`, `member_id`, normalized login, and `activity_started_at.isoformat()` with SHA-256 for the provisional key. If Discord supplies no activity start time, use the first `observed_at` persisted for that session; service lookup by open `(guild_id, member_id, twitch_login)` prevents later presence updates from generating another key.

- [ ] **Step 4: Run domain tests and static checks**

Run: `uv run pytest tests/test_live_signal_domain.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/domain/live_signals.py tests/test_live_signal_domain.py && uv run pyright src/krubit/domain/live_signals.py tests/test_live_signal_domain.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/domain/live_signals.py tests/test_live_signal_domain.py
git commit -m "feat: model live signal state"
```

---

### Task 3: Guild-Scoped Live-Signal Persistence

**Files:**
- Modify: `src/krubit/storage/sqlite.py`
- Create: `tests/test_live_signal_storage.py`

**Interfaces:**
- Produces: `set_live_signal_config()`, `get_live_signal_config()`, `save_live_session()`, `open_live_session()`, `get_live_session()`, `list_active_live_sessions()`, `claim_live_delivery()`, `complete_live_delivery()`, and `record_live_check()`.
- Consumes: Task 2 domain dataclasses.
- Consumed by: Tasks 5, 7, and 8.

- [ ] **Step 1: Write failing schema and tenant-isolation tests**

```python
@pytest.mark.asyncio
async def test_live_session_and_delivery_are_guild_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session(guild_id=111))
        assert await store.get_live_session(111, "session-1") is not None
        assert await store.get_live_session(222, "session-1") is None
        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is True
        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is False
        assert await store.claim_live_delivery(222, "stream:abc", "session-1") is True
    finally:
        await store.close()
```

Add tests for config renames retaining IDs, active-session ordering, stream-ID merge without duplicate rows, pre-existing-role ownership, failed-delivery retry state, check-detail redaction, and non-destructive migration of an existing Phase 1 database.

- [ ] **Step 2: Run storage tests and verify failure**

Run: `uv run pytest tests/test_live_signal_storage.py tests/test_storage.py -q`

Expected: FAIL because the new methods and tables do not exist; existing storage tests remain green.

- [ ] **Step 3: Add the Phase 2A tables and repository methods**

Add these `CREATE TABLE IF NOT EXISTS` statements:

```sql
CREATE TABLE IF NOT EXISTS live_signal_config (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_signal_sessions (
    guild_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    member_id INTEGER NOT NULL,
    twitch_login TEXT NOT NULL,
    twitch_url TEXT NOT NULL,
    status TEXT NOT NULL,
    presence_started_at TEXT,
    detected_at TEXT NOT NULL,
    stream_id TEXT,
    stream_display_name TEXT,
    stream_title TEXT,
    stream_category TEXT,
    stream_started_at TEXT,
    thumbnail_url TEXT,
    announcement_channel_id INTEGER,
    announcement_message_id INTEGER,
    role_id INTEGER,
    role_assigned_by_krubit INTEGER NOT NULL DEFAULT 0
        CHECK (role_assigned_by_krubit IN (0, 1)),
    presence_active INTEGER NOT NULL DEFAULT 1
        CHECK (presence_active IN (0, 1)),
    missing_since TEXT,
    last_discord_at TEXT NOT NULL,
    last_twitch_at TEXT,
    ended_at TEXT,
    PRIMARY KEY (guild_id, session_key)
);

CREATE TABLE IF NOT EXISTS live_signal_deliveries (
    guild_id INTEGER NOT NULL,
    delivery_key TEXT NOT NULL,
    session_key TEXT NOT NULL,
    status TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, delivery_key)
);

CREATE TABLE IF NOT EXISTS live_signal_checks (
    guild_id INTEGER NOT NULL,
    check_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    result TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, check_id)
);
```

Create an index on `(guild_id, status, detected_at)` and a partial unique index on `(guild_id, stream_id)` where `stream_id IS NOT NULL`. Serialize through explicit row-to-domain helpers; pass check details through existing `redact()` before JSON encoding. Every public method must require `guild_id` and validate it is positive.

- [ ] **Step 4: Run storage tests and checks**

Run: `uv run pytest tests/test_live_signal_storage.py tests/test_storage.py tests/test_companion_storage.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/storage/sqlite.py tests/test_live_signal_storage.py && uv run pyright src/krubit/storage/sqlite.py tests/test_live_signal_storage.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/storage/sqlite.py tests/test_live_signal_storage.py
git commit -m "feat: persist live signal sessions"
```

---

### Task 4: Twitch Helix Client and App-Token Lifecycle

**Files:**
- Create: `src/krubit/integrations/__init__.py`
- Create: `src/krubit/integrations/twitch.py`
- Create: `tests/test_twitch_client.py`

**Interfaces:**
- Produces: `TwitchClient` protocol and `TwitchHelixClient.get_stream(login: str) -> TwitchLookup`.
- Consumes: `TwitchLookup`, `TwitchLookupKind`, and `TwitchStream` from Task 2.
- Consumed by: Tasks 5 and 7.

- [ ] **Step 1: Write failing transport tests with a narrow fake session**

Test these exact cases: token acquisition, cached token reuse, refresh 60 seconds before expiry, hourly `/validate`, one retry after a 401, live response mapping, empty `data` as offline, 429 `Ratelimit-Reset` handling without sleeping inside tests, timeout/unparseable JSON as unavailable, and secret redaction from raised/logged details. Define `FakeResponse` as an async context manager with `status`, `headers`, and `json()`, and define `FakeSession.request()` to record `(method, url, kwargs)` and return scripted responses:

```python
class FakeResponse:
    def __init__(self, status: int, payload: object, headers: dict[str, str] | None = None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_get_stream_maps_live_helix_payload() -> None:
    session = FakeSession([
        FakeResponse(200, {"access_token": "token", "expires_in": 3600}),
        FakeResponse(200, {"data": [{
            "id": "stream-1", "user_login": "krucialstudios",
            "user_name": "Krucial Studios", "title": "Building Krucial Town",
            "game_name": "Just Chatting", "started_at": "2026-08-04T20:12:00Z",
            "thumbnail_url": "https://static-cdn.jtvnw.net/preview-{width}x{height}.jpg",
        }]}),
    ])
    result = await TwitchHelixClient(session, "client", "secret").get_stream(
        "krucialstudios"
    )
    assert result.kind is TwitchLookupKind.LIVE
    assert result.stream is not None and result.stream.stream_id == "stream-1"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_twitch_client.py -q`

Expected: FAIL because the integration module does not exist.

- [ ] **Step 3: Implement the client**

Use only official endpoints and headers:

```python
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

class TwitchClient(Protocol):
    async def get_stream(self, login: str) -> TwitchLookup: ...
```

Keep `_access_token`, `_expires_at`, and `_validated_at` in memory. Send `Client-Id` and `Authorization: Bearer ...` only after acquiring the app token. Convert `{width}` and `{height}` in thumbnails to `640` and `360`. Return typed unavailable codes such as `timeout`, `rate_limited`, `token_rejected`, and `invalid_response`; never include response bodies or credential values in those codes.

- [ ] **Step 4: Run integration tests and checks**

Run: `uv run pytest tests/test_twitch_client.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/integrations tests/test_twitch_client.py && uv run pyright src/krubit/integrations tests/test_twitch_client.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/integrations tests/test_twitch_client.py
git commit -m "feat: query Twitch live streams"
```

---

### Task 5: Idempotent Live-Signal Service and Reconciliation

**Files:**
- Create: `src/krubit/services/live_signals.py`
- Create: `tests/test_live_signal_service.py`

**Interfaces:**
- Produces: `LiveSignalService.observe()`, `presence_ended()`, `reconcile()`, `record_role_result()`, `record_delivery_result()`, `status()`, and `integration_health()`.
- Consumes: Task 2 domain types, Task 3 storage methods, and Task 4 `TwitchClient`.
- Consumed by: Tasks 7 and 8.

- [ ] **Step 1: Write failing state-machine tests**

Cover: first detection creates `ENSURE_ROLE` and `ANNOUNCE`; a replay creates neither; Helix success merges the stream ID; a five-second timeout produces a degraded announcement; recovery produces only `EDIT_ANNOUNCEMENT`; Discord disappearance plus Twitch live keeps the role; Twitch offline produces `REMOVE_ROLE`; both sources unavailable remove only after five minutes; restart with an existing delivery cannot announce again; and a session with `role_assigned_by_krubit=False` never requests removal.

```python
@pytest.mark.asyncio
async def test_replayed_presence_cannot_duplicate_announcement(store: SQLiteStore) -> None:
    service = LiveSignalService(store, FakeTwitch.live(stream_id="stream-1"))
    first = await service.observe(observation(), now=NOW)
    second = await service.observe(observation(), now=NOW + timedelta(seconds=10))
    assert first.actions == (
        LiveSignalAction.ENSURE_ROLE,
        LiveSignalAction.ANNOUNCE,
    )
    assert LiveSignalAction.ANNOUNCE not in second.actions
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_live_signal_service.py -q`

Expected: FAIL because `LiveSignalService` does not exist.

- [ ] **Step 3: Implement transitions and durable outcomes**

Use this public surface:

```python
class LiveSignalService:
    def __init__(self, store: SQLiteStore, twitch: TwitchClient) -> None: ...
    async def observe(self, observation: StreamingObservation, *, now: datetime) -> LiveSignalPlan: ...
    async def presence_ended(
        self, guild_id: int, member_id: int, *, now: datetime
    ) -> LiveSignalPlan | None: ...
    async def reconcile(self, guild_id: int, *, now: datetime) -> tuple[LiveSignalPlan, ...]: ...
    async def record_role_result(
        self, guild_id: int, session_key: str, *, role_id: int,
        assigned_by_krubit: bool, status: str
    ) -> None: ...
    async def record_delivery_result(
        self, guild_id: int, session_key: str, *, status: str,
        channel_id: int, message_id: int | None
    ) -> None: ...
```

Wrap the initial Twitch lookup in `asyncio.timeout(5)`. Store the session before returning public actions. Use the stream ID delivery key when available and the provisional key otherwise; atomically merge the identity in persistence. Do not sleep inside service methods. Reconciliation callers provide `now`, making five-minute behavior deterministic in tests.

- [ ] **Step 4: Run service and persistence tests**

Run: `uv run pytest tests/test_live_signal_service.py tests/test_live_signal_storage.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/services/live_signals.py tests/test_live_signal_service.py && uv run pyright src/krubit/services/live_signals.py tests/test_live_signal_service.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/services/live_signals.py tests/test_live_signal_service.py
git commit -m "feat: reconcile live signal state"
```

---

### Task 6: Discord Presence Adapter and Approved Creature-Signal Card

**Files:**
- Create: `src/krubit/discord/live_signals.py`
- Create: `tests/test_live_signal_discord.py`

**Interfaces:**
- Produces: `extract_twitch_observation()`, `render_live_embed()`, `render_live_content()`, `live_allowed_mentions()`, and `build_live_view()`.
- Consumes: Task 2 domain types.
- Consumed by: Tasks 7 and 8.

- [ ] **Step 1: Write failing adapter and rendering tests**

```python
def test_live_content_uses_alien_language_and_only_everyone_is_allowed() -> None:
    content = render_live_content("Krucial Studios")
    allowed = live_allowed_mentions()
    assert content == (
        "⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone Krucial Studios ⌇⊑⏃ ⌰⟟⎐⏃!"
    )
    assert allowed.everyone is True
    assert allowed.users is False
    assert allowed.roles is False


def test_extract_observation_ignores_non_streaming_and_youtube() -> None:
    assert extract_twitch_observation(member_with_game(), observed_at=NOW) is None
    assert extract_twitch_observation(member_with_youtube_stream(), observed_at=NOW) is None
```

Test bot-user exclusion, valid Twitch extraction, activity start preservation, external `@` and Markdown neutralization, title/field truncation, reduced-card rendering, 640x360 thumbnail, purple color, crystal/live-signal copy, and a link button labeled `Fetch the Stream`.

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_live_signal_discord.py -q`

Expected: FAIL because the Discord live adapter does not exist.

- [ ] **Step 3: Implement extraction and rendering**

Scan `member.activities` for `discord.ActivityType.streaming`, then pass its URL through `normalize_twitch_channel()`. Do not serialize any other activity.

Use escaped, bounded creator text and explicit allowed mentions:

```python
def live_allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=True, users=False, roles=False, replied_user=False
    )


def render_live_content(display_name: str) -> str:
    safe_name = discord.utils.escape_markdown(display_name.replace("@", "＠"))[:100]
    return f"⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone {safe_name} ⌇⊑⏃ ⌰⟟⎐⏃!"
```

Render the approved heading, subtitle, creator/platform, title, Category and Status fields, thumbnail, footer, and URL button. A reduced card uses the normalized Twitch URL and Discord activity name without inventing unavailable Twitch facts.

- [ ] **Step 4: Run adapter tests and checks**

Run: `uv run pytest tests/test_live_signal_discord.py tests/test_discord_cards.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/discord/live_signals.py tests/test_live_signal_discord.py && uv run pyright src/krubit/discord/live_signals.py tests/test_live_signal_discord.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/discord/live_signals.py tests/test_live_signal_discord.py
git commit -m "feat: render Krubit live signal cards"
```

---

### Task 7: Discord Runtime, Safe Role Ownership, and Recovery Loop

**Files:**
- Create: `src/krubit/discord/live_runtime.py`
- Modify: `src/krubit/discord/bot.py`
- Modify: `src/krubit/__main__.py`
- Create: `tests/test_live_signal_runtime.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_daily_summary.py`
- Modify: `tests/test_discord_events.py`

**Interfaces:**
- Produces: `LiveSignalRuntime.configure_guild()`, `handle_presence()`, `apply_plan()`, `reconcile_guild()`, and `reconcile_all()`.
- Consumes: Tasks 3 through 6.
- Consumed by: Task 8 commands and health surfaces.

- [ ] **Step 1: Write failing runtime tests with Discord fakes**

Test exact-name bootstrap of `live-notifications` and `Streaming Now`, persistence by ID across renames, permission/hierarchy failures, immediate add, pre-existing-role ownership, removal of only the dedicated role, exactly one send with explicit allowed mentions, in-place embed edit without content/mention replacement, guild-disabled no-op, and 60-second reconciliation without task leakage on close. Define narrow `FakeRole`, `FakeMember`, `FakeTextChannel`, and `FakeGuild` classes in the test file; each fake records only calls used by `LiveSignalRuntime`. Expose a `runtime` fixture that supplies those fakes plus a real temporary `SQLiteStore` and a scripted live-signal service.

```python
@pytest.mark.asyncio
async def test_apply_plan_preserves_every_other_role() -> None:
    member = FakeMember(roles=[member_role, creator_role])
    runtime = runtime_with(member=member, streaming_role=streaming_role)
    await runtime.apply_plan(guild, ensure_and_announce_plan())
    assert member.added_roles == [streaming_role]
    assert member.removed_roles == []
    await runtime.apply_plan(guild, remove_plan(assigned_by_krubit=True))
    assert member.removed_roles == [streaming_role]
    assert member.roles_without_mutation == [member_role, creator_role]
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_live_signal_runtime.py tests/test_cli.py tests/test_daily_summary.py tests/test_discord_events.py -q`

Expected: FAIL because runtime wiring is absent; existing tests identify constructor compatibility that must be preserved.

- [ ] **Step 3: Implement the runtime and bot wiring**

Use exact resource bootstrap constants:

```python
LIVE_CHANNEL_NAME = "live-notifications"
STREAMING_ROLE_NAME = "Streaming Now"

class LiveSignalRuntime:
    async def configure_guild(self, guild: discord.Guild) -> LiveSignalConfig | None: ...
    async def handle_presence(
        self, before: discord.Member, after: discord.Member
    ) -> None: ...
    async def apply_plan(self, guild: discord.Guild, plan: LiveSignalPlan) -> None: ...
    async def reconcile_guild(self, guild: discord.Guild) -> int: ...
    async def reconcile_all(self, guilds: Iterable[discord.Guild]) -> int: ...
```

When adding the role, check membership first and record `assigned_by_krubit=False` if already present. On removal, require `assigned_by_krubit=True`. Use Discord reasons containing only safe session IDs. Before send, verify channel permissions for view, send, embed, and mention; if mention is missing, send with `AllowedMentions.none()` and receipt the degraded delivery. Message enrichment edits only the embed and view, never resubmits content.

Change `KrubitBot` to use `phase_two_intents()`, delegate `on_presence_update`, bootstrap/reconcile on guild availability, start a `@tasks.loop(seconds=60)` reconciliation loop, and cancel it in `close()`.

Keep `KrubitBot(..., twitch=None)` valid for unit tests and disabled mode. In `_run_bot`, when `live_signals_enabled` is true, create a separate TLS-enabled `aiohttp.ClientSession`, construct `TwitchHelixClient`, and pass it to the bot. If disabled, use a typed unavailable client and perform no public live actions.

- [ ] **Step 4: Run runtime and regression tests**

Run: `uv run pytest tests/test_live_signal_runtime.py tests/test_cli.py tests/test_daily_summary.py tests/test_discord_events.py -q`

Expected: PASS with no unclosed-session or task warnings.

Run: `uv run ruff check src/krubit/discord/live_runtime.py src/krubit/discord/bot.py src/krubit/__main__.py tests/test_live_signal_runtime.py && uv run pyright src/krubit/discord/live_runtime.py src/krubit/discord/bot.py src/krubit/__main__.py tests/test_live_signal_runtime.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/discord/live_runtime.py src/krubit/discord/bot.py src/krubit/__main__.py tests/test_live_signal_runtime.py tests/test_cli.py tests/test_daily_summary.py tests/test_discord_events.py
git commit -m "feat: run automatic Discord live signals"
```

---

### Task 8: `/fetch live` Commands and Phase 2A Health Facts

**Files:**
- Create: `src/krubit/discord/live_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Modify: `src/krubit/discord/inventory.py`
- Modify: `src/krubit/services/health.py`
- Create: `tests/test_live_signal_commands.py`
- Modify: `tests/test_phase_one_commands.py`
- Modify: `tests/test_discord_inventory.py`
- Modify: `tests/test_health_service.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `/fetch live status`, `/fetch live test`, and `/fetch live reconcile`.
- Adds: stored channel/role, presence intent, Twitch availability, `Manage Roles`, role hierarchy, and mention permission to factual health output.
- Consumes: Task 5 service, Task 6 renderer, and Task 7 reconciliation callback.

- [ ] **Step 1: Write failing command and health tests**

Test guild-only and Manage-Guild authorization for all three commands; ephemeral preview; preview omits message content and allowed mentions; preview cannot call role or send adapters; reconcile calls only the idempotent callback; status is guild-scoped; inventory records configured channel/role IDs; and health reports missing presence intent, Twitch credentials, channel, role, hierarchy, manage-role, and mention-everyone capabilities distinctly. Define `FakeLiveService`, `FakeReconcileCallback`, and `FakeInteraction` in the test file; each stores every call so the assertions prove the preview is non-mutating.

```python
@pytest.mark.asyncio
async def test_fetch_live_test_is_ephemeral_and_cannot_ping_or_mutate() -> None:
    commands = LiveCommands(parent, live_service, reconcile_callback)
    interaction = FakeInteraction(manager=True)
    await command(commands, "test").callback(commands, interaction)
    assert interaction.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.edited_embed is not None
    assert runtime.role_calls == []
    assert runtime.public_send_calls == []
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_live_signal_commands.py tests/test_phase_one_commands.py tests/test_discord_inventory.py tests/test_health_service.py tests/test_cli.py -q`

Expected: FAIL because the command group and Phase 2 health facts are absent.

- [ ] **Step 3: Implement commands and health integration**

Add `LiveCommands` beneath `/fetch`, using the existing parent authorization and receipt helpers. `status` renders active session state and last Discord/Twitch checks. `test` constructs a fixed sample `TwitchStream`, renders only the embed/view ephemerally, and never includes the public alien-language content. `reconcile` calls `LiveSignalRuntime.reconcile_guild()` and receipts the number of plans applied.

Extend inventory capture with optional `live_channel_id`, `streaming_role_id`, and explicit Phase 2 capability facts. Switch Phase 2 runtime captures from `phase_one_permissions()` to `phase_two_permissions()`. Keep findings factual and deterministic.

- [ ] **Step 4: Run command, health, and full focused tests**

Run: `uv run pytest tests/test_live_signal_commands.py tests/test_phase_one_commands.py tests/test_discord_inventory.py tests/test_health_service.py tests/test_cli.py -q`

Expected: PASS.

Run: `uv run ruff check src/krubit/discord/live_commands.py src/krubit/discord/inventory.py src/krubit/services/health.py tests/test_live_signal_commands.py && uv run pyright src/krubit/discord/live_commands.py src/krubit/discord/inventory.py src/krubit/services/health.py tests/test_live_signal_commands.py`

Expected: PASS and zero Pyright errors.

- [ ] **Step 5: Commit**

```powershell
git add src/krubit/discord/live_commands.py src/krubit/discord/bot.py src/krubit/discord/inventory.py src/krubit/services/health.py tests/test_live_signal_commands.py tests/test_phase_one_commands.py tests/test_discord_inventory.py tests/test_health_service.py tests/test_cli.py
git commit -m "feat: add live signal staff controls"
```

---

### Task 9: Operator Documentation and Phase 2A Devlog

**Files:**
- Create: `docs/operations/phase-2a-live-stream-signals.md`
- Create: `docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md`
- Modify: `README.md`
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`

**Interfaces:**
- Produces: exact setup, permission, smoke-test, degraded-mode, recovery, rollback, and secret-rotation procedures.
- Consumes: implemented commands and runtime behavior from Tasks 1 through 8.

- [ ] **Step 1: Write the operations guide from the implemented interfaces**

Document these exact operator checks:

```text
Developer Portal -> Krubit -> Bot -> Privileged Gateway Intents -> Presence Intent: ON
Krubit role permissions: Manage Roles and Mention @everyone, @here, and All Roles
Role hierarchy: Krubit above Streaming Now
Destination: #live-notifications
Master env: Twitch Client ID/Secret present; KRUBIT_LIVE_SIGNALS_ENABLED=false for shadow start
Preview: /fetch live test
Preflight: /fetch permissions, /fetch integrations, /fetch live status
Manual reconciliation: /fetch live reconcile
```

Include rollback: set `KRUBIT_LIVE_SIGNALS_ENABLED=false`, restart Krubit, run reconciliation/cleanup, manually remove only stale `Streaming Now` roles that receipts prove Krubit assigned, and leave Phase 1 monitoring online.

- [ ] **Step 2: Update README and roadmap status**

Add Phase 2A capability bullets and links. Mark only the Twitch/Discord-presence slice as implemented; leave YouTube and later Phase 2 deliverables pending. State the activity-sharing limitation plainly.

- [ ] **Step 3: Write the devlog with verification slots populated from actual commands**

Record the commit sequence, schema additions, commands, permission expansion, test counts, Ruff/Pyright results, and live-canary receipts. Do not include credentials, tokens, raw environment values, or Twitch response bodies.

- [ ] **Step 4: Validate documentation**

Run: `Select-String -Path README.md,docs\operations\phase-2a-live-stream-signals.md,docs\devlogs\2026-08-04-phase-2a-live-stream-signals.md -Pattern ('T'+'BD|T'+'ODO|FIX'+'ME')`

Expected: no matches.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 5: Commit**

```powershell
git add README.md docs/operations/phase-2a-live-stream-signals.md docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md docs/roadmaps/2026-08-03-krubit-phase-rollout.md
git commit -m "docs: add Phase 2A live signal operations"
```

---

### Task 10: Full Verification and Controlled Live Canary

**Files:**
- Modify only if verification exposes a defect: the owning source/test file from Tasks 1 through 9.
- Update after successful live checks: `docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md`.

**Interfaces:**
- Validates every Phase 2A acceptance criterion end to end.
- Produces: final test/static-analysis evidence and redacted live receipts.

- [ ] **Step 1: Run the complete automated verification suite**

```powershell
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run pyright
git diff --check
```

Expected: all tests pass, Ruff is clean, Pyright reports zero errors, and Git reports no whitespace errors.

- [ ] **Step 2: Confirm runtime configuration without printing secrets**

Use a Boolean-only PowerShell check for `TWITCH_KRUBIT_CLIENT_ID`, `TWITCH_KRUBIT_CLIENT_SECRET`, and `KRUBIT_LIVE_SIGNALS_ENABLED`. Confirm Presence Intent in the Developer Portal and run:

```powershell
.\scripts\invoke-krubit.ps1 install-url
```

Verify the calculated install permissions include Manage Roles and Mention Everyone but not Administrator. Confirm the Krubit role remains above `Streaming Now`.

- [ ] **Step 3: Start in shadow mode and exercise private controls**

Keep `KRUBIT_LIVE_SIGNALS_ENABLED=false`, restart through the existing launcher, and run:

```text
/fetch live test
/fetch live status
/fetch permissions
/fetch integrations
```

Expected: the approved card previews ephemerally; there is no role change, public message, or mass mention; all required capabilities report healthy.

- [ ] **Step 4: Obtain explicit canary go-ahead, enable live actions, and observe one stream**

Set `KRUBIT_LIVE_SIGNALS_ENABLED=true` only after the user confirms the server is ready for a real `@everyone` ping. Restart Krubit, then have one server member expose a genuine Discord Twitch Streaming status.

Expected within the five-second enrichment budget or degraded fallback:

- exactly one creature-language message in `#live-notifications`;
- exactly one `@everyone` notification;
- Twitch name, title, category, thumbnail, status, and `Fetch the Stream` link when Helix succeeds;
- `Streaming Now` added to that member; and
- no other role changed.

- [ ] **Step 5: Verify end, restart, and failure recovery**

End the canary stream and confirm `Streaming Now` is removed while all original roles remain. During a second canary, restart Krubit and verify the existing announcement is reused with no second ping. Exercise a redacted Twitch-unavailable fixture or temporary credential-free test process and verify reduced delivery/recovery edits the same message without exposing the secret.

- [ ] **Step 6: Record evidence and commit any final documentation-only updates**

Add test counts, static-check results, Discord message/role receipt IDs, and observed timestamps to the devlog. Never record token values or raw Twitch response bodies.

```powershell
git add docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md
git commit -m "docs: record Phase 2A live canary"
```

- [ ] **Step 7: Final repository check**

Run: `git status --short && git log -10 --oneline`

Expected: clean worktree and an intentional commit for each independently reviewable task.
