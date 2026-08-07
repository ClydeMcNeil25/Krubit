# Krubit Phase 2 Callback Server Design

**Date:** 2026-08-07
**Status:** Approved for implementation planning
**Scope:** Start the OAuth/push callback server from `krubit run`, wire Meta's and
TikTok's OAuth authorization and Meta's deauthorization routes into it, and add the
storage those routes need to actually persist a connector authorization — closing
Phase 2's second documented production gap ("the OAuth/push callback server is never
started by `krubit run`").

## Purpose

`CallbackServer` (`src/krubit/web/callbacks.py`), `CredentialVault`
(`src/krubit/security/credential_vault.py`), and each platform's route builders
(`build_meta_oauth_redirect_route`, `build_meta_deauthorization_route`,
`build_tiktok_oauth_redirect_route`) are already implemented and tested in isolation,
but nothing in the running process ever constructs a `CallbackServer`, registers
those routes on it, or calls `start()`/`close()`. Separately, the
`connector_authorizations` table exists in the schema but has no store methods, so
even a correctly wired `on_authorized` callback has nowhere to persist the grant it
receives. This spec closes both gaps for Meta and TikTok.

## Explicit Exclusions

- **YouTube WebSub push** (subscribing to Google's PubSubHubbub hub for real-time
  upload notification) is a separate, later piece of work. YouTube uploads are
  already detected today via `YouTubeConnector`'s existing `playlistItems.list`
  polling — WebSub is a latency improvement, not a detection gap. Confirmed with the
  user 2026-08-07.
- **Generating the "click to authorize" link** a creator actually clicks
  (`MetaOAuthStates.issue()` / `TikTokOAuthStates.issue()`) is not built here — no
  call site exists anywhere in the codebase today. This spec only wires the
  *receiving* half of the OAuth flow. Issuing the link belongs to the per-account
  credential resolution work (Phase 2's first documented production gap) and needs
  its own design (likely a `/fetch creator authorize` or similar command).
- **Per-account content polling** using a saved grant (Phase 2's first documented
  production gap) is not built here. This spec only makes the grant land safely in
  storage; consuming it from the polling scheduler is separate follow-up work.
- **Generalized event scheduling** (previously discussed) is assigned to Phase 4, not
  touched here.

## Architecture

```text
krubit run (_run_bot)
        |
build_callback_routes(settings, store, vault)
        |            |
   Meta routes    TikTok route
   (if configured)  (if configured)
        |            |
        +----- CallbackServer(routes=...) -----+
                        |
              started once, before bot.start()
              closed once, in the existing
              finally-block cleanup
```

`build_callback_routes` is a pure function of `Settings`, the store, and a
`CredentialVault` — it returns `()` when `creator_signals_enabled` is false or the
callback server isn't configured (`callback_public_base_url`/`callback_port` unset),
matching every other Phase 2 flag-gating pattern in this codebase. Each platform is
independently optional: missing Meta credentials means no Meta routes are
registered, never a startup failure; same for TikTok.

## Components

### 1. `connector_authorizations` store methods (new, in `sqlite.py`)

- `save_connector_authorization(guild_id, account_id, capability, secret_ref, status,
  expires_at)` — upserts one row (matches the table's
  `PRIMARY KEY (guild_id, account_id, capability)`). `secret_ref` is always a
  `CredentialVault.seal_json(...)` output — the plaintext grant never reaches this
  method's caller's caller (the route handler owns the vault call, the store method
  only ever sees an opaque sealed string).
- `get_connector_authorization(guild_id, account_id, capability)` — returns the
  stored row (including the sealed `secret_ref`) or `None`. Callers wanting the
  plaintext grant must separately call `vault.open_json(secret_ref)`.
- `delete_connector_authorization(guild_id, account_id, capability)` — deletes the
  row outright. Used by the deauthorization webhook (see below). Delete, not a
  status flip to `revoked`, because retaining a sealed-but-revoked token is a
  needless retained secret once the platform has told Krubit to forget the account —
  matching Meta's own data-deletion callback's intent.

### 2. `krubit/web/wiring.py` (new module)

`build_callback_routes(settings: Settings, store: SQLiteStore, vault: CredentialVault
| None) -> tuple[CallbackRoute, ...]`:

- Returns `()` immediately if `not settings.creator_signals_enabled` or
  `settings.callback_public_base_url is None` or `settings.callback_port is None` or
  `vault is None`.
- If `settings.meta_app_id`/`settings.meta_app_secret` are both set: builds one
  `MetaOAuthStates` (signing key derived from `settings.credential_encryption_key` —
  reusing the same operator-supplied secret rather than requiring a second one),
  binds `exchange_authorization_code` via `functools.partial` with a real
  `aiohttp.ClientSession` and the app credentials, and defines `on_authorized` to
  seal the grant (`vault.seal_json`) and call `save_connector_authorization`.
  Registers both `build_meta_oauth_redirect_route` (path:
  `/callbacks/meta/authorize`) and `build_meta_deauthorization_route` (path:
  `/callbacks/meta/deauthorize`), the latter's `handle_deauthorization` calling
  `delete_connector_authorization` for the account Meta identifies in the signed
  payload.
- If `settings.tiktok_client_key`/`settings.tiktok_client_secret` are both set:
  same shape, one route (`build_tiktok_oauth_redirect_route`, path
  `/callbacks/tiktok/authorize`) — TikTok has no deauthorization webhook requirement
  in this codebase's connector.
- Every path is an exact literal registered once via `CallbackServer`/aiohttp's
  exact-match router (no prefix or wildcard routes anywhere in this module) — closing
  off any open-redirect or route-confusion surface by construction, not by runtime
  check.

### 3. `_run_bot` wiring (`__main__.py`)

- Construct `CredentialVault.from_env_key(settings.credential_encryption_key)` once,
  only when the key is set (`None` otherwise — matches every other optional-flag
  pattern already in `_run_bot`).
- Call `build_callback_routes(settings, store, vault)` once, construct
  `CallbackServer(public_base_url=settings.callback_public_base_url,
  port=settings.callback_port, routes=...)` once, and `await
  callback_server.start()` once — all **before** entering the
  `bot.start()`/`PrivilegedIntentsRequired` reconnect logic, and never inside that
  reconnect branch. This is what makes "only ever started once" structural rather
  than dependent on `CallbackServer.start()`'s existing internal guard (which stays
  as a second, defense-in-depth safety net, verified by its own test).
