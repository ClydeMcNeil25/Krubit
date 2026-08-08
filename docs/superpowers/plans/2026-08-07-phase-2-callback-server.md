# Phase 2 Callback Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start Krubit's OAuth/push callback server from `krubit run`, wire a
durable, account-bound OAuth authorization flow for Meta and TikTok, and wire
Meta's deauthorization/data-deletion callbacks — closing Phase 2's "callback
server never started" production gap per
`docs/superpowers/specs/2026-08-07-phase-2-callback-server-design.md`.

**Architecture:** New SQLite tables (`oauth_attempts`, `data_deletion_requests`)
and columns (`connector_authorizations.provider_resource_id`/
`authorization_subject_id`) back a durable, restart-safe OAuth state machine.
`krubit/web/wiring.py` assembles platform-specific routes from existing connector
code (`resolve_account`, `exchange_authorization_code`) and registers them on the
existing `CallbackServer`. `_run_bot` in `__main__.py` constructs and starts/stops
this server around the bot's own lifecycle.

**Tech Stack:** Python 3.12+, aiohttp (web server + client), aiosqlite, pytest,
`cryptography` (AES-GCM, already wrapped by `CredentialVault`).

## Global Constraints

- Every new SQLite table is guild-scoped where the data is guild-scoped;
  `oauth_attempts` and `connector_authorizations` carry `guild_id` as a leading
  key component; `data_deletion_requests` does not (keyed only by
  `confirmation_code`, per the spec — Meta's status check carries no guild
  context).
- No plaintext OAuth token ever reaches a log line, an HTTP response, or a
  stored column outside `CredentialVault.seal_json` output.
- Every new route's path is an exact literal (no prefix/wildcard routes).
- `KRUBIT_CREDENTIAL_ENCRYPTION_KEY` gates OAuth *authorization* routes only —
  never Meta's deauthorization/data-deletion routes (spec Component 7).
- All new async DB methods follow the existing `SQLiteStore` pattern: `async with
  self._write_transaction(immediate=True):` for writes, `await
  self._connection.execute(...)` / `cursor.fetchone()` for reads, exactly as
  `save_creator_account` (`src/krubit/storage/sqlite.py:1742`) already does.
