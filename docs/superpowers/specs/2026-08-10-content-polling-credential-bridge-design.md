# Content Polling Credential Bridge Design

**Date:** 2026-08-10
**Status:** Approved for implementation planning
**Scope:** Wire Instagram, Threads, and TikTok into Krubit's existing
content-polling scheduler by bridging per-account OAuth credentials
(already issued by `/fetch creator authorize`) into the already-complete
`Connector` protocol and `ConnectorScheduler`. Closes the "content
polling for Instagram/Facebook/Threads/TikTok" backlog item for three of
those four platforms.

## Context

Investigation found this gap is much narrower than "build content
polling." The polling engine (`ConnectorScheduler`) is already generic,
durable, and platform-agnostic — durable per-account schedule, per-
platform concurrency limits, full failure isolation (one guild's or one
platform's failing connector can never affect any other job in the same
cycle). `InstagramConnector`, `ThreadsConnector`, and `TikTokConnector`
already fully implement the `Connector` protocol
(`resolve_account`/`fetch_page`/`health`). Only YouTube, X, and Bluesky
are actually wired into the scheduler today
(`src/krubit/__main__.py`'s `_build_content_connectors`), because those
three use one fixed bot-wide credential — Instagram/Threads/TikTok each
need a different access token per enrolled creator account, and nothing
resolves that per-account credential at poll time. That resolution
mechanism is the entire scope of this plan.

The pieces this plan depends on, already built and unchanged by it:

- `SQLiteStore.get_connector_authorization(guild_id, account_id,
  capability)` — returns the stored `ConnectorAuthorization` (sealed
  token reference, status, expiry) for one account+capability, or `None`.
- `CredentialVault.open_json(sealed) -> dict` — decrypts a sealed token
  reference back into the original OAuth grant shape (`access_token`,
  `refresh_token`, `expires_at`).
- `ConnectorFailure.authorization(detail)` → `ConnectorFailureKind.
  AUTHORIZATION` → `CapabilityState.AUTHORIZATION_REQUIRED` (already
  wired in both `meta.py`'s and `tiktok.py`'s `_HEALTH_STATE_BY_FAILURE`
  maps) — the exact existing mechanism for "this needs to be
  (re-)authorized," already surfaced by `/fetch creator show`'s health
  reporting with no new domain concept required.
- `MetaConnectorError`/`TikTokConnectorError` — both simple `RuntimeError`
  subclasses carrying a `ConnectorFailure`, both already caught and
  classified correctly by `ConnectorScheduler`'s existing failure-handling
  path.

## Confirmed decisions (from conversation)

1. **No token-refresh exchange built now.** An expired access token
   degrades to `AUTHORIZATION_REQUIRED` (the existing mechanism above) —
   the creator re-runs `/fetch creator authorize` to get a fresh one.
   Building a refresh-token exchange is real, separate scope with its own
   failure modes (a bad refresh token, a revoked grant) and nothing to
   verify it against without live credentials anyway.
2. **Platforms: Instagram, Threads, TikTok.** Facebook Page/Profile
   excluded, consistent with every other decision this session (Facebook
   Page's OAuth flow is already a documented dead end; Facebook Profile
   has no Graph-comparable identity surface at all).
3. **Stateless, per-call credential resolution — no caching.** Each
   `fetch_page` call re-resolves and re-constructs the underlying
   connector fresh from the current stored authorization, rather than
   caching a connector instance keyed by account. Simpler, and correctly
   picks up a creator's re-authorization immediately on the very next
   poll rather than requiring a cache invalidation mechanism.
4. **Wired in unconditionally under `creator_signals_enabled`** — no app
   credentials (`meta_app_id`, `tiktok_client_key`) needed at the
   scheduler-wiring level itself, only at authorization time (already
   built, separate). An account with no completed authorization simply
   reports `AUTHORIZATION_REQUIRED` and is skipped by the poll cycle,
   same as any other degraded connector.

## Implementation approach

- **Three new wrapper classes**, one per platform, each satisfying the
  `Connector` protocol structurally (matching this codebase's existing
  "structural protocol, no inheritance" convention):
  `InstagramScheduledConnector`, `ThreadsScheduledConnector`,
  `TikTokScheduledConnector`. Each constructed with `(session, store,
  vault)` — no fixed token, unlike the existing per-use connectors they
  wrap.
- **`fetch_page(account, cursor)`** on each wrapper: look up
  `store.get_connector_authorization(account.guild_id, account.account_id,
  Capability.SOCIAL.value)`. If `None`, or `status != "active"`, or
  `expires_at` is at or before "now," raise the platform's existing error
  type (`MetaConnectorError`/`TikTokConnectorError`) carrying
  `ConnectorFailure.authorization(...)` — exactly the same shape every
  other authorization failure in these modules already produces, so
  `ConnectorScheduler`'s existing handling requires no changes. Otherwise,
  unseal the token via `vault.open_json(authorization.secret_ref)`,
  construct the real `InstagramConnector`/`ThreadsConnector`/
  `TikTokConnector` with the resolved `access_token`, and delegate
  `fetch_page(account, cursor=cursor)` to it.
- **`health(account)`** on each wrapper: mirrors `fetch_page`'s
  authorization lookup (without actually fetching content) to report
  `AUTHORIZATION_REQUIRED` up front when no valid authorization exists,
  or delegates to a freshly-constructed inner connector's `health()`
  otherwise.
- **`resolve_account`**: implemented for protocol completeness (delegates
  the same way, after resolving credentials), even though nothing in the
  current codebase calls it on a scheduler-facing connector today
  (confirmed: `resolve_account` is only ever called from the OAuth
  callback route in `web/wiring.py`, using the freshly-obtained token
  directly, never through a scheduler-registered connector).
- **`_build_content_connectors`** (`src/krubit/__main__.py`) gains three
  new entries, unconditional (no settings check beyond the function's
  existing `creator_signals_enabled` gate at the call site), each
  constructed with the shared `aiohttp.ClientSession`, `store`, and
  `CredentialVault` already available at that call site.

## Explicit Exclusions

- No token-refresh exchange (decision 1).
- No Facebook Page/Profile polling (decision 2).
- No change to `ConnectorScheduler`, `ConnectorFailure`,
  `MetaConnectorError`/`TikTokConnectorError`, or any existing connector
  class — this plan only adds three new wrapper classes and three new
  entries in `_build_content_connectors`.
- No change to the OAuth authorization flow itself (issuance, callback
  routes, identity verification) — already built, untouched.

## Testing

- Unit tests per wrapper: `fetch_page` with no stored authorization
  raises the correct authorization-failure error; with an expired
  authorization, same; with a valid, active authorization, correctly
  unseals the token and delegates to the inner connector (verify via a
  fake/mock inner connector or by asserting the correct token reached the
  underlying HTTP call).
- Unit test: `health(account)` reports `AUTHORIZATION_REQUIRED` for a
  missing/expired authorization without attempting any content fetch.
- A test proving `_build_content_connectors` includes all three new
  platforms in its returned mapping when `creator_signals_enabled` is
  true, with no dependency on `meta_app_id`/`tiktok_client_key` being
  set.

## Completion Gate

Complete when: `ConnectorScheduler` can successfully poll an Instagram,
Threads, or TikTok account that has a valid, active OAuth authorization
on file, correctly reports `AUTHORIZATION_REQUIRED` for one that doesn't
(without crashing the poll cycle), and the full test suite passes with no
regressions. Live end-to-end verification against a real, credentialed
account remains the project owner's own step once Meta/TikTok app review
is complete — matching this project's established convention for every
prior OAuth-adjacent piece of work.
