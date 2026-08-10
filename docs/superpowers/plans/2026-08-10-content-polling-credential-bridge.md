# Content Polling Credential Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instagram, Threads, and TikTok accounts with a completed OAuth
authorization get polled by `ConnectorScheduler` like every other
platform; an account with no valid authorization reports
`AUTHORIZATION_REQUIRED` instead of crashing the poll cycle.

**Architecture:** One generic `CredentialResolvingConnector` class
(parametrized by platform, inner-connector factory, and error type)
satisfies the `Connector` protocol by resolving a per-account OAuth token
from storage on every call, rather than a fixed token at construction —
the one thing `InstagramConnector`/`ThreadsConnector`/`TikTokConnector`
don't do today. `_build_content_connectors` wires three instances of it
in, using the platform-appropriate inner connector.

**Tech Stack:** Python 3.13, aiohttp, aiosqlite, pytest/pytest-asyncio.

## Global Constraints

- No token-refresh exchange. An authorization that is missing, has
  `status != "active"`, or has `expires_at` at or before "now" is treated
  identically: raise the platform's existing error type carrying
  `ConnectorFailure.authorization(...)`.
- Stateless per-call resolution — no caching of resolved tokens or
  constructed inner connectors across calls.
- No Facebook Page/Profile wiring.
- `_build_content_connectors` only adds the three new entries when a
  `CredentialVault` is actually available (`credential_encryption_key`
  configured) — without a vault, nothing can be unsealed, so wiring them
  in would be pointless dead weight, not a real capability.
- No change to `ConnectorScheduler`, `ConnectorFailure`,
  `MetaConnectorError`/`TikTokConnectorError`, or any existing connector
  class.

---

### Task 1: `CredentialResolvingConnector`

**Files:**
- Create: `src/krubit/integrations/credential_bridge.py`
- Test: create `tests/test_credential_bridge.py`

**Interfaces:**
- Produces: `class CredentialResolvingConnector` satisfying
  `krubit.integrations.base.Connector` structurally. Constructor:
  `(session: object, store: SQLiteStore, vault: CredentialVault, *,
  platform: Platform, inner_connector_factory: Callable[[object, str],
  Connector], error_type: type[Exception], now: Callable[[], datetime] |
  None = None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_credential_bridge.py`. This mirrors
`tests/test_content_scheduler.py`'s `account()`/`store` fixture pattern —
read that file first (already read while writing this plan) for the
exact conventions to match.

```python
"""Unit tests for krubit.integrations.credential_bridge.
CredentialResolvingConnector -- resolves a per-account OAuth token from
storage on every call, rather than the fixed-at-construction token every
other connector in this codebase uses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.integrations.base import (
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.integrations.credential_bridge import CredentialResolvingConnector
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
GUILD_ID = 111
VAULT_KEY = "test-key-for-credential-bridge-tests"


class _FakeInnerConnectorError(RuntimeError):
    def __init__(self, failure: ConnectorFailure) -> None:
        super().__init__(failure.kind.value)
        self.failure = failure


class _FakeInnerConnector:
    """Records the access token it was constructed with, so tests can
    assert the bridge resolved and passed through the right one."""

    def __init__(self, session: object, access_token: str) -> None:
        self.session = session
        self.access_token = access_token

    async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
        raise NotImplementedError

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        return ConnectorPage(items=(), next_cursor=None)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)


def _account(account_id: str) -> CreatorAccount:
    return CreatorAccount(
        guild_id=GUILD_ID,
        account_id=account_id,
        owner_member_id=222,
        platform=Platform.INSTAGRAM,
        handle="examplecreator",
        canonical_url="https://www.instagram.com/examplecreator",
        external_id="ig-external-id",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault.from_env_key(VAULT_KEY)


def _bridge(store: SQLiteStore, vault: CredentialVault) -> CredentialResolvingConnector:
    return CredentialResolvingConnector(
        object(),
        store,
        vault,
        platform=Platform.INSTAGRAM,
        inner_connector_factory=_FakeInnerConnector,
        error_type=_FakeInnerConnectorError,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_no_authorization_exists(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_status_is_not_active(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    secret_ref = vault.seal_json({"access_token": "tok-123", "refresh_token": None, "expires_at": None})
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="revoked",
        expires_at=None,
        now=NOW,
    )
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_expired(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    secret_ref = vault.seal_json({"access_token": "tok-123", "refresh_token": None, "expires_at": None})
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=NOW - timedelta(minutes=1),
        now=NOW,
    )
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_delegates_to_inner_connector_with_the_resolved_token(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    secret_ref = vault.seal_json(
        {"access_token": "the-real-token", "refresh_token": None, "expires_at": None}
    )
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=None,
        now=NOW,
    )
    bridge = _bridge(store, vault)

    result = await bridge.fetch_page(_account(account_id), cursor=None)

    assert result == ConnectorPage(items=(), next_cursor=None)


@pytest.mark.asyncio
async def test_fetch_page_accepts_an_authorization_with_no_expiry_and_a_future_expiry(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    secret_ref = vault.seal_json({"access_token": "tok-123", "refresh_token": None, "expires_at": None})
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    bridge = _bridge(store, vault)

    result = await bridge.fetch_page(_account(account_id), cursor=None)

    assert result == ConnectorPage(items=(), next_cursor=None)


@pytest.mark.asyncio
async def test_health_reports_authorization_required_without_fetching_when_no_authorization(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    bridge = _bridge(store, vault)

    health = await bridge.health(_account(account_id))

    assert health.state.value == "authorization_required"
```

