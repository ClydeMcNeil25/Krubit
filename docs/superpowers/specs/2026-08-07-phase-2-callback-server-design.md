# Krubit Phase 2 Callback Server Design

**Date:** 2026-08-07 (revised same day after review)
**Status:** Draft — revised per review, pending re-approval
**Scope:** Start the OAuth/push callback server from `krubit run`, wire Meta's and
TikTok's OAuth authorization and Meta's deauthorization/data-deletion routes into
it, and add the storage those routes need to persist a connector authorization
safely and revocably — closing Phase 2's second documented production gap ("the
OAuth/push callback server is never started by `krubit run`").

## Revision Note

This spec was reviewed before implementation began (no code was written against the
first version). Seven blocking issues were found and are addressed below: TikTok's
callback context incompatibility, non-durable OAuth state, missing account binding,
missing provider-identity verification, an incorrect Meta deauthorization protocol,
incomplete HTTP resource ownership, and access-log token leakage. Confirmed against
current code: `creator_accounts.external_id` (`sqlite.py:432`) already stores each
account's resolved platform identifier, which provider-identity verification checks
against. Confirmed against Meta's current documentation (fetched 2026-08-07): the
data-deletion/deauthorization callback is a form-posted `signed_request` parameter
(base64, HMAC-SHA256 over a JSON payload) — a different protocol from the
`X-Hub-Signature-256` header the first draft assumed, which is Instagram/Facebook
Graph API *content* webhooks' protocol, not this one.

## Purpose

`CallbackServer` (`src/krubit/web/callbacks.py`) and `CredentialVault`
(`src/krubit/security/credential_vault.py`) are already implemented and tested in
isolation, but nothing in the running process ever constructs a `CallbackServer`,
registers routes on it, or calls `start()`/`close()`. The `connector_authorizations`
table exists in the schema but has no store methods. This spec closes both gaps for
Meta and TikTok, with the OAuth flow redesigned around a durable, account-bound
attempt record rather than the original stateless-signed-token approach.

## Explicit Exclusions

- **YouTube WebSub push** — separate, later work; YouTube uploads are already
  detected today via polling. Confirmed with the user 2026-08-07.
- **Generating the "click to authorize" link** a creator clicks — this spec builds
  the link-issuing call site (see "Initiating an authorization," below) only to the
  extent needed to produce a valid, account-bound attempt record; the user-facing
  command that surfaces the link (e.g. `/fetch creator authorize`) is separate
  follow-up work, tracked as Phase 2 gap #1.
