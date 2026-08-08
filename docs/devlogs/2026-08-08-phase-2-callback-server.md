# Krubit Development Log: Phase 2 Callback Server

**Date:** August 8, 2026
**Status:** Implementation and verification through the whole-branch review and its
fix wave complete. No live Discord guild or credentialed Meta/TikTok app was
available in this development session; see
[Known limitations](#known-limitations-that-change-what-this-build-actually-does)
below for exactly what is evidenced versus deferred.

## Scope

This effort closes the second of Phase 2's three documented production gaps — "the
OAuth/push callback server is never started by `krubit run`" — per the
[Phase 2 callback server design](../superpowers/specs/2026-08-07-phase-2-callback-server-design.md).
The design went through two rounds of review before implementation began: the
first fixed a stateless, non-durable OAuth-state design, TikTok's static-redirect
incompatibility, and an incorrect assumption about Meta's deauthorization
protocol; the second caught a deeper identity-modeling error — the design
originally assumed `creator_accounts.external_id` was a platform-resolved stable
id, when it is in fact just the URL-parsed handle captured at registration time
for every platform, requiring identity verification to be resolved fresh at
authorization time instead.

Krubit gains: a running callback server bound to loopback by default; durable,
single-use, account-bound OAuth authorization for TikTok, Instagram, and Threads;
Meta's deauthorization and data-deletion webhook contract, covering all four Meta
capabilities' authorized-account cleanup even where authorization itself is
unsupported; and a documented, honest gap for Facebook Page and Facebook Profile
authorization rather than a silently broken one.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | Durable `oauth_attempts` table and store methods, replacing in-memory HMAC-signed state — single-use, expiring, restart-safe |
| 2 | `connector_authorizations` split into `provider_resource_id` (the resource being monitored) and `authorization_subject_id` (the user who authorized), with a safe status DTO |
| 3 | `data_deletion_requests` table backing Meta's confirmation-code/status-check contract |
| 4 | `CallbackServer` bound to `127.0.0.1` by default, access logging disabled so OAuth codes/state never reach stdout |
| 5 | `SignedFormRequest`/`build_signed_form_route` scaffold and `verify_meta_signed_request`, implementing Meta's real form-posted `signed_request` protocol (not the header-based scheme originally assumed) |
| 6 | `TikTokConnector.fetch_authorized_identity` — independently confirms a TikTok account's `username` from the API, since `resolve_account` only ever echoes its caller's input |
| 7 | `krubit/web/wiring.py`: durable, account-bound Meta/TikTok OAuth authorization routes |
| 8 | Meta deauthorization and data-deletion routes, keyed on `authorization_subject_id`, gated independently of the credential vault |
| 9 | `_run_bot` wiring: callback server and dedicated OAuth session started/stopped exactly once, `KRUBIT_CALLBACK_BIND_HOST` setting |
| 10 | `/fetch integrations` renders connector authorization status via the existing `HealthFinding` pattern |
| 11 | `oauth_attempts` purge wired into the existing sweep cycle, isolated so a purge failure never blocks guild sweeps |
| 12 | Cross-cutting security tests (token-leak, idempotent start, partial-bind-failure) against the fully assembled route set |

## Whole-branch review and its fix wave

The final whole-branch review (dispatched on the most capable available model,
per this codebase's subagent-driven-development discipline) found two defects
that had survived all twelve task-scoped reviews because each is only visible
across files a single task's review never sees together:

1. **`connector_authorizations`' two new columns were added via `CREATE TABLE IF
   NOT EXISTS`** — a no-op against the table's pre-existing shipped 7-column
   shape. Every save/get/find/list call would have raised
   `sqlite3.OperationalError` on any already-deployed database, and Meta's
   data-deletion endpoint would have returned a valid confirmation code while
   deleting nothing. Fixed with an additive `PRAGMA table_info` +
   `ALTER TABLE ... ADD COLUMN` migration, mirroring the existing
   `live_signal_deliveries` precedent, with a regression test that seeds the old
   7-column shape and proves the upgrade path works.
2. **Meta's account-binding check was structurally incapable of failing** for
   Facebook Page and Facebook Profile (both connectors echo their caller's input
   handle, so the comparison was always `x == x`), and fail-open for
   Instagram/Threads when Graph omitted `username`. Fixed for Instagram/Threads
   by requiring an independently-fetched `username` and rejecting on its
   absence — mirroring Task 6's TikTok fix exactly.

The first attempt at fixing the Facebook Page/Profile half of finding 2 compared
a Graph-resolved id against `account.external_id` — which a scoped re-review
caught was itself just the URL-parsed handle, converting the always-*passes* bug
into an always-*fails* bug (100% of Facebook Page/Profile authorizations would
have been rejected in production). Per this project's process discipline, a
final-review fix wave gets exactly one fix dispatch and one scoped re-review — no
second wave — so this residual, load-bearing finding surfaced to the project
owner rather than looping. The owner's decision: ship without Facebook Page/
Profile authorization working, as an honest, documented gap rather than a broken
feature. That is what shipped: an explicit rejection before any Meta API call is
even made, the same generic error every other rejection path already uses, and a
new bullet in the design spec's Explicit Exclusions section.

## Known limitations that change what this build actually does

- **Facebook Page and Facebook Profile OAuth authorization is not supported.**
  Neither connector can independently confirm which Facebook account granted
  access without a proper Page-token exchange (`/me/accounts`) this build does
  not implement; Facebook Profile additionally has no Graph-comparable
  handle/username field for a personal profile at all. Authorization attempts
  for both capabilities are cleanly rejected before any Graph API call.
  Instagram, Threads, TikTok, and Meta's deauthorization/data-deletion routes
  (which still cover all four Meta capabilities, including Facebook Page/
  Profile, for cleanup purposes) are unaffected.
- **Nothing generates the "click to authorize" link a creator actually clicks.**
  `MetaOAuthStates`/`TikTokOAuthStates.issue()`-equivalent call sites do not
  exist anywhere in the codebase. This build only makes the *receiving* half of
  the OAuth flow durable and correct; issuing the link is per-account credential
  resolution work (Phase 2's first documented production gap), tracked
  separately.
- **No live Discord guild or credentialed Meta/TikTok developer app was
  available in this development session.** Every property in this devlog is
  evidenced by the automated test suite (1079 tests, all passing) and by direct
  reading of the installed `aiohttp`/Meta/TikTok API shapes against the
  implementation — not by an end-to-end run against real Meta/TikTok
  infrastructure. The [Phase 2 live canary](../operations/phase-2-completion-audit.md)
  item already tracks this class of gap for the rest of Phase 2.
- **`pyright` is not currently a meaningful gate for this codebase.** The
  environment fails to resolve `aiohttp`/`aiosqlite` type stubs, producing
  thousands of pre-existing errors unrelated to any code in this branch
  (confirmed via before/after diffing on every task touched). `ruff` and the
  full `pytest` suite remain meaningful and both pass clean.