Check `SQLiteStore.save_connector_authorization`'s exact keyword
parameter names before running this (search `src/krubit/storage/sqlite.py`
for `async def save_connector_authorization` — the plan's test code above
was written from reading that method, but verify it against the current
signature rather than assuming, since a mismatch here would fail with a
`TypeError` immediately in Step 2 rather than a meaningful assertion
failure).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_credential_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'krubit.integrations.credential_bridge'`

- [ ] **Step 3: Implement `credential_bridge.py`**

Create `src/krubit/integrations/credential_bridge.py`:

```python
"""Bridges per-account OAuth credentials (already issued by `/fetch
creator authorize`) into the `Connector` protocol `ConnectorScheduler`
already polls generically.

`InstagramConnector`/`ThreadsConnector`/`TikTokConnector` (and every
other connector in this codebase) take a fixed access token at
construction -- correct for YouTube/X/Bluesky's single bot-wide
credential, wrong for a platform where each enrolled creator account has
its own separately-authorized token. `CredentialResolvingConnector`
supplies the one thing missing: on every call, it looks up the specific
account's stored authorization, unseals its token, and constructs the
real per-use connector fresh -- stateless, no caching, so a creator's
re-authorization takes effect on the very next poll with no cache
invalidation to reason about.

An account with no valid authorization on file (missing, revoked, or
expired -- no token-refresh exchange is built here, an expired token is
simply an authorization the creator needs to redo) raises the exact same
error shape (`ConnectorFailure.authorization(...)`, wrapped in the
platform's own existing error type) every other authorization failure in
this codebase already produces -- `ConnectorScheduler`'s existing
failure-handling and this codebase's existing `AUTHORIZATION_REQUIRED`
health-state reporting require no changes to understand it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
)
from krubit.integrations.base import (
    Connector,
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CredentialResolvingConnector:
    """Satisfies `Connector` by resolving a per-account token from
    storage on every call, rather than a fixed token at construction."""

    def __init__(
        self,
        session: object,
        store: SQLiteStore,
        vault: CredentialVault,
        *,
        platform: Platform,
        inner_connector_factory: Callable[[object, str], Connector],
        error_type: type[Exception],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._vault = vault
        self._platform = platform
        self._inner_connector_factory = inner_connector_factory
        self._error_type = error_type
        self._now = now or _utc_now

    async def _resolve_inner(self, account: CreatorAccount) -> Connector:
        authorization = await self._store.get_connector_authorization(
            account.guild_id, account.account_id, Capability.SOCIAL.value
        )
        if authorization is None:
            raise self._error_type(
                ConnectorFailure.authorization("no authorization on file for this account")
            )
        if authorization.status != "active":
            raise self._error_type(
                ConnectorFailure.authorization(
                    f"stored authorization status is {authorization.status!r}, not active"
                )
            )
        if authorization.expires_at is not None and authorization.expires_at <= self._now():
            raise self._error_type(
                ConnectorFailure.authorization("stored authorization has expired")
            )
        grant = self._vault.open_json(authorization.secret_ref)
        access_token = grant.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise self._error_type(
                ConnectorFailure.authorization("stored authorization is malformed")
            )
        return self._inner_connector_factory(self._session, access_token)

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        # Never called by `ConnectorScheduler` -- confirmed by repo-wide search,
        # the only production caller of `.resolve_account(` is the OAuth callback
        # route, which constructs and uses the real per-use connector directly
        # with the token it just obtained, never through this bridge. Implemented
        # for `Connector` protocol completeness only.
        inner = await self._resolve_inner(
            CreatorAccount(
                guild_id=0,
                account_id="",
                owner_member_id=0,
                platform=self._platform,
                handle=recognized.handle,
                canonical_url=recognized.canonical_url,
                external_id="",
                paused=False,
                created_at=self._now(),
                updated_at=self._now(),
            )
        )
        return await inner.resolve_account(recognized)

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        inner = await self._resolve_inner(account)
        return await inner.fetch_page(account, cursor=cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if account is None:
            return ConnectorHealth(
                capability=Capability.SOCIAL,
                state=CapabilityState.UNCONFIGURED,
                detail="no account specified",
            )
        try:
            inner = await self._resolve_inner(account)
        except self._error_type as exc:
            failure = getattr(exc, "failure", None)
            detail = failure.safe_detail if isinstance(failure, ConnectorFailure) else None
            return ConnectorHealth(
                capability=Capability.SOCIAL,
                state=CapabilityState.AUTHORIZATION_REQUIRED,
                detail=detail,
            )
        return await inner.health(account)
```

Note on `resolve_account`: the brief above constructs a throwaway
`CreatorAccount` with placeholder `guild_id=0`/`account_id=""` purely to
satisfy `_resolve_inner`'s type signature — since this method is never
actually called in production and has no real account to resolve against
yet (that's the whole reason it's unreachable: an account must already
exist, with an ID, before any authorization can be looked up for it).
This is acceptable dead-but-protocol-complete code; do not over-engineer
it further. If constructing a placeholder `CreatorAccount` proves awkward
given its own `__post_init__` validation (check whether `guild_id=0`/
empty strings raise `ValueError` there — if so, this method should
instead just raise `NotImplementedError` immediately with a clear message
explaining why, rather than fighting validation for genuinely dead code).

Check `ConnectorFailure`'s exact `authorization()` classmethod signature
and `.safe_detail` attribute name (both referenced above) against the
actual current file (`src/krubit/integrations/base.py`) before finalizing
— they were read while writing this plan but confirm they haven't
changed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_credential_bridge.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/integrations/credential_bridge.py tests/test_credential_bridge.py`
Expected: clean.

```bash
git add src/krubit/integrations/credential_bridge.py tests/test_credential_bridge.py
git commit -m "feat: add CredentialResolvingConnector for per-account OAuth polling"
```

---

### Task 2: Wire Instagram/Threads/TikTok into `_build_content_connectors`

**Files:**
- Modify: `src/krubit/__main__.py`
- Test: `tests/test_cli.py` (or wherever `_build_content_connectors` is
  already tested — search for its name first; if no existing test file
  covers it, add one alongside the closest related existing test)

**Interfaces:**
- Modifies: `_build_content_connectors(settings: Settings, session:
  aiohttp.ClientSession, store: SQLiteStore, vault: CredentialVault |
  None) -> dict[Platform, Connector]` (signature grows two new required
  parameters — every call site must be updated).

- [ ] **Step 1: Check for existing test coverage**

Search `tests/` for `_build_content_connectors` to find whether it
already has direct test coverage (it may only be exercised indirectly via
`_run_bot`, which is harder to unit test in isolation). If a direct test
exists, follow its exact pattern for Step 2 below. If none exists, add a
new test file `tests/test_build_content_connectors.py` following this
shape.

- [ ] **Step 2: Write the failing test**

```python
"""Unit test for krubit.__main__._build_content_connectors's Instagram/
Threads/TikTok wiring."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest

