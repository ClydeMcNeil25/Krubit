# Krubit Phase 2 Callback Server Design

**Date:** 2026-08-07 (revised twice same day after review)
**Status:** Approved for implementation planning
**Scope:** Start the OAuth/push callback server from `krubit run`, wire Meta's and
TikTok's OAuth authorization and Meta's deauthorization/data-deletion routes into
it, and add the storage those routes need to persist a connector authorization
safely and revocably — closing Phase 2's second documented production gap ("the
OAuth/push callback server is never started by `krubit run`").

## Revision Note

This spec was reviewed twice before implementation began (no code was written
against either version). The first round found seven blocking issues: TikTok's
callback context incompatibility, non-durable OAuth state, missing account binding,
missing provider-identity verification, an incorrect Meta deauthorization protocol,
incomplete HTTP resource ownership, and access-log token leakage. Confirmed against
Meta's current documentation (fetched 2026-08-07): the data-deletion/deauthorization
callback is a form-posted `signed_request` parameter (base64, HMAC-SHA256 over a
JSON payload) — a different protocol from the `X-Hub-Signature-256` header the first
draft assumed, which is Instagram/Facebook Graph API *content* webhooks' protocol,
not this one.

A second round found that the first revision's identity model was still wrong.
Confirmed against current code: `creator_accounts.external_id` is **not** a
platform-resolved stable identifier — `CreatorRegistry.add_account`
(`services/creator_registry.py:101`) receives `resolved_external_id` from its
caller, and the only production call site, `content_commands.py:213`, passes
`recognized.handle` (the URL-parsed handle string) as that argument. Every
connector defines a `resolve_account` method that *would* produce a true stable id
(e.g. `InstagramConnector.resolve_account` at `meta.py:537` calls Graph `/me` and
returns a numeric `id`; `TikTokConnector.resolve_account` at `tiktok.py:499`
resolves `open_id`), but none of these are ever called from `add_account` — so
`external_id` holds a handle, not a resource id, for every existing account today.
Comparing a freshly-resolved provider id against `external_id` would therefore
reject valid authorizations. This revision fixes that by resolving identity fresh
at authorization time and comparing against `creator_accounts.handle` (the field
that is actually trustworthy today), and by splitting "the resource being
monitored" from "the user who granted access" into two separate columns, since for
Facebook Pages, Instagram Business accounts, and Threads these are genuinely
different ids.

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
- **Facebook Page / Facebook Profile OAuth authorization** — the authorize route
  rejects every attempt for these two platforms before exchanging the code.
  Neither has a reliable Graph-resolved identity to bind against: Page
  authorization would need a proper `/me/accounts` Page-token exchange (never
  implemented) to resolve a genuine Page identity, and a personal Profile's
  `/me` has no comparable field at all. Instagram, Threads, and TikTok
  authorization are unaffected. Deauthorization/data-deletion for these two
  platforms still works, so any pre-existing rows can still be cleaned up.
  Deliberate, documented gap — revisit if/when a proper Page-token exchange is
  implemented.

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
   resolve_authorized_identity(access_token)  [capability-specific,
   reuses each connector's existing resolve_account logic]
        |
   resolved handle == creator_accounts.handle ?  (reject on mismatch)
        |
   seal grant + provider_resource_id + authorization_subject_id
   -> connector_authorizations
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
capability, grant, provider_resource_id, authorization_subject_id)` always knows
exactly which registered `creator_accounts` row the token belongs to — including
when one member owns multiple accounts on the same platform.

### 4. Provider identity verification (capability-specific, resolved fresh)

There is no generic identity check. Each connector capability already implements
`resolve_account`, which — given an access token — calls the correct
capability-specific endpoint and returns a `ConnectorAccount(external_id, handle,
...)`:

| Capability          | Existing resolver                              | Resolved id            |
|----------------------|------------------------------------------------|-------------------------|
| Instagram Business   | `InstagramConnector.resolve_account` (`meta.py:537`) | Graph `/me` numeric id |
| Facebook Page        | `FacebookPageConnector.resolve_account` (`meta.py:650`) | Page id |
| Facebook Profile     | `FacebookProfileConnector.resolve_account` (`meta.py:793`) | Graph `/me` numeric id |
| Threads               | `ThreadsConnector.resolve_account` (`meta.py:902`) | Threads user id |
| TikTok                | `TikTokConnector.resolve_account` (`tiktok.py:499`) | `open_id` |

The authorization route handler constructs the capability-appropriate connector
with the *freshly exchanged* access token and calls its existing `resolve_account`
— reusing exactly the code path `/fetch creator add`'s baseline check already
exercises, rather than inventing a second, parallel identity-resolution mechanism.
This directly satisfies "a platform-specific resolver contract, not a generic
`/me`," using code that already exists and is already tested per capability.

The result gives two distinct identifiers, stored as two distinct columns (see
below), because they answer different questions:

- **`provider_resource_id`** — `resolve_account`'s returned `external_id`: the
  resource being monitored (an Instagram Business account, a Facebook Page, a
  Threads profile, a TikTok account). This is what a future poller uses to know
  *what* to fetch.
- **`authorization_subject_id`** — the Meta/TikTok **user** who completed the OAuth
  grant. For a Facebook Profile or a TikTok account these are the same id as
  `provider_resource_id`. For an Instagram Business account or a Facebook Page they
  are **not** — the authorizing human and the Page/IG account they administer are
  different ids. Resolving this is a second, always-present call: Meta's Graph
  `/me` with the just-granted user access token (independent of which capability
  was authorized, since every Meta OAuth grant starts as a user-level token before
  a Page/IG-scoped token is derived from it); TikTok's `authorization_subject_id`
  equals its `provider_resource_id`, since TikTok's OAuth is inherently
  user-scoped. This is the id Meta's deauthorization/data-deletion payload actually
  contains, so it is the id deletion lookups must key on — using
  `provider_resource_id` for that lookup would silently fail to find Page/IG rows
  whose resource id differs from the authorizing user's id.

**Identity check:** the resolved `handle` from `resolve_account` is compared,
case-normalized, against `creator_accounts.handle` for `(guild_id, account_id)` —
**not** against `external_id`, which (per the Revision Note) is not a trustworthy
resolved id in the current codebase. A mismatch means the authorizing browser
logged into a different account than the one `/fetch creator add` originally
registered by handle; the grant is discarded, nothing is saved, and the response is
the same generic failure text as any other rejected authorization.

### 5. `connector_authorizations` and `oauth_attempts` store methods (new)

`connector_authorizations` gains two columns replacing the single
`provider_subject_id` the first revision proposed: `provider_resource_id` and
`authorization_subject_id` (both indexed; `authorization_subject_id` is the one
deauthorization lookups use).

- `save_connector_authorization(guild_id, account_id, capability, secret_ref,
  provider_resource_id, authorization_subject_id, status, expires_at)` — upserts
  one row. `secret_ref` is always `CredentialVault.seal_json(...)` output.
- `get_connector_authorization(guild_id, account_id, capability)` — returns the row
  or `None`.
- `find_connector_authorizations_by_authorization_subject(platform,
  authorization_subject_id)` — the lookup Meta's deauthorization/data-deletion
  payload's `user_id` actually resolves through. Returns every matching row (one
  Meta user can administer Pages/IG accounts registered by more than one guild).
- `delete_connector_authorizations(rows)` — deletes a batch of matched rows inside
  one transaction, and writes one redacted receipt per deletion to the existing
  `creator_registry_receipts` table (action `"connector_deauthorized"`, detail JSON
  containing only `platform`/`capability`/`account_id` — never the token or either
  identifier).
- `list_connector_authorization_status(guild_id) -> tuple[ConnectorAuthorizationStatus,
  ...]` — a deliberately narrow DTO (`platform`, `capability`, `status`,
  `expires_at` only — no `secret_ref`, no identifiers) for anything that renders
  authorization state to staff. `/fetch integrations` is extended to call this and
  show "authorized / expired / not authorized" per platform, which is what makes
  the "safe rendering" security test non-vacuous — otherwise there is no rendering
  call site to test against at all.
- `purge_oauth_attempts(now, *, consumed_retention: timedelta,
  unconsumed_grace: timedelta)` — deletes rows where either (a) `consumed_at` is
  set and older than `consumed_retention` (default 30 days — long enough to
  investigate a disputed authorization, short enough not to accumulate forever), or
  (b) `consumed_at` is unset and `expires_at` is older than `now - unconsumed_grace`
  (default 1 day past expiry). Wired into the existing Phase 4 `sweep_cycle`
  isolation pattern (one table's purge failure never blocks another's) as one more
  sweep target, run on the same schedule as the activity-ledger retention sweep —
  not a new scheduler.

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
    handle_notification: Callable[[Mapping[str, object]], Awaitable[web.StreamResponse]]

def build_signed_form_route(*, path: str, field_name: str, webhook: SignedFormRequest) -> CallbackRoute:
    ...  # reads `field_name` from the POST form body; verify_and_parse returning
         # None -> 403 before handle_notification ever runs, matching every other
         # verify-before-ingest route in this module.
```