- **Per-account content polling** using a saved grant — separate follow-up (gap #1).
- **Generalized event scheduling** — assigned to Phase 4, not touched here.

## Architecture

```text
/fetch creator authorize <account>  (future command, gap #1)
        |
issue_oauth_attempt(guild_id, member_id, account_id, platform, capability)
        |
   durable oauth_attempts row (SQLite) --------------------+
        |                                                  |
authorize URL (redirect_uri is the ONE static,              |
platform-registered callback path, no query params)          |
        |                                                  |
   creator's browser -> platform login -> redirect          |
        |                                                  |
GET /callbacks/{platform}/authorize?code=...&state=...      |
        |                                                  |
   consume_oauth_attempt(state) [atomic, durable] ----------+
        |
   exchange_code(code, redirect_uri=<same static URI>)
        |
   verify provider identity == creator_accounts.external_id
        |
   seal grant + provider metadata -> connector_authorizations
```

```text
krubit run (_run_bot)
        |
build_callback_routes(settings, store, vault, oauth_session)
        |            |
   Meta routes    TikTok route
        |            |
        +----- CallbackServer(routes=..., bind_host=...) -----+
                        |
              started once, before bot.start()
              closed first in shutdown, before sessions/store
```

## Components

### 1. Durable OAuth attempt record (new, replaces in-memory `OAuthStates`)

New table `oauth_attempts`:

```sql
CREATE TABLE IF NOT EXISTS oauth_attempts (
    state_hash TEXT NOT NULL PRIMARY KEY,   -- sha256(opaque random state token)
    guild_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    capability TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY (guild_id, account_id) REFERENCES creator_accounts (guild_id, account_id)
);
```

- `issue_oauth_attempt(guild_id, member_id, account_id, platform, capability,
  redirect_uri, ttl)` generates a cryptographically random state token (`secrets.
  token_urlsafe(32)`), stores only its SHA-256 hash plus the full context, and
  returns the plaintext token (embedded in the authorize URL's `state` parameter,
  never stored in plaintext). The row **is** the source of truth — no HMAC signing
  key is needed at all, which removes the original design's reuse of
  `credential_encryption_key` as a second-purpose signing key.
- `consume_oauth_attempt(state_token)` hashes the presented token, then performs one
  atomic `UPDATE oauth_attempts SET consumed_at = ? WHERE state_hash = ? AND
  consumed_at IS NULL AND expires_at > ?` and checks `rowcount == 1` inside the
  existing single-connection SQLite discipline this codebase already uses for
  idempotent writes. A second consume, a consume after expiry, or a consume for an
  unknown token are all indistinguishable failures (returns `None`), matching the
  original design's "one uninformative outcome" CSRF-defense principle — but now
  durable across a restart, since the row (not an in-process set) is what prevents
  reuse.
- `MetaOAuthStates`/`TikTokOAuthStates` (the in-memory HMAC classes) are retired
  from the production wiring path. They stay in the codebase only if existing tests
  still reference them directly; new code uses `oauth_attempts` exclusively.

### 2. TikTok and Meta redirect URIs carry no per-request context

Per TikTok's Login Kit documentation (static, pre-registered redirect URI; no
custom query parameters permitted) and to keep Meta's redirect handling identical
in shape: the registered callback path for each platform (e.g.
`/callbacks/tiktok/authorize`, `/callbacks/meta/authorize`) is fixed and receives
only what the platform itself appends — `code`, `state`, and the platform's error
parameters. **All** context (`guild_id`, `member_id`, `account_id`, `platform`,
`capability`) travels via the `oauth_attempts` row looked up by `state`, never via
the URL. `exchange_code` is called with the identical `redirect_uri` string that
was used to build the authorize URL in the first place — platforms reject a token
exchange whose `redirect_uri` doesn't match byte-for-byte, so this value is stored
in the `oauth_attempts` row (not reconstructed) to guarantee the match.

### 3. Account binding

Because `account_id` now comes from the consumed `oauth_attempts` row (not the
query string), `on_authorized(guild_id, member_id, account_id, platform,
capability, grant, provider_subject_id)` always knows exactly which registered
`creator_accounts` row the token belongs to — including when one member owns
multiple accounts on the same platform.

### 4. Provider identity verification

After `exchange_code` succeeds, before any grant is sealed or saved, the route
handler calls the platform's identity endpoint (Meta: `GET /me` on the Graph API
with the new access token; TikTok: the userinfo endpoint) to resolve
`provider_subject_id`. This is compared against `creator_accounts.external_id` for
`(guild_id, account_id)`. A mismatch means the authorizing browser logged into a
*different* account than the one Krubit's `/fetch creator add` originally
resolved — the grant is discarded, nothing is saved, and the response is the same
generic failure text as any other rejected authorization (no detail on *why*, to
avoid leaking which check failed). `connector_authorizations` gains a
`provider_subject_id` column (indexed) alongside the existing columns, storing this
verified, non-secret identifier next to the sealed token — needed for deauthorization
lookup (below) and to make a future audit/export capable of showing *which* account
was authorized without unsealing anything.

### 5. `connector_authorizations` store methods (new)

- `save_connector_authorization(guild_id, account_id, capability, secret_ref,
  provider_subject_id, status, expires_at)` — upserts one row. `secret_ref` is
  always `CredentialVault.seal_json(...)` output.
- `get_connector_authorization(guild_id, account_id, capability)` — returns the row
  or `None`.