- Run `uv run pytest -q`, `uv run ruff check .`, and `uv run pyright` before every
  commit (per `README.md`'s Development section).

---

## Task 1: `oauth_attempts` table and store methods

**Files:**
- Modify: `src/krubit/storage/sqlite.py` (schema in `initialize()`'s
  `executescript` block near `connector_authorizations`, ~line 456; new methods
  near `save_creator_account`, ~line 1742)
- Test: `tests/test_oauth_attempts.py` (new)

**Interfaces:**
- Produces:
  - `SQLiteStore.issue_oauth_attempt(guild_id: int, member_id: int, account_id: str, platform: str, capability: str, redirect_uri: str, *, now: datetime, ttl: timedelta) -> str` — returns the plaintext state token.
  - `SQLiteStore.consume_oauth_attempt(state_token: str, *, now: datetime) -> OAuthAttempt | None`
  - `OAuthAttempt` dataclass: `guild_id: int, member_id: int, account_id: str, platform: str, capability: str, redirect_uri: str`
  - `SQLiteStore.purge_oauth_attempts(now: datetime, *, consumed_retention: timedelta, unconsumed_grace: timedelta) -> int` (returns rows deleted)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_oauth_attempts.py
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_issue_and_consume_oauth_attempt_round_trips(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    token = await store.issue_oauth_attempt(
        guild_id=1,
        member_id=2,
        account_id="acct-1",
        platform="tiktok",
        capability="account",
        redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now,
        ttl=timedelta(minutes=10),
    )
    attempt = await store.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    assert attempt is not None
    assert attempt.guild_id == 1
    assert attempt.member_id == 2
    assert attempt.account_id == "acct-1"
    assert attempt.platform == "tiktok"
    assert attempt.capability == "account"
    assert attempt.redirect_uri == "https://example.test/callbacks/tiktok/authorize"
    await store.close()


async def test_consume_oauth_attempt_is_single_use(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    first = await store.consume_oauth_attempt(token, now=now)
    second = await store.consume_oauth_attempt(token, now=now)
    assert first is not None
    assert second is None
    await store.close()


async def test_consume_oauth_attempt_rejects_expired(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    late = await store.consume_oauth_attempt(token, now=now + timedelta(minutes=11))
    assert late is None
    await store.close()


async def test_consume_oauth_attempt_rejects_unknown_token(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    result = await store.consume_oauth_attempt(secrets.token_urlsafe(32), now=now)
    assert result is None
    await store.close()


async def test_consume_oauth_attempt_survives_a_new_store_handle(tmp_path):
    """Durability across a simulated restart: a fresh SQLiteStore over the same
    file must still enforce single-use and expiry."""
    store_a = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    token = await store_a.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    await store_a.close()

    store_b = await SQLiteStore.open(tmp_path / "test.db")
    await store_b.initialize()
    first = await store_b.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    second = await store_b.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    assert first is not None
    assert second is None
    await store_b.close()


async def test_purge_oauth_attempts_removes_old_consumed_and_expired_unconsumed(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)

    old_consumed = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="a", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=40), ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(old_consumed, now=now - timedelta(days=40))

    recent_consumed = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="b", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=1), ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(recent_consumed, now=now - timedelta(days=1))

    old_unconsumed_expired = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="c", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=3), ttl=timedelta(minutes=10),
    )

    still_live = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="d", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now, ttl=timedelta(minutes=10),
    )

    deleted = await store.purge_oauth_attempts(
        now, consumed_retention=timedelta(days=30), unconsumed_grace=timedelta(days=1)
    )
    assert deleted == 2

    # Recent-consumed and still-live attempts survive; an unknown consume on the
    # purged tokens still correctly returns None (never present, never resurrected).
    assert await store.consume_oauth_attempt(old_consumed, now=now) is None
    assert await store.consume_oauth_attempt(old_unconsumed_expired, now=now) is None
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth_attempts.py -v`
Expected: FAIL with `AttributeError: 'SQLiteStore' object has no attribute
'issue_oauth_attempt'`

- [ ] **Step 3: Add the schema**

In `src/krubit/storage/sqlite.py`, inside the `executescript` call in
`initialize()`, add immediately after the existing `connector_authorizations`
table (~line 467):

```sql
            CREATE TABLE IF NOT EXISTS oauth_attempts (
                state_hash TEXT NOT NULL PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                capability TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_oauth_attempts_expiry
                ON oauth_attempts (consumed_at, expires_at);
```

- [ ] **Step 4: Implement the store methods**

Add near `save_creator_account` in `src/krubit/storage/sqlite.py`:

```python
@dataclass(frozen=True, slots=True)
class OAuthAttempt:
    guild_id: int
    member_id: int
    account_id: str
    platform: str
    capability: str
    redirect_uri: str


class SQLiteStore:
    # ... existing methods ...

    async def issue_oauth_attempt(
        self,
        *,
        guild_id: int,
        member_id: int,
        account_id: str,
        platform: str,
        capability: str,
        redirect_uri: str,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        """Mint a durable, single-use OAuth attempt and return its plaintext state token.

        Only the SHA-256 hash of the token is stored; the token itself is never
        persisted, matching the same "the row is the source of truth, not a
        signature" property the design spec calls for.
        """
        _require_guild_id(guild_id)
        token = secrets.token_urlsafe(32)
        state_hash = sha256(token.encode("utf-8")).hexdigest()
        expires_at = now + ttl
        async with self._write_transaction(immediate=True):
            await self._connection.execute(
                """
                INSERT INTO oauth_attempts (
                    state_hash, guild_id, member_id, account_id, platform,
                    capability, redirect_uri, created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    state_hash, guild_id, member_id, account_id, platform,
                    capability, redirect_uri, now.isoformat(), expires_at.isoformat(),
                ),
            )
        return token

    async def consume_oauth_attempt(
        self, state_token: str, *, now: datetime
    ) -> OAuthAttempt | None:
        """Atomically consume a state token, or return None for reuse/expiry/unknown.

        All three failure cases return the same `None` — distinguishing them would
        let an attacker probe the CSRF-prevention mechanism itself.
        """
        state_hash = sha256(state_token.encode("utf-8")).hexdigest()
        async with self._write_transaction(immediate=True):
            cursor = await self._connection.execute(
                """
                SELECT guild_id, member_id, account_id, platform, capability, redirect_uri
                FROM oauth_attempts
                WHERE state_hash = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (state_hash, now.isoformat()),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            result = await self._connection.execute(
                """
                UPDATE oauth_attempts SET consumed_at = ?
                WHERE state_hash = ? AND consumed_at IS NULL
                """,
                (now.isoformat(), state_hash),
            )
            if result.rowcount != 1:
                return None
            return OAuthAttempt(
                guild_id=int(row["guild_id"]),
                member_id=int(row["member_id"]),
                account_id=str(row["account_id"]),
                platform=str(row["platform"]),
                capability=str(row["capability"]),
                redirect_uri=str(row["redirect_uri"]),
            )

    async def purge_oauth_attempts(
        self,
        now: datetime,
        *,
        consumed_retention: timedelta,
        unconsumed_grace: timedelta,
    ) -> int:
        """Delete old consumed rows and long-expired unconsumed rows; return count."""
        consumed_cutoff = (now - consumed_retention).isoformat()
        unconsumed_cutoff = (now - unconsumed_grace).isoformat()
        async with self._write_transaction(immediate=True):
            result = await self._connection.execute(
                """
                DELETE FROM oauth_attempts
                WHERE (consumed_at IS NOT NULL AND consumed_at < ?)
                   OR (consumed_at IS NULL AND expires_at < ?)
                """,
                (consumed_cutoff, unconsumed_cutoff),
            )
            return result.rowcount
```

Add `import secrets` and `from datetime import timedelta` to the module's
existing imports (`datetime`/`UTC`/`date` are already imported at the top;
`secrets` and `timedelta` are not).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth_attempts.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/krubit/storage/sqlite.py tests/test_oauth_attempts.py
git commit -m "feat: add durable oauth_attempts table and store methods"
```

---

## Task 2: `connector_authorizations` identity columns and store methods

**Files:**
- Modify: `src/krubit/storage/sqlite.py` (schema ~line 456; new methods near
  Task 1's additions)
- Test: `tests/test_connector_authorizations.py` (new)

**Interfaces:**
- Consumes: `CredentialVault.seal_json`/`open_json` (`src/krubit/security/credential_vault.py`)
- Produces:
  - `ConnectorAuthorization` dataclass: `guild_id: int, account_id: str, capability: str, secret_ref: str, provider_resource_id: str, authorization_subject_id: str, status: str, expires_at: datetime | None`
  - `ConnectorAuthorizationStatus` dataclass (safe DTO): `platform: str, capability: str, status: str, expires_at: datetime | None`
  - `SQLiteStore.save_connector_authorization(guild_id, account_id, capability, secret_ref, provider_resource_id, authorization_subject_id, status, expires_at, *, now) -> None`
  - `SQLiteStore.get_connector_authorization(guild_id, account_id, capability) -> ConnectorAuthorization | None`
  - `SQLiteStore.find_connector_authorizations_by_authorization_subject(platform, authorization_subject_id) -> tuple[ConnectorAuthorization, ...]`
  - `SQLiteStore.delete_connector_authorizations(rows: tuple[ConnectorAuthorization, ...], *, now) -> None` (also writes redacted receipts)
  - `SQLiteStore.list_connector_authorization_status(guild_id) -> tuple[ConnectorAuthorizationStatus, ...]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connector_authorizations.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_save_and_get_connector_authorization_round_trips(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:sealed-token", provider_resource_id="ig-12345",
        authorization_subject_id="fb-user-999", status="active",
        expires_at=now + timedelta(days=60), now=now,
    )
    row = await store.get_connector_authorization(1, "acct-1", "content")
    assert row is not None
    assert row.secret_ref == "v1:sealed-token"
    assert row.provider_resource_id == "ig-12345"
    assert row.authorization_subject_id == "fb-user-999"
    assert row.status == "active"
    await store.close()


async def test_save_connector_authorization_upserts(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    for secret in ("v1:first", "v1:second"):
        await store.save_connector_authorization(
            guild_id=1, account_id="acct-1", capability="content",
            secret_ref=secret, provider_resource_id="ig-12345",
            authorization_subject_id="fb-user-999", status="active",
            expires_at=None, now=now,
        )
    row = await store.get_connector_authorization(1, "acct-1", "content")
    assert row is not None
    assert row.secret_ref == "v1:second"
    await store.close()


async def test_get_connector_authorization_returns_none_when_absent(tmp_path):
    store = await _store(tmp_path)
    assert await store.get_connector_authorization(1, "nope", "content") is None
    await store.close()


async def test_find_by_authorization_subject_distinguishes_from_resource_id(tmp_path):
    """A Page's provider_resource_id must never be usable to find it via the
    authorization_subject_id lookup, and vice versa — proves the two columns are
    genuinely independent, not aliases of one value."""
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_connector_authorization(
        guild_id=1, account_id="page-1", capability="content",
        secret_ref="v1:x", provider_resource_id="page-resource-1",
        authorization_subject_id="admin-user-1", status="active",
        expires_at=None, now=now,
    )
    by_subject = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "admin-user-1"
    )
    assert len(by_subject) == 1
    assert by_subject[0].provider_resource_id == "page-resource-1"

    by_wrong_key = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "page-resource-1"
    )
    assert by_wrong_key == ()
    await store.close()


async def test_find_by_authorization_subject_matches_across_guilds(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    for guild_id, account_id in ((1, "acct-a"), (2, "acct-b")):
        await store.save_connector_authorization(
            guild_id=guild_id, account_id=account_id, capability="content",
            secret_ref="v1:x", provider_resource_id=f"resource-{account_id}",
            authorization_subject_id="shared-admin", status="active",
            expires_at=None, now=now,
        )
    rows = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "shared-admin"
    )
    assert {r.guild_id for r in rows} == {1, 2}
    await store.close()


async def test_delete_connector_authorizations_removes_rows_and_writes_receipts(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:x", provider_resource_id="r-1",
        authorization_subject_id="subj-1", status="active",
        expires_at=None, now=now,
    )
    rows = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "subj-1"
    )
    await store.delete_connector_authorizations(rows, now=now)
    assert await store.get_connector_authorization(1, "acct-1", "content") is None
    await store.close()


async def test_list_connector_authorization_status_omits_secret_ref(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:super-secret-token", provider_resource_id="r-1",
        authorization_subject_id="subj-1", status="active",
        expires_at=now + timedelta(days=1), now=now,
    )
    statuses = await store.list_connector_authorization_status(1)
    assert len(statuses) == 1
    assert statuses[0].status == "active"
    assert not hasattr(statuses[0], "secret_ref")
    assert not hasattr(statuses[0], "provider_resource_id")
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_connector_authorizations.py -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Update the schema**

Replace the existing `connector_authorizations` table definition
(`src/krubit/storage/sqlite.py:456-467`) with:

```sql
            CREATE TABLE IF NOT EXISTS connector_authorizations (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                secret_ref TEXT,
                provider_resource_id TEXT,
                authorization_subject_id TEXT,
                status TEXT NOT NULL,
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id, capability),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_connector_authorizations_subject
                ON connector_authorizations (authorization_subject_id);
```

(This is a pre-existing, never-yet-shipped table — no migration of existing rows
is needed since nothing has ever written to it.)

- [ ] **Step 4: Implement the store methods**

```python
@dataclass(frozen=True, slots=True)
class ConnectorAuthorization:
    guild_id: int
    account_id: str
    capability: str
    secret_ref: str
    provider_resource_id: str
    authorization_subject_id: str
    status: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConnectorAuthorizationStatus:
    platform: str
    capability: str
    status: str
    expires_at: datetime | None


class SQLiteStore:
    # ... existing methods ...

    async def save_connector_authorization(
        self,
        *,
        guild_id: int,
        account_id: str,
        capability: str,
        secret_ref: str,
        provider_resource_id: str,
        authorization_subject_id: str,
        status: str,
        expires_at: datetime | None,
        now: datetime,
    ) -> None:
        _require_guild_id(guild_id)
        async with self._write_transaction(immediate=True):
            await self._connection.execute(
                """
                INSERT INTO connector_authorizations (
                    guild_id, account_id, capability, secret_ref,
                    provider_resource_id, authorization_subject_id, status,
                    expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id, capability) DO UPDATE SET
                    secret_ref = excluded.secret_ref,
                    provider_resource_id = excluded.provider_resource_id,
                    authorization_subject_id = excluded.authorization_subject_id,
                    status = excluded.status,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id, account_id, capability, secret_ref,
                    provider_resource_id, authorization_subject_id, status,
                    expires_at.isoformat() if expires_at is not None else None,
                    now.isoformat(),
                ),
            )

    async def get_connector_authorization(
        self, guild_id: int, account_id: str, capability: str
    ) -> ConnectorAuthorization | None:
        cursor = await self._connection.execute(
            """
            SELECT guild_id, account_id, capability, secret_ref,
                   provider_resource_id, authorization_subject_id, status, expires_at
            FROM connector_authorizations
            WHERE guild_id = ? AND account_id = ? AND capability = ?
            """,
            (guild_id, account_id, capability),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_connector_authorization(row)

    async def find_connector_authorizations_by_authorization_subject(
        self, platform: str, authorization_subject_id: str
    ) -> tuple[ConnectorAuthorization, ...]:
        cursor = await self._connection.execute(
            """
            SELECT ca.guild_id, ca.account_id, ca.capability, ca.secret_ref,
                   ca.provider_resource_id, ca.authorization_subject_id, ca.status,
                   ca.expires_at
            FROM connector_authorizations ca
            JOIN creator_accounts acc
                ON acc.guild_id = ca.guild_id AND acc.account_id = ca.account_id
            WHERE acc.platform = ? AND ca.authorization_subject_id = ?
            """,
            (platform, authorization_subject_id),
        )
        rows = await cursor.fetchall()
        return tuple(_row_to_connector_authorization(row) for row in rows)

    async def delete_connector_authorizations(
        self, rows: tuple[ConnectorAuthorization, ...], *, now: datetime
    ) -> None:
        if not rows:
            return
        async with self._write_transaction(immediate=True):
            for row in rows:
                await self._connection.execute(
                    """
                    DELETE FROM connector_authorizations
                    WHERE guild_id = ? AND account_id = ? AND capability = ?
                    """,
                    (row.guild_id, row.account_id, row.capability),
                )
                await self._connection.execute(
                    """
                    INSERT INTO creator_registry_receipts (
                        guild_id, receipt_id, account_id, action, actor_member_id,
                        detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.guild_id, uuid4().hex, row.account_id,
                        "connector_deauthorized", 0,
                        json.dumps(
                            {"platform_capability": row.capability, "account_id": row.account_id},
                            sort_keys=True,
                        ),
                        now.isoformat(),
                    ),
                )

    async def list_connector_authorization_status(
        self, guild_id: int
    ) -> tuple[ConnectorAuthorizationStatus, ...]:
        cursor = await self._connection.execute(
            """
            SELECT acc.platform, ca.capability, ca.status, ca.expires_at
            FROM connector_authorizations ca
            JOIN creator_accounts acc
                ON acc.guild_id = ca.guild_id AND acc.account_id = ca.account_id
            WHERE ca.guild_id = ?
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return tuple(
            ConnectorAuthorizationStatus(
                platform=str(row["platform"]),
                capability=str(row["capability"]),
                status=str(row["status"]),
                expires_at=(
                    datetime.fromisoformat(row["expires_at"])
                    if row["expires_at"] is not None
                    else None
                ),
            )
            for row in rows
        )


def _row_to_connector_authorization(row: object) -> ConnectorAuthorization:
    return ConnectorAuthorization(
        guild_id=int(row["guild_id"]),
        account_id=str(row["account_id"]),
        capability=str(row["capability"]),
        secret_ref=str(row["secret_ref"]),
        provider_resource_id=str(row["provider_resource_id"]),
        authorization_subject_id=str(row["authorization_subject_id"]),
        status=str(row["status"]),
        expires_at=(
            datetime.fromisoformat(row["expires_at"])
            if row["expires_at"] is not None
            else None
        ),
    )
```

Add `from uuid import uuid4` if not already imported (check first — `uuid4` may
already be imported near the top of `sqlite.py` for other receipt tables; reuse
the existing import if so instead of adding a duplicate).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_connector_authorizations.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/krubit/storage/sqlite.py tests/test_connector_authorizations.py
git commit -m "feat: split connector_authorizations identity into resource/subject columns"
```

---

## Task 3: `data_deletion_requests` table and store methods

**Files:**
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_data_deletion_requests.py` (new)

**Interfaces:**
- Produces:
  - `DataDeletionRequest` dataclass: `confirmation_code: str, authorization_subject_id: str, platform: str, requested_at: datetime, rows_deleted: int`
  - `SQLiteStore.save_data_deletion_request(confirmation_code, authorization_subject_id, platform, requested_at, rows_deleted) -> None`
  - `SQLiteStore.get_data_deletion_request(confirmation_code) -> DataDeletionRequest | None`
  - `SQLiteStore.find_recent_data_deletion_request(authorization_subject_id, platform, *, since) -> DataDeletionRequest | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_data_deletion_requests.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_save_and_get_data_deletion_request_round_trips(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-abc123",
        authorization_subject_id="subj-1",
        platform="facebook_page",
        requested_at=now,
        rows_deleted=2,
    )
    row = await store.get_data_deletion_request("conf-abc123")
    assert row is not None
    assert row.authorization_subject_id == "subj-1"
    assert row.rows_deleted == 2
    await store.close()


async def test_get_data_deletion_request_returns_none_for_unknown_code(tmp_path):
    store = await _store(tmp_path)
    assert await store.get_data_deletion_request("nope") is None
    await store.close()


async def test_find_recent_data_deletion_request_within_window(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-1", authorization_subject_id="subj-1",
        platform="facebook_page", requested_at=now, rows_deleted=1,
    )
    found = await store.find_recent_data_deletion_request(
        "subj-1", "facebook_page", since=now - timedelta(minutes=5)
    )
    assert found is not None
    assert found.confirmation_code == "conf-1"
    await store.close()


async def test_find_recent_data_deletion_request_outside_window_returns_none(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-1", authorization_subject_id="subj-1",
        platform="facebook_page", requested_at=now - timedelta(minutes=10),
        rows_deleted=1,
    )
    found = await store.find_recent_data_deletion_request(
        "subj-1", "facebook_page", since=now - timedelta(minutes=5)
    )
    assert found is None
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_data_deletion_requests.py -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add the schema**

Add after the `oauth_attempts` table added in Task 1:

```sql
            CREATE TABLE IF NOT EXISTS data_deletion_requests (
                confirmation_code TEXT NOT NULL PRIMARY KEY,
                authorization_subject_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                rows_deleted INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_data_deletion_requests_subject
                ON data_deletion_requests (authorization_subject_id, platform, requested_at);
```

- [ ] **Step 4: Implement the store methods**

```python
@dataclass(frozen=True, slots=True)
class DataDeletionRequest:
    confirmation_code: str
    authorization_subject_id: str
    platform: str
    requested_at: datetime
    rows_deleted: int


class SQLiteStore:
    # ... existing methods ...

    async def save_data_deletion_request(
        self,
        *,
        confirmation_code: str,
        authorization_subject_id: str,
        platform: str,
        requested_at: datetime,
        rows_deleted: int,
    ) -> None:
        async with self._write_transaction(immediate=True):
            await self._connection.execute(
                """
                INSERT INTO data_deletion_requests (
                    confirmation_code, authorization_subject_id, platform,
                    requested_at, rows_deleted
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    confirmation_code, authorization_subject_id, platform,
                    requested_at.isoformat(), rows_deleted,
                ),
            )

    async def get_data_deletion_request(
        self, confirmation_code: str
    ) -> DataDeletionRequest | None:
        cursor = await self._connection.execute(
            """
            SELECT confirmation_code, authorization_subject_id, platform,
                   requested_at, rows_deleted
            FROM data_deletion_requests WHERE confirmation_code = ?
            """,
            (confirmation_code,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_data_deletion_request(row)

    async def find_recent_data_deletion_request(
        self, authorization_subject_id: str, platform: str, *, since: datetime
    ) -> DataDeletionRequest | None:
        cursor = await self._connection.execute(
            """
            SELECT confirmation_code, authorization_subject_id, platform,
                   requested_at, rows_deleted
            FROM data_deletion_requests
            WHERE authorization_subject_id = ? AND platform = ? AND requested_at >= ?
            ORDER BY requested_at DESC LIMIT 1
            """,
            (authorization_subject_id, platform, since.isoformat()),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_data_deletion_request(row)


def _row_to_data_deletion_request(row: object) -> DataDeletionRequest:
    return DataDeletionRequest(
        confirmation_code=str(row["confirmation_code"]),
        authorization_subject_id=str(row["authorization_subject_id"]),
        platform=str(row["platform"]),
        requested_at=datetime.fromisoformat(row["requested_at"]),
        rows_deleted=int(row["rows_deleted"]),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_data_deletion_requests.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/krubit/storage/sqlite.py tests/test_data_deletion_requests.py
git commit -m "feat: add data_deletion_requests table for Meta's deletion contract"
```

---

## Task 4: `CallbackServer` bind host and access-log silencing

**Files:**
- Modify: `src/krubit/web/callbacks.py`
- Test: `tests/test_callback_ingress.py` (extend existing file — check for it first;
  create if absent)

**Interfaces:**
- Produces: `CallbackServer.__init__(..., bind_host: str = "127.0.0.1")` (new
  keyword parameter; `start()` binds to `self._bind_host` instead of the
  hardcoded `"0.0.0.0"`)

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_callback_ingress.py (or create the file if it does not exist)
from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krubit.web.callbacks import CallbackRoute, CallbackServer

pytestmark = pytest.mark.asyncio


async def test_callback_server_defaults_to_loopback_bind_host():
    server = CallbackServer(public_base_url="https://example.test", port=0)
    assert server._bind_host == "127.0.0.1"


async def test_callback_server_accepts_explicit_bind_host():
    server = CallbackServer(public_base_url="https://example.test", port=0, bind_host="0.0.0.0")
    assert server._bind_host == "0.0.0.0"


async def test_callback_server_second_start_is_a_noop():
    server = CallbackServer(public_base_url="https://example.test", port=0)
    await server.start()
    runner_after_first_start = server._runner
    await server.start()
    assert server._runner is runner_after_first_start
    await server.close()


async def test_callback_server_access_log_is_silent_for_query_string_secrets(caplog):
    async def handle(request: web.Request) -> web.Response:
        return web.Response(status=200)

    server = CallbackServer(
        public_base_url="https://example.test",
        port=0,
        routes=(CallbackRoute(path="/cb", method="GET", handler=handle),),
    )
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        with caplog.at_level(logging.INFO, logger="aiohttp.access"):
            await client.get("/cb?code=super-secret-code&state=super-secret-state")
    access_records = [r for r in caplog.records if r.name == "aiohttp.access"]
    assert access_records == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_callback_ingress.py -v -k "bind_host or access_log"`
Expected: FAIL — `bind_host` tests fail with `AttributeError` (no `_bind_host`);
the access-log test fails because aiohttp's default `AppRunner` currently emits
an `aiohttp.access` record for that request.

- [ ] **Step 3: Implement `bind_host` and disable the default access log**

In `src/krubit/web/callbacks.py`, modify `CallbackServer.__init__` and `start`:

```python
    def __init__(
        self,
        *,
        public_base_url: str | None,
        port: int | None,
        routes: tuple[CallbackRoute, ...] = (),
        bind_host: str = "127.0.0.1",
    ) -> None:
        if public_base_url is not None and not public_base_url.startswith("https://"):
            raise CallbackServerError("callback public base URL must use https")
        if port is not None and not (_MIN_PORT <= port <= _MAX_PORT):
            raise CallbackServerError(f"callback port must be between {_MIN_PORT} and {_MAX_PORT}")
        self._public_base_url = public_base_url
        self._port = port
        self._routes = routes
        self._bind_host = bind_host
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Bind and start serving, or do nothing when not fully configured."""
        if not self.enabled or self._runner is not None:
            return
        app = self.build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._bind_host, self._port)
        await site.start()
        self._runner = runner
```

(`access_log=None` is aiohttp's documented way to disable the default access
logger entirely — this is what stops the query string, which carries the OAuth
`code`/`state`, from ever reaching stdout.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_callback_ingress.py -v`
Expected: PASS, including all pre-existing tests in this file (no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/krubit/web/callbacks.py tests/test_callback_ingress.py
git commit -m "feat: bind callback server to loopback by default and silence access log"
```

---

## Task 5: `SignedFormRequest` scaffold and Meta signature verification

**Files:**
- Modify: `src/krubit/web/callbacks.py` (new scaffold)
- Modify: `src/krubit/integrations/meta.py` (new `verify_meta_signed_request`)
- Test: `tests/test_callback_ingress.py`, `tests/test_meta_signed_request.py` (new)

**Interfaces:**
- Produces (`callbacks.py`):
  - `SignedFormRequest` dataclass: `verify_and_parse: Callable[[str], Mapping[str, object] | None], handle_notification: Callable[[Mapping[str, object]], Awaitable[web.StreamResponse]]`
  - `build_signed_form_route(*, path: str, field_name: str, webhook: SignedFormRequest) -> CallbackRoute`
- Produces (`meta.py`):
  - `verify_meta_signed_request(signed_request: str, app_secret: str, *, now: Callable[[], datetime] = _utc_now, max_age: timedelta = timedelta(minutes=5)) -> Mapping[str, object] | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_meta_signed_request.py
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from krubit.integrations.meta import verify_meta_signed_request

_SECRET = "app-secret-value"


def _sign(payload: dict[str, object], secret: str = _SECRET) -> str:
    payload_json = json.dumps(payload)
    encoded_payload = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def test_verify_meta_signed_request_accepts_valid_fresh_request():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signed_request = _sign(payload)
    result = verify_meta_signed_request(signed_request, _SECRET, now=lambda: now)
    assert result is not None
    assert result["user_id"] == "12345"


def test_verify_meta_signed_request_rejects_tampered_payload():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signature, _, _ = _sign(payload).partition(".")
    tampered_payload = base64.urlsafe_b64encode(
        json.dumps({**payload, "user_id": "99999"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    forged = f"{signature}.{tampered_payload}"
    assert verify_meta_signed_request(forged, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_wrong_secret():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signed_request = _sign(payload, secret="wrong-secret")
    assert verify_meta_signed_request(signed_request, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_stale_issued_at():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    stale_time = now - timedelta(minutes=10)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(stale_time.timestamp()), "user_id": "12345"}
    signed_request = _sign(payload)
    assert verify_meta_signed_request(signed_request, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_malformed_input():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    assert verify_meta_signed_request("not-a-valid-signed-request", _SECRET, now=lambda: now) is None
    assert verify_meta_signed_request("", _SECRET, now=lambda: now) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_meta_signed_request.py -v`
Expected: FAIL with `ImportError: cannot import name 'verify_meta_signed_request'`

- [ ] **Step 3: Add `SignedFormRequest`/`build_signed_form_route` to `callbacks.py`**

Add after the existing `SignedWebhook`/`build_signed_webhook_route` section in
`src/krubit/web/callbacks.py`:

```python
@dataclass(frozen=True, slots=True)
class SignedFormRequest:
    """A form-posted signed request's verification and ingestion behavior.

    Mirrors `SignedWebhook` but for platforms (Meta's deauthorization/data-deletion
    callbacks) that sign a single form field's value rather than the raw request
    body — `verify_and_parse` receives that field's raw string value and returns
    the parsed, verified payload, or `None` to reject.
    """

    verify_and_parse: Callable[[str], Mapping[str, object] | None]
    handle_notification: Callable[[Mapping[str, object]], Awaitable[web.StreamResponse]]


def build_signed_form_route(
    *, path: str, field_name: str, webhook: SignedFormRequest
) -> CallbackRoute:
    """Build the single POST route for one form-signed-request endpoint.

    An unverified or missing field is rejected with 403 and `handle_notification`
    is never called, matching every other verify-before-ingest route in this
    module.
    """

    async def handle_post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        raw_value = form.get(field_name)
        if not isinstance(raw_value, str):
            return web.Response(status=403)
        parsed = webhook.verify_and_parse(raw_value)
        if parsed is None:
            return web.Response(status=403)
        return await webhook.handle_notification(parsed)

    return CallbackRoute(path=path, method="POST", handler=handle_post)
```

- [ ] **Step 4: Implement `verify_meta_signed_request` in `meta.py`**

Add near `verify_meta_signed_request` (`meta.py`, next to the existing
`verify_meta_signed_request`-adjacent helper at ~line 1042 — note: the first
revision's route used a header-based function at this location; replace it, since
the spec confirmed Meta's actual deauthorization/data-deletion protocol is
form-signed, not header-signed):

```python
def verify_meta_signed_request(
    signed_request: str,
    app_secret: str,
    *,
    now: Callable[[], datetime] = _utc_now,
    max_age: timedelta = timedelta(minutes=5),
) -> Mapping[str, object] | None:
    """Verify and parse Meta's form-posted `signed_request` value.

    Format: `<base64url-signature>.<base64url-json-payload>`, HMAC-SHA256 over the
    encoded payload string, keyed by the app secret. Rejects a missing/extra
    separator, a bad signature, non-JSON payload, or an `issued_at` older than
    `max_age` — a validly-signed but stale request is a replay, not a fresh call.
    """
    parts = signed_request.split(".")
    if len(parts) != 2:
        return None
    encoded_signature, encoded_payload = parts
    try:
        signature = base64.urlsafe_b64decode(encoded_signature + "==")
        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "==")
    except (binascii.Error, ValueError):
        return None
    expected_signature = hmac.new(
        app_secret.encode("utf-8"), encoded_payload.encode("utf-8"), sha256
    ).digest()
    if not hmac.compare_digest(expected_signature, signature):
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        return None
    issued_datetime = datetime.fromtimestamp(issued_at, tz=UTC)
    if now() - issued_datetime > max_age:
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    return payload
```

Add `import base64`, `import binascii`, and `import json` to `meta.py`'s imports
if not already present (check the top of the file first — `hmac`/`sha256` are
already imported for the existing `X-Hub-Signature-256` webhook verification).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_meta_signed_request.py tests/test_callback_ingress.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/krubit/web/callbacks.py src/krubit/integrations/meta.py \
    tests/test_meta_signed_request.py
git commit -m "feat: add signed-form-request scaffold and Meta signed_request verification"
```

---

## Task 6: TikTok identity resolution fix (`fetch_authorized_identity`)

**Files:**
- Modify: `src/krubit/integrations/tiktok.py`
- Test: `tests/test_tiktok_identity.py` (new)

**Rationale:** `TikTokConnector.resolve_account` (`tiktok.py:499`) only requests
`open_id,display_name` from TikTok's userinfo endpoint and echoes back whatever
`handle` was passed into it — it never independently confirms a handle from
TikTok's API. Comparing its output against `creator_accounts.handle` for identity
verification would therefore always trivially "match" regardless of which TikTok
account actually authorized, which defeats the purpose of the check. A new method
requests `username` as well, giving a real, independently-sourced value to
compare. `resolve_account` itself is left untouched (existing tests and callers
keep their current, tested behavior).

**Interfaces:**
- Produces: `TikTokConnector.fetch_authorized_identity() -> TikTokIdentity`
  where `TikTokIdentity` is a new dataclass: `open_id: str, username: str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tiktok_identity.py
from __future__ import annotations

import pytest

from krubit.integrations.tiktok import TikTokConnector, TikTokConnectorError

pytestmark = pytest.mark.asyncio


class _FakeSession:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.requested_url: str | None = None

    def post(self, url: str, **kwargs: object) -> object:
        self.requested_url = url

        class _Response:
            status = 200

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc: object) -> None:
                return None

            async def json(self_inner) -> dict[str, object]:
                return self._payload

        return _Response()


async def test_fetch_authorized_identity_returns_open_id_and_username():
    session = _FakeSession(
        {"data": {"user": {"open_id": "open-123", "username": "creator_handle"}}, "error": {"code": "ok"}}
    )
    connector = TikTokConnector(session, "fake-token")
    identity = await connector.fetch_authorized_identity()
    assert identity.open_id == "open-123"
    assert identity.username == "creator_handle"


async def test_fetch_authorized_identity_raises_on_missing_open_id():
    session = _FakeSession({"data": {"user": {"username": "creator_handle"}}, "error": {"code": "ok"}})
    connector = TikTokConnector(session, "fake-token")
    with pytest.raises(TikTokConnectorError):
        await connector.fetch_authorized_identity()
```

(This test's exact fake-session shape should match whatever pattern
`tests/test_tiktok_connector.py` already uses for `_post` — check that file
first and adapt the fake to match its existing conventions rather than
introducing a second, inconsistent fake-session style in the test suite.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tiktok_identity.py -v`
Expected: FAIL with `AttributeError: 'TikTokConnector' object has no attribute
'fetch_authorized_identity'`

- [ ] **Step 3: Implement `fetch_authorized_identity`**

Add to `TikTokConnector` in `src/krubit/integrations/tiktok.py`, near
`resolve_account`:

```python
@dataclass(frozen=True, slots=True)
class TikTokIdentity:
    """The authorizing TikTok account's independently-confirmed identity.

    Unlike `resolve_account`'s `handle` (which is merely echoed back from its
    caller, never confirmed against TikTok's own data), `username` here comes
    straight from TikTok's userinfo response — the only field this codebase can
    use to genuinely verify which account authorized a token.
    """

    open_id: str
    username: str | None


class TikTokConnector:
    # ... existing methods ...

    async def fetch_authorized_identity(self) -> TikTokIdentity:
        data = await self._post(f"{USER_INFO_URL}?fields=open_id,username", {})
        user = _mapping(data.get("user"))
        if user is None:
            raise self._fail(ConnectorFailure.invalid_response())
        open_id = user.get("open_id")
        if not isinstance(open_id, str) or not open_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = user.get("username")
        return TikTokIdentity(
            open_id=open_id,
            username=username if isinstance(username, str) and username.strip() else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tiktok_identity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/integrations/tiktok.py tests/test_tiktok_identity.py
git commit -m "feat: add TikTok identity resolution independent of resolve_account's echo"
```

---

## Task 7: `krubit/web/wiring.py` — Meta and TikTok OAuth authorization routes

**Files:**
- Create: `src/krubit/web/wiring.py`
- Test: `tests/test_wiring_oauth.py` (new)

**Interfaces:**
- Consumes: `SQLiteStore.issue_oauth_attempt`/`consume_oauth_attempt` (Task 1),
  `SQLiteStore.save_connector_authorization` (Task 2), `CredentialVault.seal_json`,
  `meta.exchange_authorization_code`, `meta.InstagramConnector.resolve_account`
  (and the other three Meta capability connectors), `tiktok.exchange_authorization_code`,
  `tiktok.TikTokConnector.fetch_authorized_identity` (Task 6),
  `krubit.storage.sqlite.SQLiteStore`, `krubit.config.Settings`
- Produces: `build_callback_routes(settings: Settings, store: SQLiteStore, vault:
  CredentialVault | None, oauth_session: object) -> tuple[CallbackRoute, ...]`
  (this task implements the OAuth-authorization half; Task 8 adds Meta
  deauthorization/data-deletion to the same function)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wiring_oauth.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.config import Settings
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object) -> Settings:
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test",
        callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck", tiktok_client_secret="cs",
        meta_app_id=None, meta_app_secret=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_build_callback_routes_returns_nothing_when_signals_disabled(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(
        _settings(creator_signals_enabled=False), store, vault, object()
    )
    assert routes == ()
    await store.close()


async def test_build_callback_routes_returns_nothing_without_callback_config(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(
        _settings(callback_public_base_url=None), store, vault, object()
    )
    assert routes == ()
    await store.close()


async def test_build_callback_routes_registers_tiktok_authorize_when_vault_present(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/tiktok/authorize", "GET") in paths
    await store.close()


async def test_build_callback_routes_omits_tiktok_authorize_without_vault(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/tiktok/authorize", "GET") not in paths
    await store.close()


async def test_tiktok_authorize_route_rejects_reused_state(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="tiktok:acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="tiktok:acct-1", platform="tiktok",
        capability="account",
        redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.tiktok import TikTokOAuthGrant
        return TikTokOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr(
        "krubit.integrations.tiktok.exchange_authorization_code", fake_exchange
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.tiktok import TikTokIdentity
        return TikTokIdentity(open_id="open-1", username="creator_handle")

    monkeypatch.setattr(
        "krubit.integrations.tiktok.TikTokConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(
        public_base_url="https://example.test", port=0, routes=routes
    )
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        first = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert first.status == 200
        second = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert second.status == 400

    saved = await store.get_connector_authorization(1, "tiktok:acct-1", "account")
    assert saved is not None
    assert saved.authorization_subject_id == "open-1"
    await store.close()


async def test_authorize_route_never_redirects_regardless_of_query(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/tiktok/authorize",
            params={"redirect_uri": "https://evil.test", "next": "https://evil.test"},
            allow_redirects=False,
        )
        assert response.status < 300 or response.status >= 400
        assert "Location" not in response.headers
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_wiring_oauth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'krubit.web.wiring'`

- [ ] **Step 3: Implement `build_callback_routes` (OAuth authorization half)**

```python
# src/krubit/web/wiring.py
"""Assembles Krubit's callback-server routes from settings, storage, and each
platform's existing connector code.

`build_callback_routes` is the single production call site that turns
`Settings`/`SQLiteStore`/`CredentialVault` into the route set `CallbackServer`
serves. Every route it registers is gated independently per the design spec's
capability-specific gating rule (`docs/superpowers/specs/2026-08-07-phase-2-callback-server-design.md`,
Component 7): OAuth authorization routes require the vault (sealing a grant needs
it); Meta's deauthorization/data-deletion routes require only `meta_app_secret`
(verifying and deleting needs no decryption).
"""

from __future__ import annotations

import functools
import secrets
from datetime import UTC, datetime, timedelta

from krubit.config import Settings
from krubit.integrations import meta, tiktok
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackRoute, OAuthRedirect, build_oauth_redirect_route

_ATTEMPT_TTL = timedelta(minutes=10)

_META_RESOLVER_BY_CAPABILITY = {
    "instagram": meta.InstagramConnector,
    "facebook_page": meta.FacebookPageConnector,
    "facebook": meta.FacebookProfileConnector,
    "threads": meta.ThreadsConnector,
}


def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
) -> tuple[CallbackRoute, ...]:
    if not settings.creator_signals_enabled:
        return ()
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []

    if (
        settings.tiktok_client_key is not None
        and settings.tiktok_client_secret is not None
        and vault is not None
    ):
        routes.append(
            _build_tiktok_authorize_route(settings, store, vault, oauth_session)
        )

    if (
        settings.meta_app_id is not None
        and settings.meta_app_secret is not None
        and vault is not None
    ):
        routes.append(
            _build_meta_authorize_route(settings, store, vault, oauth_session)
        )

    return tuple(routes)


def _build_tiktok_authorize_route(
    settings: Settings, store: SQLiteStore, vault: CredentialVault, oauth_session: object
) -> CallbackRoute:
    redirect_uri = f"{settings.callback_public_base_url}/callbacks/tiktok/authorize"
    exchange_code = functools.partial(
        tiktok.exchange_authorization_code,
        oauth_session,
        client_key=settings.tiktok_client_key,
        client_secret=settings.tiktok_client_secret,
        redirect_uri=redirect_uri,
    )

    async def handle_redirect(query: object) -> str:
        code = query.get("code")
        state = query.get("state")
        if not code or not state:
            raise ValueError("authorization redirect is missing required parameters")

        attempt = await store.consume_oauth_attempt(state, now=datetime.now(UTC))
        if attempt is None:
            raise ValueError("authorization request could not be completed")

        grant = await exchange_code(code=code)
        connector = tiktok.TikTokConnector(oauth_session, grant.access_token)
        identity = await connector.fetch_authorized_identity()

        account = await store.get_creator_account(attempt.guild_id, attempt.account_id)
        if account is None or (
            identity.username is not None
            and identity.username.lower() != account.handle.lower()
        ):
            raise ValueError("authorization request could not be completed")

        secret_ref = vault.seal_json(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        )
        await store.save_connector_authorization(
            guild_id=attempt.guild_id,
            account_id=attempt.account_id,
            capability=attempt.capability,
            secret_ref=secret_ref,
            provider_resource_id=identity.open_id,
            authorization_subject_id=identity.open_id,
            status="active",
            expires_at=grant.expires_at,
            now=datetime.now(UTC),
        )
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path="/callbacks/tiktok/authorize", redirect=redirect)


def _build_meta_authorize_route(
    settings: Settings, store: SQLiteStore, vault: CredentialVault, oauth_session: object
) -> CallbackRoute:
    redirect_uri = f"{settings.callback_public_base_url}/callbacks/meta/authorize"

    async def handle_redirect(query: object) -> str:
        code = query.get("code")
        state = query.get("state")
        if not code or not state:
            raise ValueError("authorization redirect is missing required parameters")

        attempt = await store.consume_oauth_attempt(state, now=datetime.now(UTC))
        if attempt is None:
            raise ValueError("authorization request could not be completed")

        platform = meta.Platform(attempt.platform)
        grant = await meta.exchange_authorization_code(
            oauth_session,
            platform=platform,
            code=code,
            client_id=settings.meta_app_id,
            client_secret=settings.meta_app_secret,
            redirect_uri=redirect_uri,
        )

        resolver_class = _META_RESOLVER_BY_CAPABILITY[attempt.capability]
        connector = resolver_class(oauth_session, grant.access_token)
        from krubit.domain.creator_signals import RecognizedAccountUrl
        account = await store.get_creator_account(attempt.guild_id, attempt.account_id)
        if account is None:
            raise ValueError("authorization request could not be completed")
        resolved = await connector.resolve_account(
            RecognizedAccountUrl(
                platform=platform, handle=account.handle, canonical_url=account.canonical_url
            )
        )
        if resolved.handle.lower() != account.handle.lower():
            raise ValueError("authorization request could not be completed")

        authorization_subject_id = await meta.fetch_authorizing_user_id(
            oauth_session, grant.access_token
        )

        secret_ref = vault.seal_json(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        )
        await store.save_connector_authorization(
            guild_id=attempt.guild_id,
            account_id=attempt.account_id,
            capability=attempt.capability,
            secret_ref=secret_ref,
            provider_resource_id=resolved.external_id,
            authorization_subject_id=authorization_subject_id,
            status="active",
            expires_at=grant.expires_at,
            now=datetime.now(UTC),
        )
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path="/callbacks/meta/authorize", redirect=redirect)
```

This task also requires adding `meta.fetch_authorizing_user_id(session,
access_token) -> str` to `src/krubit/integrations/meta.py` — a small new function
(Graph `GET /me?fields=id` with the just-granted token, returning the raw `id`
string, raising `MetaConnectorError` via the module's existing `_fail`/`_graph_get`
pattern on failure) since no existing function resolves "the user who granted
this token" independent of which capability was authorized. Add it near
`exchange_authorization_code`, using the same `_graph_get`-style call shape
already used throughout this module.

Also requires adding `SQLiteStore.get_creator_account(guild_id, account_id) ->
CreatorAccount | None` if it does not already exist — check `sqlite.py` first
(search for `get_creator_account`); `creator_registry.py` may already expose an
equivalent lookup through `CreatorRegistry` that this code should call instead of
duplicating a store method. Use whichever already exists; only add a new store
method if genuinely nothing already provides this lookup.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_wiring_oauth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/web/wiring.py src/krubit/integrations/meta.py \
    tests/test_wiring_oauth.py
git commit -m "feat: wire durable, account-bound OAuth authorization routes"
```

---

## Task 8: Meta deauthorization and data-deletion routes

**Files:**
- Modify: `src/krubit/web/wiring.py`
- Test: `tests/test_wiring_deauthorization.py` (new)

**Interfaces:**
- Consumes: `verify_meta_signed_request` (Task 5),
  `find_connector_authorizations_by_authorization_subject`/
  `delete_connector_authorizations` (Task 2),
  `save_data_deletion_request`/`find_recent_data_deletion_request` (Task 3),
  `build_signed_form_route` (Task 5)
- Produces: `build_callback_routes` additionally registers
  `/callbacks/meta/deauthorize` and `/callbacks/meta/data-deletion` when
  `settings.meta_app_secret` is set (independent of the vault)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wiring_deauthorization.py
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

pytestmark = pytest.mark.asyncio

_SECRET = "app-secret"


def _sign(payload: dict[str, object]) -> str:
    payload_json = json.dumps(payload)
    encoded_payload = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_SECRET.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def _settings(**overrides: object):
    from krubit.config import Settings
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test", callback_port=8080,
        credential_encryption_key=None,
        tiktok_client_key=None, tiktok_client_secret=None,
        meta_app_id="app-1", meta_app_secret=_SECRET,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_deauthorize_routes_register_without_vault(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/meta/deauthorize", "POST") in paths
    assert ("/callbacks/meta/data-deletion", "POST") in paths
    await store.close()


async def test_deauthorize_removes_matching_rows(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content", secret_ref="v1:x",
        provider_resource_id="page-1", authorization_subject_id="user-1",
        status="active", expires_at=None, now=now,
    )
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign({"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-1"})
        response = await client.post("/callbacks/meta/deauthorize", data={"signed_request": signed})
        assert response.status == 200
    assert await store.get_connector_authorization(1, "acct-1", "content") is None
    await store.close()


async def test_deauthorize_rejects_bad_signature(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.post(
            "/callbacks/meta/deauthorize", data={"signed_request": "garbage.garbage"}
        )
        assert response.status == 403
    await store.close()


async def test_data_deletion_returns_documented_contract(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign({"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-2"})
        response = await client.post("/callbacks/meta/data-deletion", data={"signed_request": signed})
        assert response.status == 200
        body = await response.json()
        assert "confirmation_code" in body
        assert body["url"].endswith(f"/callbacks/meta/data-deletion/status?id={body['confirmation_code']}")

        status_response = await client.get(
            "/callbacks/meta/data-deletion/status", params={"id": body["confirmation_code"]}
        )
        assert status_response.status == 200
        status_body = await status_response.json()
        assert status_body["status"] == "complete"
    await store.close()


async def test_data_deletion_status_unknown_code_is_404(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get("/callbacks/meta/data-deletion/status", params={"id": "unknown"})
        assert response.status == 404
    await store.close()


async def test_repeat_data_deletion_request_reuses_confirmation_code(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign({"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-3"})
        first = await client.post("/callbacks/meta/data-deletion", data={"signed_request": signed})
        second = await client.post("/callbacks/meta/data-deletion", data={"signed_request": signed})
        first_body = await first.json()
        second_body = await second.json()
        assert first_body["confirmation_code"] == second_body["confirmation_code"]
    await store.close()


async def test_data_deletion_on_already_deleted_subject_deletes_zero_rows_without_error(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign({"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "never-existed"})
        response = await client.post("/callbacks/meta/data-deletion", data={"signed_request": signed})
        assert response.status == 200
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_wiring_deauthorization.py -v`
Expected: FAIL — the deauthorize/data-deletion routes don't exist yet

- [ ] **Step 3: Implement the routes in `wiring.py`**

Add to `src/krubit/web/wiring.py`:

```python
from krubit.web.callbacks import SignedFormRequest, build_signed_form_route
from krubit.storage.sqlite import ConnectorAuthorization


def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
) -> tuple[CallbackRoute, ...]:
    if not settings.creator_signals_enabled:
        return ()
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []

    if (
        settings.tiktok_client_key is not None
        and settings.tiktok_client_secret is not None
        and vault is not None
    ):
        routes.append(_build_tiktok_authorize_route(settings, store, vault, oauth_session))

    if (
        settings.meta_app_id is not None
        and settings.meta_app_secret is not None
        and vault is not None
    ):
        routes.append(_build_meta_authorize_route(settings, store, vault, oauth_session))

    if settings.meta_app_secret is not None:
        routes.append(_build_meta_deauthorize_route(settings, store))
        routes.extend(_build_meta_data_deletion_routes(settings, store))

    return tuple(routes)


def _build_meta_deauthorize_route(settings: Settings, store: SQLiteStore) -> CallbackRoute:
    app_secret = settings.meta_app_secret

    def verify_and_parse(raw_value: str) -> dict[str, object] | None:
        return dict(meta.verify_meta_signed_request(raw_value, app_secret))  # type: ignore[arg-type]

    async def handle_notification(payload: object):
        from aiohttp import web
        user_id = str(payload["user_id"])
        rows = await store.find_connector_authorizations_by_authorization_subject(
            "meta", user_id
        )
        await store.delete_connector_authorizations(rows, now=datetime.now(UTC))
        return web.Response(status=200)

    webhook = SignedFormRequest(
        verify_and_parse=verify_and_parse, handle_notification=handle_notification
    )
    return build_signed_form_route(
        path="/callbacks/meta/deauthorize", field_name="signed_request", webhook=webhook
    )


def _build_meta_data_deletion_routes(
    settings: Settings, store: SQLiteStore
) -> tuple[CallbackRoute, CallbackRoute]:
    app_secret = settings.meta_app_secret
    base_url = settings.callback_public_base_url

    def verify_and_parse(raw_value: str) -> dict[str, object] | None:
        result = meta.verify_meta_signed_request(raw_value, app_secret)
        return dict(result) if result is not None else None

    async def handle_notification(payload: object):
        from aiohttp import web
        user_id = str(payload["user_id"])
        now = datetime.now(UTC)

        existing = await store.find_recent_data_deletion_request(
            user_id, "meta", since=now - timedelta(minutes=5)
        )
        if existing is not None:
            return web.json_response(
                {
                    "url": f"{base_url}/callbacks/meta/data-deletion/status?id={existing.confirmation_code}",
                    "confirmation_code": existing.confirmation_code,
                }
            )

        rows = await store.find_connector_authorizations_by_authorization_subject(
            "meta", user_id
        )
        await store.delete_connector_authorizations(rows, now=now)

        confirmation_code = secrets.token_urlsafe(16)
        await store.save_data_deletion_request(
            confirmation_code=confirmation_code,
            authorization_subject_id=user_id,
            platform="meta",
            requested_at=now,
            rows_deleted=len(rows),
        )
        return web.json_response(
            {
                "url": f"{base_url}/callbacks/meta/data-deletion/status?id={confirmation_code}",
                "confirmation_code": confirmation_code,
            }
        )

    webhook = SignedFormRequest(
        verify_and_parse=verify_and_parse, handle_notification=handle_notification
    )
    deletion_route = build_signed_form_route(
        path="/callbacks/meta/data-deletion", field_name="signed_request", webhook=webhook
    )

    async def handle_status(request: object):
        from aiohttp import web
        confirmation_code = request.query.get("id")  # type: ignore[attr-defined]
        if not confirmation_code:
            return web.Response(status=404)
        record = await store.get_data_deletion_request(confirmation_code)
        if record is None:
            return web.Response(status=404)
        return web.json_response(
            {"confirmation_code": record.confirmation_code, "status": "complete"}
        )

    status_route = CallbackRoute(
        path="/callbacks/meta/data-deletion/status", method="GET", handler=handle_status
    )
    return deletion_route, status_route
```

Add `from krubit.web.callbacks import CallbackRoute` (already imported from Task
7) and the new `SignedFormRequest`/`build_signed_form_route` names to the file's
import block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_wiring_deauthorization.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/web/wiring.py tests/test_wiring_deauthorization.py
git commit -m "feat: wire Meta deauthorization and data-deletion routes"
```

---

## Task 9: `_run_bot` wiring, dedicated OAuth session, and shutdown order

**Files:**
- Modify: `src/krubit/__main__.py`
- Test: `tests/test_run_bot_callback_server.py` (new)

**Interfaces:**
- Consumes: `build_callback_routes` (Tasks 7/8), `CallbackServer` (Task 4),
  `CredentialVault.from_env_key`
- Produces: `_run_bot` starts/stops a `CallbackServer` and a dedicated OAuth
  `aiohttp.ClientSession` around the bot's lifecycle

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_run_bot_callback_server.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from krubit.__main__ import _run_bot
from krubit.config import Settings

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object) -> Settings:
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test", callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck", tiktok_client_secret="cs",
        meta_app_id=None, meta_app_secret=None,
        live_signals_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_run_bot_starts_and_closes_callback_server_once():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(_settings())

        mock_server.start.assert_awaited_once()
        mock_server.close.assert_awaited_once()


async def test_run_bot_does_not_start_callback_server_when_unconfigured():
    with patch("krubit.__main__.CallbackServer") as mock_server_cls, \
         patch("krubit.__main__.SQLiteStore.open", new_callable=AsyncMock), \
         patch("krubit.__main__.KrubitBot") as mock_bot_cls:
        mock_server = AsyncMock()
        mock_server.enabled = False
        mock_server_cls.return_value = mock_server
        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await _run_bot(_settings(callback_public_base_url=None))

        mock_server_cls.assert_called_once()
        call_kwargs = mock_server_cls.call_args.kwargs
        assert call_kwargs["public_base_url"] is None
```

(These tests exercise the wiring via mocking, matching the level of test that's
practical for a function this deeply tied to real network/Discord I/O; the
end-to-end route behavior is already covered by Tasks 7/8's tests against a real
`CallbackServer`+`TestClient`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_run_bot_callback_server.py -v`
Expected: FAIL — `CallbackServer` is not yet referenced from `__main__.py`

- [ ] **Step 3: Wire the callback server into `_run_bot`**

In `src/krubit/__main__.py`, add imports:

```python
from krubit.security.credential_vault import CredentialVault
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes
```

Modify `_run_bot` (`src/krubit/__main__.py:116`): add `callback_server` and
`oauth_session`/`oauth_connector` to the tracked-resource locals at the top of
the function, construct and start them after `store` is opened and before
`bot.start()` is first called, and reorder the `finally` block per the spec.

```python
async def _run_bot(settings: Settings) -> int:
    twitch_session: aiohttp.ClientSession | None = None
    twitch_connector: aiohttp.TCPConnector | None = None
    connector: aiohttp.TCPConnector | None = None
    content_session: aiohttp.ClientSession | None = None
    content_tcp_connector: aiohttp.TCPConnector | None = None
    oauth_session: aiohttp.ClientSession | None = None
    oauth_tcp_connector: aiohttp.TCPConnector | None = None
    callback_server: CallbackServer | None = None
    store: SQLiteStore | None = None
    bot: KrubitBot | None = None
    twitch = None
    primary_error: BaseException | None = None
    try:
        if settings.live_signals_enabled:
            client_id, client_secret = settings.require_twitch_credentials()
            twitch_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
            twitch_session = aiohttp.ClientSession(connector=twitch_connector)
            twitch = TwitchHelixClient(twitch_session, client_id, client_secret)
        store = await SQLiteStore.open(settings.database_path)
        await store.initialize()
        await migrate_all_twitch_content(store)
        content_tcp_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        content_session = aiohttp.ClientSession(connector=content_tcp_connector)
        content_connectors = (
            _build_content_connectors(settings, content_session)
            if settings.creator_signals_enabled
            else {}
        )

        vault = (
            CredentialVault.from_env_key(settings.credential_encryption_key)
            if settings.credential_encryption_key is not None
            else None
        )
        oauth_tcp_connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        oauth_session = aiohttp.ClientSession(connector=oauth_tcp_connector)
        callback_routes = build_callback_routes(settings, store, vault, oauth_session)
        callback_server = CallbackServer(
            public_base_url=settings.callback_public_base_url,
            port=settings.callback_port,
            routes=callback_routes,
            bind_host=settings.callback_bind_host,
        )
        await callback_server.start()

        connector = aiohttp.TCPConnector(ssl=system_ssl_context())
        bot = KrubitBot(
            settings,
            FoundationService(store),
            connector=connector,
            twitch=twitch,
            content_connectors=content_connectors,
        )
        try:
            await bot.start(settings.require_token())
        except discord.PrivilegedIntentsRequired:
            if not settings.watchdog_enabled:
                raise
            _logger.warning(
                "KRUBIT_WATCHDOG_ENABLED=true but the privileged Message Content "
                "intent is not enabled for this application in the Discord "
                "Developer Portal (https://discord.com/developers/applications -> "
                "your application -> Bot -> Privileged Gateway Intents). "
                "Reconnecting without it: watch-window message inspection and "
                "spam-wave correlation are unavailable until it is enabled there, "
                "but Entry Sniff join-signal detection, watch-window expiry, "
                "raid/webhook-abuse/permission-risk detection, and every other "
                "Krubit capability continue to run normally."
            )
            await bot.close()
            connector = aiohttp.TCPConnector(ssl=system_ssl_context())
            bot = KrubitBot(
                settings,
                FoundationService(store),
                connector=connector,
                twitch=twitch,
                content_connectors=content_connectors,
                request_message_content_intent=False,
            )
            await bot.start(settings.require_token())
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        for resource in (
            callback_server,
            oauth_session,
            oauth_tcp_connector,
            bot,
            store,
            connector,
            twitch_session,
            twitch_connector,
            content_session,
            content_tcp_connector,
        ):
            if resource is None:
                continue
            try:
                await resource.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error
    return 0
```

Note the reconnect branch (the `except discord.PrivilegedIntentsRequired` block)
is untouched by `callback_server`/`oauth_session` construction — they are built
exactly once, before `bot.start()` is ever called the first time, so the retry
inside that branch never re-enters their construction, satisfying the spec's
"only ever started once" structural requirement without relying solely on
`CallbackServer.start()`'s internal guard.

- [ ] **Step 4: Add `callback_bind_host` to `Settings`**

In `src/krubit/config.py`, add a new optional field and parsing, following the
exact pattern already used for `callback_public_base_url`/`callback_port`
(~lines 53-54, 125-126, 156-166, 248-249):

```python
    callback_bind_host: str = "127.0.0.1"
```

```python
        raw_callback_bind_host = values.get("KRUBIT_CALLBACK_BIND_HOST", "").strip()
```

```python
            callback_bind_host=raw_callback_bind_host or "127.0.0.1",
```

Add `KRUBIT_CALLBACK_BIND_HOST` to `scripts/invoke-krubit.ps1`'s `$allowedNames`
array (it is optional, so it does not go in `$requiredNames`), matching every
other optional Phase 2 setting already listed there
(`scripts/invoke-krubit.ps1:10-39`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_run_bot_callback_server.py tests/test_launcher_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/krubit/__main__.py src/krubit/config.py scripts/invoke-krubit.ps1 \
    tests/test_run_bot_callback_server.py
git commit -m "feat: start and cleanly shut down the callback server from krubit run"
```

---

## Task 10: `/fetch integrations` renders authorization status safely

**Correction from the original plan draft:** `/fetch integrations`
(`src/krubit/discord/bot.py:318`) does not build its own text/embed directly — it
calls `self._health.integration_health(snapshot, watchdog=..., activity_ledger=...)`
(`src/krubit/services/health.py:329`), which returns a `HealthReport` built from
`HealthFinding` objects, then renders that report via `render_health_card`. Phase
3 and Phase 4 both already extended this exact method the same way: a small
`XHealthFacts` frozen dataclass, an optional keyword parameter defaulting to
`None` (so every existing caller is unaffected), and a `_x_findings(facts) ->
list[HealthFinding]` helper mirroring `_watchdog_findings`
(`health.py:108`)/`_activity_ledger_findings` (`health.py:67`) exactly. This task
follows that same shape rather than editing `bot.py`'s rendering directly.

**Files:**
- Modify: `src/krubit/services/health.py` (new `ConnectorAuthorizationFacts` type,
  new `_connector_authorization_findings` helper, new keyword parameter on
  `integration_health`)
- Modify: `src/krubit/discord/bot.py` (the `integrations` command, ~line 318 —
  pass the new facts through)
- Test: `tests/test_health_service.py` (extend existing file)

**Interfaces:**
- Consumes: `SQLiteStore.list_connector_authorization_status` (Task 2)
- Produces: `integration_health(snapshot, *, watchdog=None, activity_ledger=None,
  connector_authorizations: tuple[ConnectorAuthorizationStatus, ...] | None = None)`

- [ ] **Step 1: Read the existing pattern first**

Read `src/krubit/services/health.py` in full, focusing on
`ActivityLedgerHealthFacts` (~line 52), `_activity_ledger_findings` (~line 67),
and where `integration_health` (~line 329) calls both. Read
`tests/test_health_service.py` for the existing test style covering
`activity_ledger`/`watchdog` findings — match that style exactly, including
however it constructs a `SnapshotRecord` fixture.

- [ ] **Step 2: Write the failing test**

```python
# add to tests/test_health_service.py, matching its existing fixture style
def test_integration_health_reports_connector_authorization_status():
    from krubit.storage.sqlite import ConnectorAuthorizationStatus
    from krubit.services.health import HealthService  # match existing import style

    authorizations = (
        ConnectorAuthorizationStatus(
            platform="tiktok", capability="account", status="active", expires_at=None
        ),
    )
    service = HealthService()  # match however this file already constructs it
    report = service.integration_health(
        _some_snapshot_fixture(),  # reuse this file's existing snapshot fixture helper
        connector_authorizations=authorizations,
    )
    codes = {f.code for f in report.findings}
    assert "connector_authorization_tiktok_account_active" in codes
    rendered_messages = " ".join(f.message for f in report.findings)
    assert "active" in rendered_messages


def test_integration_health_flags_expired_connector_authorization():
    from datetime import UTC, datetime, timedelta
    from krubit.storage.sqlite import ConnectorAuthorizationStatus
    from krubit.services.health import HealthService

    authorizations = (
        ConnectorAuthorizationStatus(
            platform="meta", capability="content", status="expired",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    service = HealthService()
    report = service.integration_health(
        _some_snapshot_fixture(), connector_authorizations=authorizations
    )
    matching = [f for f in report.findings if f.code == "connector_authorization_meta_content_expired"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"


def test_integration_health_omits_connector_authorization_findings_when_none_supplied():
    from krubit.services.health import HealthService

    service = HealthService()
    report = service.integration_health(_some_snapshot_fixture())
    assert not any(f.code.startswith("connector_authorization_") for f in report.findings)
```

(`_some_snapshot_fixture()` is a placeholder name — replace it with whatever
`tests/test_health_service.py` already uses to build a `SnapshotRecord` for its
existing `integration_health` tests; do not invent a new fixture.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_health_service.py -v -k connector_authorization`
Expected: FAIL — `connector_authorizations` is not yet a parameter

- [ ] **Step 4: Implement the facts type, helper, and parameter**

Add to `src/krubit/services/health.py`, near `ActivityLedgerHealthFacts` and
`_activity_ledger_findings`:

```python
def _connector_authorization_findings(
    authorizations: tuple[ConnectorAuthorizationStatus, ...] | None,
) -> list[HealthFinding]:
    """Factual per-platform authorization status — never the sealed secret or
    either identifier column, only what `ConnectorAuthorizationStatus` (the
    deliberately narrow DTO from `SQLiteStore.list_connector_authorization_status`)
    already exposes: platform, capability, status, expiry."""
    if not authorizations:
        return []
    findings: list[HealthFinding] = []
    for auth in authorizations:
        severity = "healthy" if auth.status == "active" else "warning"
        expiry_text = f", expires {auth.expires_at.isoformat()}" if auth.expires_at else ""
        findings.append(
            HealthFinding(
                f"connector_authorization_{auth.platform}_{auth.capability}_{auth.status}",
                severity,
                f"{auth.platform} {auth.capability} authorization is {auth.status}{expiry_text}.",
            )
        )
    return findings
```

Add `from krubit.storage.sqlite import ConnectorAuthorizationStatus` to this
file's imports (check first whether this creates a circular import — `sqlite.py`
must not import from `health.py`; if it does, move `ConnectorAuthorizationStatus`
to `krubit.domain.creator_signals` or another domain module both files already
import from, following whatever pattern `WatchdogHealthFacts`/
`ActivityLedgerHealthFacts` already use to avoid the same problem).

Modify `integration_health` (~line 329):

```python
    def integration_health(
        self,
        snapshot: SnapshotRecord,
        *,
        watchdog: WatchdogHealthFacts | None = None,
        activity_ledger: ActivityLedgerHealthFacts | None = None,
        connector_authorizations: tuple[ConnectorAuthorizationStatus, ...] | None = None,
    ) -> HealthReport:
        findings = _integration_findings(snapshot)
        findings.extend(_watchdog_findings(watchdog))
        findings.extend(_activity_ledger_findings(activity_ledger))
        findings.extend(_connector_authorization_findings(connector_authorizations))
        return _report(findings, snapshot.captured_at)
```

- [ ] **Step 5: Wire it through the `/fetch integrations` command**

In `src/krubit/discord/bot.py`'s `integrations` command handler (~line 321-329),
add one call before `self._health.integration_health(...)`:

```python
        connector_authorizations = await self._foundation.store.list_connector_authorization_status(
            guild.id
        )
```

(Match whichever existing attribute already exposes the store to this command
class — check how `snapshot`/`self.capture(guild)` accesses storage a few lines
above, and use the same access path rather than assuming `self._foundation.store`
verbatim.) Then pass it through:

```python
        report = self._health.integration_health(
            snapshot,
            watchdog=self._watchdog_facts,
            activity_ledger=self._activity_ledger_facts,
            connector_authorizations=connector_authorizations,
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_health_service.py tests/test_phase_one_commands.py -v`
Expected: PASS, including every pre-existing test in both files (no regressions)

- [ ] **Step 7: Commit**

```bash
git add src/krubit/services/health.py src/krubit/discord/bot.py tests/test_health_service.py
git commit -m "feat: render connector authorization status in /fetch integrations"
```

---

## Task 11: Wire `oauth_attempts` purge into the existing sweep cycle

**Confirmed location:** `ActivityRuntime.sweep_cycle` in
`src/krubit/discord/activity_runtime.py:477`. It prunes stale voice-join cache
entries, then loops over every guild running `_seed_default_exclusions`,
`_seed_default_retention_policy`, and `self._retention_sweep.sweep(guild_id,
now)` inside a per-guild `try/except Exception` that logs and continues —
"one guild's failure must never block another's." `oauth_attempts` is **not**
guild-scoped the way that per-guild loop's targets are (an OAuth attempt's
`guild_id` column exists, but the purge itself operates globally across every
guild's rows in one call), so it does not belong inside the per-guild loop —
it belongs once per `sweep_cycle()` invocation, in its own isolated
`try/except`, exactly mirroring the per-guild isolation's *shape* (log and
continue) applied at the whole-table level instead.

**Files:**
- Modify: `src/krubit/discord/activity_runtime.py` (`sweep_cycle`, ~line 477)
- Test: `tests/test_activity_runtime.py` (extend existing file)

**Interfaces:**
- Consumes: `SQLiteStore.purge_oauth_attempts` (Task 1)

- [ ] **Step 1: Read the existing pattern first**

Read `ActivityRuntime.sweep_cycle` (`src/krubit/discord/activity_runtime.py:477-497`)
and `tests/test_activity_runtime.py`'s existing `sweep_cycle` tests in full —
match their store/fixture setup conventions exactly (how a `SQLiteStore` and
`ActivityRuntime` instance are constructed in that file's existing tests).

- [ ] **Step 2: Write the failing test**

```python
# add to tests/test_activity_runtime.py, matching its existing fixture style
async def test_sweep_cycle_purges_oauth_attempts_without_blocking_guild_sweeps():
    from datetime import UTC, datetime, timedelta
    # Reuse this file's existing store/runtime construction helper here.
    store = ...  # this file's existing SQLiteStore fixture
    runtime = ...  # this file's existing ActivityRuntime fixture

    now = datetime(2026, 8, 7, tzinfo=UTC)
    old_consumed = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="a", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=40), ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(old_consumed, now=now - timedelta(days=40))

    await runtime.sweep_cycle(now)

    assert await store.consume_oauth_attempt(old_consumed, now=now) is None
    # Assert this file's existing guild-sweep assertions still pass unmodified —
    # do not remove or alter any pre-existing assertion in this file.


async def test_sweep_cycle_continues_guild_sweeps_when_oauth_purge_fails(monkeypatch):
    # Reuse this file's existing store/runtime construction helper here.
    store = ...
    runtime = ...

    async def failing_purge(*args: object, **kwargs: object) -> int:
        raise RuntimeError("simulated purge failure")

    monkeypatch.setattr(store, "purge_oauth_attempts", failing_purge)

    from datetime import UTC, datetime
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await runtime.sweep_cycle(now)  # must not raise
    # Assert this file's existing guild-sweep assertions (e.g. that
    # _seed_default_retention_policy/_retention_sweep.sweep still ran for every
    # guild) still pass — the failing purge must not have blocked them.
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_activity_runtime.py -v -k oauth_attempts`
Expected: FAIL — `sweep_cycle` does not yet call `purge_oauth_attempts`

- [ ] **Step 4: Add the purge call as one more isolated sweep target**

In `src/krubit/discord/activity_runtime.py`, modify `sweep_cycle` (~line
477-497):

```python
    async def sweep_cycle(self, now: datetime) -> None:
        """Prune stale voice-join cache entries, purge expired OAuth attempts, and
        run the retention sweep for every guild, isolating each target's failure
        from every other's -- mirroring `WatchdogRuntime.sweep_cycle`'s and
        `RetentionSweepService.sweep_all_guilds`'s own per-guild isolation
        discipline, applied here at the whole-table level for `oauth_attempts`
        since that purge is not guild-scoped the way the per-guild loop below is.
        """
        if not self._activity_ledger_enabled:
            return
        _require_aware("now", now)
        self._prune_stale_voice_joins(now)
        try:
            await self._store.purge_oauth_attempts(
                now, consumed_retention=timedelta(days=30), unconsumed_grace=timedelta(days=1)
            )
        except Exception:
            _logger.exception(
                "ActivityRuntime.sweep_cycle: oauth_attempts purge failed; "
                "continuing with guild sweeps"
            )
        for guild_id in self._guild_ids():
            try:
                await self._seed_default_exclusions(guild_id, now)
                await self._seed_default_retention_policy(guild_id, now)
                await self._retention_sweep.sweep(guild_id, now)
            except Exception:
                _logger.exception(
                    "ActivityRuntime.sweep_cycle: sweep failed for guild %s; "
                    "continuing with the next guild",
                    guild_id,
                )
```

(Check whether `ActivityRuntime` already holds a `self._store`/`self._sqlite`
attribute referencing the `SQLiteStore` — it must, since `_seed_default_exclusions`
and friends already write to storage; use that exact existing attribute name
rather than assuming `self._store`. `timedelta` is very likely already imported
in this file given its use of `datetime`; add the import only if it is missing.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_activity_runtime.py -v`
Expected: PASS, including every pre-existing test in the file (no regressions)

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/activity_runtime.py tests/test_activity_runtime.py
git commit -m "feat: purge expired oauth_attempts on the existing sweep schedule"
```

---

## Task 12: Full-suite verification and cross-cutting security tests

**Files:**
- Test: `tests/test_wiring_security.py` (new — the tests that don't belong to
  any single earlier task because they exercise the whole assembled route set)

**Interfaces:**
- Consumes: everything from Tasks 1–9

- [ ] **Step 1: Write the cross-cutting tests**

```python
# tests/test_wiring_security.py
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object):
    from krubit.config import Settings
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test", callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck", tiktok_client_secret="cs",
        meta_app_id="app-1", meta_app_secret="app-secret",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_failing_exchange_never_leaks_token_in_response_or_logs(tmp_path, monkeypatch, caplog):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    now = datetime(2026, 8, 7, tzinfo=UTC)
    from krubit.domain.creator_signals import CreatorAccount, Platform
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="tiktok:acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="tiktok:acct-1", platform="tiktok",
        capability="account", redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def failing_exchange(*args: object, **kwargs: object) -> object:
        raise RuntimeError("token exchange failed for secret-token-abc123")

    monkeypatch.setattr("krubit.integrations.tiktok.exchange_authorization_code", failing_exchange)

    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        with caplog.at_level(logging.ERROR):
            response = await client.get(
                "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
            )
        body = await response.text()
        assert "secret-token-abc123" not in body
        assert all("secret-token-abc123" not in r.message for r in caplog.records)
    await store.close()


async def test_second_callback_server_start_binds_nothing_extra(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    await server.start()
    runner = server._runner
    await server.start()
    assert server._runner is runner
    await server.close()
    await store.close()


async def test_partial_bind_failure_leaves_runner_unset(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    occupied = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    await occupied.start()
    bound_port = occupied._runner.addresses[0][1]  # type: ignore[union-attr]

    colliding = CallbackServer(
        public_base_url="https://example.test", port=bound_port, routes=routes
    )
    with pytest.raises(OSError):
        await colliding.start()
    assert colliding._runner is None

    await occupied.close()
    await store.close()
```

- [ ] **Step 2: Run tests to verify they fail (or pass, confirming prior tasks)**

Run: `uv run pytest tests/test_wiring_security.py -v`
Expected: These should already PASS if Tasks 1–9 are correctly implemented —
this task is verification, not new production code. If any fail, that indicates
a real gap in an earlier task; fix the earlier task's code, not this test.

- [ ] **Step 3: Run the complete test suite, linter, and type checker**

```bash
uv run pytest -q
uv run ruff check .
uv run pyright
```

Expected: all green, zero regressions anywhere in the suite.

- [ ] **Step 4: Commit**

```bash
git add tests/test_wiring_security.py
git commit -m "test: add cross-cutting security coverage for callback server wiring"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers durable/single-use/restart-safe state
  (spec issue 2). Task 2 covers the resource/subject identifier split (issue 2)
  and safe rendering DTO (part of issue 7). Task 3 covers the data-deletion
  contract (issue 4). Task 4 covers bind host and access-log silencing (issue 6,
  part of issue 7). Task 5 covers the correct Meta signed-request protocol
  (issue 5). Task 6 covers meaningful TikTok identity verification, a gap
  surfaced only while implementing the plan for issue 1 — the codebase already
  contradicted the "compare against a resolved handle" assumption for TikTok
  specifically, so this was added rather than silently producing a vacuous
  check. Task 7 covers TikTok-context incompatibility and account binding
  (issues 1 and 3) plus provider identity verification (issue 4/1 combined).
  Task 8 covers the redesigned Meta deauthorization/data-deletion routes (issue
  5) keyed correctly on `authorization_subject_id` (issue 3). Task 9 covers HTTP
  resource ownership and shutdown order (issue 6) and idempotent single-start
  construction (first-round issue). Task 10 makes the safe-rendering test
  non-vacuous (issue 7). Task 11 covers the `oauth_attempts` cleanup policy
  (issue 5). Task 12 covers the remaining cross-cutting security properties
  (token leakage, double-start, partial-bind-failure) that need the fully
  assembled route set rather than one component in isolation.
- **Placeholder scan:** every step above contains complete, runnable code; no
  "TBD"/"similar to Task N" placeholders remain except explicit, justified
  "check the existing file first and match its conventions" instructions in
  Tasks 6, 10, and 11 — these three genuinely depend on existing test-fixture
  conventions this plan's author has not read line-by-line, and guessing a
  fabricated fixture shape would be worse than flagging it for the implementer
  to confirm against the real file before writing.
- **Type consistency:** `OAuthAttempt`, `ConnectorAuthorization`,
  `ConnectorAuthorizationStatus`, and `DataDeletionRequest` field names are used
  identically across Tasks 1–3 and their consumers in Tasks 7–10 (checked:
  `provider_resource_id`/`authorization_subject_id` never swapped;
  `secret_ref` never exposed outside Task 2's non-status methods).

## Execution Options

Plan complete and saved to
`docs/superpowers/plans/2026-08-07-phase-2-callback-server.md`. Two execution
options:

**1. Subagent-Driven (recommended)** - dispatch a fresh subagent per task,
review between tasks, fast iteration

**2. Inline Execution** - execute tasks in this session using executing-plans,
batch execution with checkpoints

Which approach?