`verify_meta_signed_request(signed_request: str, app_secret: str) ->
Mapping[str, object] | None` implements the split/decode/HMAC-compare/parse
sequence, and additionally rejects an `issued_at` older than a bounded window
(5 minutes) to reject a replayed-but-validly-signed old request.

**`/callbacks/meta/deauthorize`:** `handle_notification` resolves `user_id`,
calls `find_connector_authorizations_by_authorization_subject(...)`, deletes the
matches transactionally, and returns Meta's expected empty `200`. No response body
content is required by Meta's contract for this endpoint.

**`/callbacks/meta/data-deletion`:** a distinct, precisely specified contract,
since Meta requires a specific JSON response and supports the user checking status
later:

- On a valid signed request, deletion runs immediately (deletion is naturally
  idempotent — a repeat request for an already-deleted `authorization_subject_id`
  deletes zero rows, not an error).
- A `confirmation_code` is generated (`secrets.token_urlsafe(16)`) and persisted in
  a new `data_deletion_requests` table (`confirmation_code TEXT PRIMARY KEY,
  authorization_subject_id TEXT NOT NULL, platform TEXT NOT NULL, requested_at TEXT
  NOT NULL, rows_deleted INTEGER NOT NULL`) — **not** keyed by guild, since Meta's
  status-check request carries only the confirmation code.
