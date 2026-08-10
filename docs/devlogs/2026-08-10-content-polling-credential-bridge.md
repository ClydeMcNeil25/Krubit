# Krubit Development Log: Content Polling Credential Bridge

**Date:** August 10, 2026
**Status:** Implementation, fix wave, and final review complete. Merged to `main`. Live end-to-end polling against real accounts is pending Meta/TikTok App Review, same as `/fetch creator authorize` before it.

## Scope

Closes "content polling for Instagram/Facebook/Threads/TikTok" for three of
those four platforms. Investigation found the gap was much narrower than
"build content polling": `ConnectorScheduler` (durable per-account
schedule, per-platform concurrency limits, full failure isolation) and the
`InstagramConnector`/`ThreadsConnector`/`TikTokConnector` classes already
fully implemented everything needed — only YouTube, X, and Bluesky were
actually wired into the scheduler, because those three use one fixed
bot-wide credential. Instagram/Threads/TikTok each need a different access
token per enrolled creator account, and nothing resolved that per-account
credential at poll time. See
[the design spec](../superpowers/specs/2026-08-10-content-polling-credential-bridge-design.md).

## Delivered

- `src/krubit/integrations/credential_bridge.py` — `CredentialResolvingConnector`,
  one parameterized class (not three near-duplicates, correctly superseding
  the spec's original three-class sketch) satisfying the `Connector`
  protocol by resolving a fresh per-account OAuth token from storage on
  every call — stateless, no caching, so a creator's re-authorization takes
  effect on the very next poll. Missing, inactive, expired, or unsealable
  authorizations all degrade to the platform's existing
  `AUTHORIZATION_REQUIRED` failure shape — no token-refresh exchange, by
  design (decision confirmed during brainstorming: an expired token means
  the creator re-runs `/fetch creator authorize`).
- `_build_content_connectors` (`src/krubit/__main__.py`) gains three new
  entries — Instagram, Threads, TikTok — gated only on a `CredentialVault`
  actually being configured, no app credentials needed at the wiring level.

## Final review and fix wave

The final whole-branch review found no Critical defects — token lifetime,
concurrency safety, and the `_run_bot` startup reordering all held up
under independent tracing — but surfaced a false premise in the design
itself: the spec assumed `AUTHORIZATION_REQUIRED` was "already surfaced by
`/fetch creator show`'s health reporting." It wasn't.
`ConnectorScheduler.result()`, `content_schedule.last_state`, and
`Connector.health()` all had zero production readers; `/fetch creator show`
reported only delivery stats. An account with no completed authorization
would have polled, failed, and backed off silently forever with no
staff-visible signal anywhere in the product.

Fixed in a single consolidated fix wave, re-verified by a scoped
re-review rather than trusting the implementer's report:

1. **`/fetch creator show` now surfaces poll status.** Reads the account's
   `content_schedule` row (already written every cycle) and renders a
   "Poll status" line from the existing `CapabilityState`/`safe_detail`
   values — no invented state strings. Verified the new test drives a real
   `ConnectorScheduler.run_cycle()` rather than faking the schedule row
   directly, and that an unpolled account degrades to "Not yet polled"
   rather than a misleading default.
2. **`CredentialVaultError` was escaping the bridge's own failure
   taxonomy.** An operator rotating `KRUBIT_CREDENTIAL_ENCRYPTION_KEY`
   would have made every stored authorization fail AES-GCM tag
   verification at once — and that error wasn't caught, so it reported as
   a generic "unexpected error" instead of `AUTHORIZATION_REQUIRED`, and
   made `health()` raise instead of returning a value like every sibling
   connector's `health()` does. Fixed by catching `CredentialVaultError`
   narrowly (not a broad `except Exception`, which would have masked real
   bugs) and re-raising as the same safe authorization-failure shape every
   other guard clause in the method already produces.
3. **Adjacent bug in `/fetch admin integrations`**: an authorization whose
   `status` column was still `"active"` but whose `expires_at` had already
   passed reported `severity="healthy"`. Harmless before this branch;
   actively misleading now that expiry is a hard poll-blocking condition
   for the scheduler. Fixed to require both `status == "active"` and an
   unexpired `expires_at`.

## Known limitations

- **No token-refresh exchange** — deliberate scope exclusion; an expired
  token requires the creator to re-run `/fetch creator authorize`.
- **Facebook Page/Profile excluded** — consistent with every prior
  decision this session (Facebook Page's OAuth flow is a documented dead
  end; Facebook Profile has no Graph-comparable identity surface).
- **Four Minor findings deferred, not blocking:** `SchedulerResult` is
  keyed per-platform, not per-account, so if a future staff surface reads
  it directly the key must be widened first; the bridge's unseal-and-
  validate logic duplicates (as a strict, correct subset of) the existing
  `open_oauth_grant` helpers in `meta.py`/`tiktok.py`; the design spec's
  text describing `resolve_account` as delegating is stale — it correctly
  raises `NotImplementedError`, confirmed dead code with zero production
  callers; and there's no dedicated test for `health()`'s success-path
  delegation to the inner connector.
- **Live end-to-end verification against real, credentialed accounts**
  remains the project owner's own step once Meta/TikTok App Review is
  complete, matching this project's established convention for every
  prior OAuth-adjacent piece of work.

## Test evidence

Full suite: 1184/1184 passing at merge. Ruff clean throughout. Pyright
error count unchanged from each changed file's pre-existing baseline (356
errors before and after the fix wave, all pre-existing discord-stub/typing
noise, confirmed via before/after diffing during the scoped re-review).