- `find_connector_authorizations_by_provider_subject(platform, provider_subject_id)`
  — new lookup, since Meta's deauthorization/data-deletion payload identifies the
  account only by the platform's own user id, never by `guild_id`/`account_id`.
  Returns every matching row (a provider subject could in principle map to more
  than one guild's registration).
- `delete_connector_authorizations(rows)` — deletes a batch of matched rows inside
  one transaction, and writes one redacted receipt per deletion to the existing
  `creator_registry_receipts` table (action `"connector_deauthorized"`, detail JSON
  containing only `platform`/`capability`/`account_id` — never the token or
  `provider_subject_id`).
- `list_connector_authorization_status(guild_id) -> tuple[ConnectorAuthorizationStatus,
  ...]` — a new, deliberately narrow DTO (`platform`, `capability`, `status`,
  `expires_at` only — no `secret_ref`, no `provider_subject_id`) for anything that
  renders authorization state to staff. `/fetch integrations` is extended to call
  this and show "authorized / expired / not authorized" per platform, which is what
  makes the "safe rendering" security test non-vacuous (see Testing, below) —
  otherwise there is no rendering call site to test against at all.

### 6. Meta deauthorization and data-deletion routes (redesigned)

Meta's Deauthorize Callback URL and Data Deletion Request URL are two separately
configured endpoints in the Meta App Dashboard, but both use the same protocol: a
POST containing a `signed_request` form field — base64url-encoded
`<signature>.<payload>`, where `payload` is a JSON object (`algorithm`, `issued_at`,
`user_id`) and `signature` is HMAC-SHA256 over the raw payload string, keyed by the
app secret. This is a **different** verification shape than
`build_signed_webhook_route`'s header-based model, so a new scaffold is added to
`krubit/web/callbacks.py`:

```python
@dataclass(frozen=True, slots=True)
class SignedFormRequest:
    verify_and_parse: Callable[[str], Mapping[str, object] | None]
    handle_notification: Callable[[Mapping[str, object]], Awaitable[str]]

def build_signed_form_route(*, path: str, field_name: str, webhook: SignedFormRequest) -> CallbackRoute:
    ...  # reads `field_name` from the POST form body; verify_and_parse returning
         # None -> 403 before handle_notification ever runs, matching every other
         # verify-before-ingest route in this module; the returned string is the
         # response body Meta expects (Deauthorize: none required; Data Deletion:
         # a confirmation URL + confirmation_code JSON body per Meta's contract).
```

`verify_meta_signed_request(signed_request: str, app_secret: str) ->
Mapping[str, object] | None` implements the split/decode/HMAC-compare/parse
sequence. Both routes (`/callbacks/meta/deauthorize` and
`/callbacks/meta/data-deletion`) use it; their `handle_notification` both resolve
`user_id` from the parsed payload, call
`find_connector_authorizations_by_provider_subject(Platform.META-ish-values,
user_id)` (Meta's `user_id` here is app-scoped per app, not per-product, so this
matches across Instagram/Facebook/Threads registrations that share the same Meta
app), and call `delete_connector_authorizations` transactionally. Deletion, not a
status flip — a revoked-but-retained sealed token is a needless retained secret
once the platform says to forget the account.

### 7. HTTP resource ownership and shutdown order

A dedicated `aiohttp.ClientSession` (its own `TCPConnector`) is constructed in
`_run_bot` specifically for OAuth code-exchange and provider-identity calls —
**not** the existing `content_session` used for polling connectors, since the two
have different lifetimes and failure domains (a stuck OAuth exchange must never
block or be blocked by content polling). `build_callback_routes` receives this
session and threads it into `exchange_authorization_code`/identity-lookup calls via
`functools.partial`, matching the existing pattern in `meta.py`/`tiktok.py`'s own
docstrings.

Shutdown order in `_run_bot`'s `finally` block is reordered to:
1. `await callback_server.close()` — stop accepting new callback requests first
2. `await oauth_session.close()` (and its connector) — no in-flight OAuth exchange
   depends on anything closed after this
3. everything else as today (`bot`, `store`, `connector`, `twitch_session`,
   `content_session`, ...) — `store` last, since every resource above may still
   need to write a receipt or authorization row during its own cleanup

### 8. Bind host

`CallbackServer` gains a `bind_host: str = "127.0.0.1"` constructor parameter
(currently hardcoded `"0.0.0.0"`). A new optional setting,
`KRUBIT_CALLBACK_BIND_HOST`, overrides this for deployments that terminate TLS at
the process itself rather than behind a reverse proxy; when unset, the server binds
loopback-only, matching the expectation (already implied by `CallbackServer`
enforcing an `https://` *public* base URL while speaking plain HTTP locally) that a
reverse proxy sits in front and terminates TLS.

## Security Properties and Tests

- **State single-use and durable across restart:** consuming a state twice fails;
  consuming after simulated restart (fresh `SQLiteStore` handle, same database
  file) still correctly rejects reuse and still correctly rejects an expired
  attempt, because the row — not process memory — is authoritative. A concurrent
  double-consume test (two simultaneous `consume_oauth_attempt` calls for the same
  token) asserts exactly one succeeds, exercising SQLite's transactional guarantee
  directly rather than trusting it by inspection.
- **Redirect URI integrity:** a test asserts `exchange_code` is always invoked with
  the exact `redirect_uri` string stored on the consumed `oauth_attempts` row, and
  a second test asserts every registered route's response is never a 3xx / never
  carries a `Location` header, regardless of query-parameter content (closing the
  open-redirect surface by construction, not by runtime check).
- **Account binding correctness:** a guild with two accounts on the same platform;
  issuing two attempts and consuming them (in either order) each land the grant on
  the correct `account_id` — never swapped, never both landing on one row.
- **Provider identity mismatch is rejected:** a fake `exchange_code` succeeds but
  the fake identity lookup returns a `provider_subject_id` that does not match
  `creator_accounts.external_id`; assert nothing is written to
  `connector_authorizations` and the response is the same generic failure text as
  any other rejected attempt.
- **Meta signed-request verification:** a valid `signed_request` (correct HMAC) is
  accepted; a tampered payload, a wrong-algorithm payload, and a request signed
  with the wrong app secret are all rejected with 403 before
  `handle_notification`/deletion logic ever runs.
- **Deauthorization/data-deletion removes exactly the matched rows:** save
  authorizations for two different guilds sharing one `provider_subject_id`-style
  fixture; a deletion request removes both (Meta's `user_id` is app-scoped, not
  guild-scoped) and writes one redacted receipt per row, containing no token and no
  `provider_subject_id`.
- **Tokens never leak — response, logs, or access logs:** a failing `exchange_code`
  whose exception message embeds a fake token string produces a response body and
  application log record containing neither the token nor the exception's `str()`.
  Separately, `CallbackServer` is constructed with `access_log=None` on its
  `web.AppRunner`, and a test drives a request whose query string carries a
  fake `code`/`state` value and asserts the `aiohttp.access` logger receives zero
  records for that request (aiohttp's default access logger otherwise writes the
  full request line, including the query string, to stdout).
- **Safe rendering:** `/fetch integrations` output for a guild with a stored
  authorization is asserted to contain only platform/capability/status/expiry
  substrings — never the sealed `secret_ref` value, never `provider_subject_id` —
  driven through the actual command handler now that it calls
  `list_connector_authorization_status`, not through a store method in isolation.
- **Idempotent startup:** a second `CallbackServer.start()` call on an
  already-started instance is a no-op (`_runner` identity unchanged, no second bind
  attempt) — both a direct unit test and a structural fact about `_run_bot` (the
  `PrivilegedIntentsRequired` reconnect branch never re-enters callback-server
  construction/start at all, since it is constructed and started once, before that
  branch exists).
- **Partial-bind-failure cleanup:** binding the callback server to an already-
  occupied port raises during `start()`; assert this does not leave a runner
  half-registered (`_runner` stays `None`) and that `_run_bot`'s cleanup still runs
  cleanly for every other resource when this happens early in startup.

## Testing and Rollout

Automated tests must cover every property above, plus: `oauth_attempts` and
`connector_authorizations` store methods in isolation (round-trip, atomic
consumption, sealed-value unreadable without the correct vault key,
provider-subject lookup returning zero/one/many rows correctly);
`build_callback_routes` returning the right route set for every combination of
configured/unconfigured Meta and TikTok credentials; and that `_run_bot` starts and
cleanly closes the callback server and its dedicated OAuth session exactly once per
process lifetime, in the revised shutdown order, including across the
`PrivilegedIntentsRequired` reconnect path and a simulated partial-bind failure.

## Completion Gate

This spec is complete only when: `krubit run` starts a callback server bound to
`127.0.0.1` (or the configured bind host) whenever configured; an OAuth attempt is
durable across a process restart and single-use under concurrency; a completed
authorization is bound to the exact account it was issued for and its provider
identity is verified against `creator_accounts.external_id` before anything is
saved; Meta deauthorization and data-deletion requests are verified via the correct
`signed_request` protocol and remove every matching row transactionally with a
redacted receipt; no token or query-string secret reaches an HTTP response, an
application log, or the `aiohttp.access` log; `/fetch integrations` can safely
render authorization status without exposing `secret_ref`; and every property above
is verified by an automated test, not just asserted by design.
