# Krubit Development Log: `/fetch creator authorize`

**Date:** August 10, 2026
**Status:** Implementation, fix wave, and final review complete. Merged to `main`. Live credentials not yet configured (Meta App Review is a lengthy process, deferred by the project owner).

## Scope

Phase 2's first documented production gap — "nothing generates the
click-to-authorize link a creator actually clicks" — closed. Investigation
found the gap was narrower than the earlier devlog's wording suggested:
`SQLiteStore.issue_oauth_attempt` (durable, DB-backed OAuth state
issuance) and the receiving/token-exchange side
(`src/krubit/web/wiring.py`) already existed, fully wired, verified by
code trace and the test suite with no live credentials. What was missing
was exactly two things: building the actual platform authorization URL,
and a Discord command to request one. See
[the design spec](../superpowers/specs/2026-08-10-creator-authorize-link-design.md)
for the full architecture and the verified-not-guessed scope research.

## Delivered

- `src/krubit/integrations/authorize_urls.py` — pure URL-building
  functions for Meta (Instagram/Threads) and TikTok, with a whitelist
  (not blacklist) approach so an unsupported platform/capability
  combination raises `ValueError` by construction.
- `/fetch creator authorize <url> <capability>` — staff-or-self
  authority (reusing the exact `require_creator_authority` function
  `/fetch creator add` already uses), verifies the account is already
  registered **and** that its stored `owner_member_id` matches the
  intended owner (defense-in-depth beyond the authority check alone),
  before issuing anything.
- Scope: Instagram, Threads, TikTok, `account`/`social` capabilities
  only — Facebook Page/Profile and `live` capability explicitly excluded,
  since both are already-documented dead ends even with a working link.

## Final review and fix wave

The final whole-branch review found a real Critical bug: the Threads
authorize link was routed through Facebook's Login dialog
(`facebook.com/v21.0/dialog/oauth`), but this codebase's own Threads
token exchange (pre-existing, untouched code) already redeems codes at
the standalone Threads API host (`graph.threads.net`) — the Facebook
dialog neither grants the right scopes nor mints a code that host would
accept. A member clicking a Threads link would have hit an invalid-scope
error, or gotten a code that failed at exchange after the single-use
state was already burned with no recovery path. This was a defect in the
plan's own scope research — the scope *names* were verified against
Meta's real permissions reference, but the authorization *host* was
assumed rather than checked. Fixed by giving Threads its own host
(`threads.net/oauth/authorize`), independently re-confirmed against
external documentation during the scoped re-review, while keeping the
same `client_id` (the receiving side already assumes Threads shares
Instagram's Meta app credentials).

Two Important findings were fixed in the same pass: `redirect_uri` was
built from `META_KRUBIT_CALLBACK_BASE_URL`/`TIKTOK_KRUBIT_CALLBACK_BASE_URL`
— two settings with no enforced relationship to
`KRUBIT_CALLBACK_PUBLIC_BASE_URL`, the only one actually validated and
used to start the callback server — fixed by building both platforms'
redirect URI from the correct setting instead. And the `oauth_attempts`
purge sweep never started under `creator_signals_enabled=true,
activity_ledger_enabled=false` (a real, supported configuration) despite
`ActivityRuntime.sweep_cycle` deliberately un-gating the purge itself for
exactly this scenario — the loop-start condition one layer up was never
updated to match. An audit receipt for the authorize action was also
added (never including the state token).

## Known limitations

- **Live credentials are not configured.** Meta App Review and TikTok's
  own review process are both lengthy; the project owner has deferred
  starting that process.
- **A pre-existing, unrelated bug**, flagged during this review but
  explicitly out of scope for this branch: Instagram's identity
  verification on the receiving side reads the wrong Graph API field
  (`/me` instead of resolving the IG Business account via
  `/me/accounts`), which will likely reject every real Instagram
  authorization once credentials exist. Worth fixing before any live
  canary attempt — tracked as separate follow-up work.

## Test evidence

Full suite: 1164/1164 passing at merge. Ruff clean throughout. No live
Meta/TikTok verification performed — matches this project's established
convention for every prior OAuth-adjacent piece of work.