- Add `callback_server` to the existing `finally` block's cleanup tuple
  (`await callback_server.close()`), matching how every other resource in `_run_bot`
  is already cleaned up.

## Security Properties and Tests

- **State single-use and expiring:** exercised through the wired route, not just the
  `MetaOAuthStates`/`TikTokOAuthStates` classes in isolation — hit the real
  `/callbacks/.../authorize` route twice with the same `state`; second call fails.
  Hit it after the TTL has elapsed (fake clock); fails.
- **No attacker-controlled redirect:** every response from every registered route,
  across an exhaustive set of query-parameter payloads (including ones shaped like
  `redirect_uri=`, `next=`, `return_to=`), is asserted to never carry a 3xx status or
  a `Location` header. `handle_redirect` returns only a static confirmation body by
  construction, so this test is a structural proof, not a fuzzing exercise.
  `CallbackServer.__init__` rejecting a non-`https` `public_base_url` is already
  covered by existing tests; add one asserting `build_callback_routes` never runs
  when that URL is absent.
- **Tokens never leak:** drive a failing `exchange_code` whose raised exception
  message embeds a fake token string through the real route; assert the HTTP
  response body and the emitted log record contain neither the token substring nor
  the exception's `str()` — exercising `_redacted_errors_middleware` end-to-end
  through this feature, not just its own existing unit tests. Assert
  `/fetch server-health` and `/fetch integrations` output never includes
  `secret_ref` or any decrypted grant field for a guild with a stored authorization —
  only status/expiry facts, matching how Watchdog/Activity Ledger capability facts
  already surface enabled/disabled state without leaking internals.
- **Deauthorization removes the stored authorization safely:** save a
  `connector_authorization` row, drive a valid signed deauthorization POST through
  the real route, assert `get_connector_authorization` returns `None` afterward, and
  assert an invalid-signature deauthorization POST is rejected with 403 and leaves
  the row untouched (verification-before-deletion, mirroring every other
  signature-then-ingest ordering already established in `callbacks.py`).
- **Idempotent startup:** calling `CallbackServer.start()` a second time on an
  already-started instance is a no-op (no exception, `_runner` identity unchanged,
  no second bind attempt) — both as a direct unit test on `CallbackServer` and as a
  structural fact about `_run_bot` (the reconnect branch never re-enters the
  construction/start code path at all).

## Testing and Rollout

Automated tests must cover: the two new store methods (round-trip save/get, sealed
value unreadable without the correct vault key, delete removing the row), 
`build_callback_routes` returning `()` when flags/credentials are absent, returning
Meta's two routes when only Meta is configured, TikTok's one route when only TikTok
is configured, and both platforms' full route sets when both are configured; every
security property above; and that `_run_bot` starts and cleanly closes the callback
server exactly once per process lifetime, including across the
`PrivilegedIntentsRequired` reconnect path.

## Completion Gate

This spec is complete only when: `krubit run` starts a listening callback server
whenever it is configured; Meta and TikTok OAuth authorizations that complete
successfully persist a sealed, retrievable authorization; Meta deauthorization
requests remove that authorization; every security property above is verified by an
automated test, not just asserted by design; and calling `start()` twice, or
restarting the bot's Discord connection without restarting the whole process, never
produces a second bound callback server.