- The response body is exactly `{"url": "<callback_public_base_url>/callbacks/meta/data-deletion/status?id=<confirmation_code>", "confirmation_code": "<confirmation_code>"}`,
  matching Meta's documented shape.
- **Replay:** if `handle_notification` receives a second valid signed request for
  the same `authorization_subject_id` within a short window (the same 5-minute
  window `verify_meta_signed_request` already bounds `issued_at` to), it reuses the
  existing pending `data_deletion_requests` row's `confirmation_code` rather than
  minting a new one — Meta's own retry behavior on a slow response must not produce
  two different confirmation codes for what is really one request.
- **Status route:** `GET /callbacks/meta/data-deletion/status?id=<code>` (new,
  unauthenticated by design — it exists so Meta's own systems and the user can
  check status with only the confirmation code, matching the pattern Meta's docs
  describe) looks up the row and returns `{"confirmation_code": ..., "status":
  "complete"}` for a known code, `404` for an unknown one. Nothing is deleted or
  re-deleted here — this route only reads `data_deletion_requests`.
- **Malformed/expired signed_request:** any parse failure, HMAC mismatch, or
  `issued_at` outside the freshness window is rejected with 403 before any
  deletion, confirmation-code generation, or lookup ever runs.

### 7. Route gating is capability-specific, not vault-gated as a whole

The first revision gated every callback route behind `vault is not None`. That's
wrong for deauthorization/data-deletion: those routes only need to *verify a
signature* and *delete rows by an indexed column* — no decryption, so no vault
dependency. Route registration in `build_callback_routes` is split accordingly:

- **OAuth authorization routes** (`/callbacks/meta/authorize`,
  `/callbacks/tiktok/authorize`) require: `creator_signals_enabled`, the callback
  server's public base URL/port, the platform's app credentials, **and** the vault
  (since sealing the grant needs it). Absent any of these, that platform's
  authorization route is not registered.
- **Meta deauthorization and data-deletion routes** require only:
  `creator_signals_enabled`, the callback server's public base URL/port, and
  `settings.meta_app_secret` (to verify `signed_request`). They register
  independently of whether `credential_encryption_key`/the vault is configured —
  Krubit must be able to honor a deletion request even if the encryption key was
  never set or is temporarily unavailable, since refusing to process a legally
  required data-deletion request because of an unrelated missing setting would be
  the wrong failure mode.

### 8. HTTP resource ownership and shutdown order

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