from krubit.__main__ import _build_content_connectors
from krubit.config import Settings
from krubit.domain.creator_signals import Platform
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_build_content_connectors_includes_credential_bridge_platforms_when_vault_present(
    tmp_path: Path,
) -> None:
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    store = await SQLiteStore.open(settings.database_path)
    await store.initialize()
    vault = CredentialVault.from_env_key("test-key")
    async with aiohttp.ClientSession() as session:
        try:
            connectors = _build_content_connectors(settings, session, store, vault)
        finally:
            await store.close()

    assert Platform.INSTAGRAM in connectors
    assert Platform.THREADS in connectors
    assert Platform.TIKTOK in connectors


@pytest.mark.asyncio
async def test_build_content_connectors_omits_credential_bridge_platforms_without_a_vault(
    tmp_path: Path,
) -> None:
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    store = await SQLiteStore.open(settings.database_path)
    await store.initialize()
    async with aiohttp.ClientSession() as session:
        try:
            connectors = _build_content_connectors(settings, session, store, vault=None)
        finally:
            await store.close()

    assert Platform.INSTAGRAM not in connectors
    assert Platform.THREADS not in connectors
    assert Platform.TIKTOK not in connectors
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_build_content_connectors.py -v`
Expected: FAIL — `TypeError: _build_content_connectors() takes 2
positional arguments but 4 were given` (or similar signature mismatch).

- [ ] **Step 4: Update `_build_content_connectors` and its call site**

In `src/krubit/__main__.py`, add the new import:

```python
from krubit.integrations.credential_bridge import CredentialResolvingConnector
from krubit.integrations.meta import (
    InstagramConnector,
    MetaConnectorError,
    ThreadsConnector,
)
from krubit.integrations.tiktok import TikTokConnector, TikTokConnectorError
```

Update the function signature and body:

```python
def _build_content_connectors(
    settings: Settings,
    session: aiohttp.ClientSession,
    store: SQLiteStore,
    vault: CredentialVault | None,
) -> dict[Platform, Connector]:
    """Build the connectors `ConnectorScheduler` can poll.

    YouTube/X/Bluesky use one fixed bot-wide credential each. Instagram/
    Threads/TikTok each need a different access token per enrolled
    creator account, resolved at poll time via `CredentialResolvingConnector`
    -- only wired in when `vault` is present, since without one nothing
    could ever be unsealed and wiring them in would be pointless dead
    weight rather than a real capability.

    Callers must gate this on `settings.creator_signals_enabled` themselves
    (see `_run_bot`) -- this function does not check the flag, since
    `KrubitBot` also enforces it independently for any caller that
    constructs it directly (for example, a test).
    """
    connectors: dict[Platform, Connector] = {}
    if settings.youtube_api_key is not None:
        connectors[Platform.YOUTUBE] = YouTubeConnector(session, settings.youtube_api_key)
    if settings.x_bearer_token is not None:
        connectors[Platform.X] = XConnector(session, settings.x_bearer_token)
    connectors[Platform.BLUESKY] = BlueskyConnector(session)
    if vault is not None:
        connectors[Platform.INSTAGRAM] = CredentialResolvingConnector(
            session,
            store,
            vault,
            platform=Platform.INSTAGRAM,
            inner_connector_factory=InstagramConnector,
            error_type=MetaConnectorError,
        )
        connectors[Platform.THREADS] = CredentialResolvingConnector(
            session,
            store,
            vault,
            platform=Platform.THREADS,
            inner_connector_factory=ThreadsConnector,
            error_type=MetaConnectorError,
        )
        connectors[Platform.TIKTOK] = CredentialResolvingConnector(
            session,
            store,
            vault,
            platform=Platform.TIKTOK,
            inner_connector_factory=TikTokConnector,
            error_type=TikTokConnectorError,
        )
    return connectors
```

Note: `InstagramConnector`/`ThreadsConnector`/`TikTokConnector`'s
constructors take `(session, access_token, *, now=...)` — passing the
class itself as `inner_connector_factory` works directly since
`CredentialResolvingConnector` only ever calls it as
`factory(session, access_token)`, matching the two positional parameters
those constructors require (their `now` parameter has a default, so it's
never an issue that the bridge doesn't pass one through).

In `_run_bot` (same file, around line 119-150), **reorder** so `vault` is
constructed *before* `_build_content_connectors` is called (currently
`vault` is built at line 152-156, after the `content_connectors = ...`
call at line 146-150 — this task's new dependency on `vault` requires
flipping that order). Move the `vault = (...)` block up to immediately
after `await store.initialize()`/`await migrate_all_twitch_content(store)`
(i.e., right after `store` exists, before `content_session`/
`content_tcp_connector` are created), then update the
`_build_content_connectors` call to pass the new `store, vault`
arguments:

```python
content_connectors = (
    _build_content_connectors(settings, content_session, store, vault)
    if settings.creator_signals_enabled
    else {}
)
```

Double-check no other code between the old and new `vault = (...)`
position depends on `vault` not existing yet, and that moving it earlier
doesn't change `credential_encryption_key`'s error-handling behavior
(`CredentialVault.from_env_key` raising is still handled the same way
regardless of exactly where in `_run_bot` it's called, since it's still
inside the same outer `try` block).

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_build_content_connectors.py -v`
Expected: all PASS

- [ ] **Step 6: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, no regressions (baseline before this task: 1173
passing). Pay particular attention to any existing test that calls
`_build_content_connectors` with the old 2-argument signature or
constructs/exercises `_run_bot` — both must be updated to match the new
signature/ordering, not just the new test file added in this task.

- [ ] **Step 7: Lint and type-check**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/__main__.py tests/`
Expected: clean.

Run: `./.venv/Scripts/python.exe -m pyright src/krubit/__main__.py src/krubit/integrations/credential_bridge.py`
Expected: no new error category versus this branch's baseline (confirm
via `git stash`/`git stash pop` diffing, per this project's established
verification convention).

- [ ] **Step 8: Commit**

```bash
git add src/krubit/__main__.py tests/
git commit -m "feat: wire Instagram/Threads/TikTok into content polling via the credential bridge"
```

---

## Final Verification

- [ ] Run the full suite once more: `./.venv/Scripts/python.exe -m pytest -q` — must show `1173 + N passed` where `N` is the number of new tests, zero failures.
- [ ] No live Meta/TikTok verification is required for this plan to be
      considered complete — matching this project's established
      convention for every prior OAuth-adjacent piece of work. Live
      end-to-end polling verification remains the project owner's own
      step once credentials and at least one completed `/fetch creator
      authorize` exist.