### 9. Bind host

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
  the fake `resolve_account` call returns a handle that does not match
  `creator_accounts.handle`; assert nothing is written to
  `connector_authorizations` and the response is the same generic failure text as
  any other rejected attempt. A separate test asserts a Facebook Page authorization
  correctly stores a `provider_resource_id` (the Page id) distinct from
  `authorization_subject_id` (the administering user's id) — not the same value
  written to both columns by accident.
- **Meta signed-request verification:** a valid `signed_request` (correct HMAC,
  fresh `issued_at`) is accepted; a tampered payload, a wrong-algorithm payload, a
  request signed with the wrong app secret, and a validly-signed but stale
  `issued_at` (outside the freshness window) are all rejected with 403 before
  `handle_notification`/deletion logic ever runs.
- **Deauthorization removes exactly the matched rows, keyed correctly:** save a
  Facebook Page authorization and an Instagram authorization administered by the
  same person (same `authorization_subject_id`, different `provider_resource_id`,
  possibly different guilds); a deauthorization request for that
  `authorization_subject_id` removes both rows and writes one redacted receipt per
  row containing no token and neither identifier. A second test proves the lookup
  is genuinely keyed by `authorization_subject_id` and not `provider_resource_id`
  by constructing a fixture where using the wrong column would find zero rows.
- **Data-deletion contract:** a valid request returns the documented
  `{"url": ..., "confirmation_code": ...}` body and persists a
  `data_deletion_requests` row; the status route returns `"complete"` for that
  code and `404` for an unknown one; a second valid signed request for the same
  `authorization_subject_id` within the freshness window reuses the same
  `confirmation_code` rather than minting a new one; deleting an
  already-deleted (or never-existing) `authorization_subject_id` deletes zero rows
  without error.
- **`oauth_attempts` purge is bounded and safe:** a consumed row younger than
  `consumed_retention` survives a purge call; one older is removed; an unconsumed,
  unexpired row always survives regardless of age; an unconsumed row past
  `expires_at + unconsumed_grace` is removed. A purge failure for this table (fault
  injection) does not prevent the activity-ledger retention sweep, or any other
  sweep target, from completing — exercising the existing per-target isolation
  Phase 4 established.
- **Route gating is capability-specific:** with `credential_encryption_key` unset
  (no vault) but `meta_app_secret` set, `build_callback_routes` registers Meta's
  deauthorization and data-deletion routes but **not** the Meta or TikTok
  authorization routes. With the vault present but `meta_app_secret` unset, no Meta
  routes register at all (not even deauthorization, which needs the secret to
  verify signatures).
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
  substrings — never the sealed `secret_ref` value, never `provider_resource_id` or
  `authorization_subject_id` — driven through the actual command handler now that
  it calls `list_connector_authorization_status`, not through a store method in
  isolation.
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

Automated tests must cover every property above, plus: `oauth_attempts`,
`connector_authorizations`, and `data_deletion_requests` store methods in isolation
(round-trip, atomic consumption, sealed-value unreadable without the correct vault
key, `authorization_subject_id` lookup returning zero/one/many rows correctly);
`resolve_account`-based identity resolution exercised for all five capabilities
(Instagram, Facebook Page, Facebook Profile, Threads, TikTok), each with a matching
and a mismatching fixture; `build_callback_routes` returning the right route set
for every combination of configured/unconfigured Meta app id/secret, TikTok
credentials, and vault presence (per the capability-specific gating in Component
7); and that `_run_bot` starts and cleanly closes the callback server and its
dedicated OAuth session exactly once per process lifetime, in the revised shutdown
order, including across the `PrivilegedIntentsRequired` reconnect path and a
simulated partial-bind failure.

## Completion Gate

This spec is complete only when: `krubit run` starts a callback server bound to
`127.0.0.1` (or the configured bind host) whenever configured; an OAuth attempt is
durable across a process restart and single-use under concurrency; a completed
authorization is bound to the exact account it was issued for and its identity is
verified fresh, per capability, against `creator_accounts.handle` before anything
is saved; `provider_resource_id` and `authorization_subject_id` are stored as
distinct columns and deauthorization lookups key on the latter; Meta
deauthorization and data-deletion requests are verified via the correct
`signed_request` protocol (including freshness), data-deletion responses match
Meta's documented contract and are replay-safe; expired/consumed `oauth_attempts`
rows are purged on a bounded retention schedule without disrupting other sweep
targets; route registration is gated per-capability so a missing vault never blocks
Meta's deletion obligations from being honored; no token or query-string secret
reaches an HTTP response, an application log, or the `aiohttp.access` log; `/fetch
integrations` can safely render authorization status without exposing `secret_ref`
or either identifier; and every property above is verified by an automated test,
not just asserted by design.
