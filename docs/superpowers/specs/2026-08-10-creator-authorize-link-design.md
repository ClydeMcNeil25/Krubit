# `/fetch creator authorize` — OAuth Link Issuance Design

**Date:** 2026-08-10
**Status:** Approved for implementation planning
**Scope:** The missing middle piece of Phase 2's creator OAuth flow — a
command that builds a real, clickable authorization URL and sends it to a
member. This closes the "Phase 2's first documented production gap"
(nothing generates the click-to-authorize link) flagged in
`docs/devlogs/2026-08-08-phase-2-callback-server.md`. Confirmed
interactively with the project owner: build this fully now, wired all the
way to where real Meta/TikTok app credentials plug in, deferring only the
actual credential acquisition (a separate, owner-only task requiring app
creation and platform review) to later.

## Context

Investigation into this codebase found the gap is narrower than the
existing devlog's wording suggested. Two pieces already exist and are
fully wired:

- `SQLiteStore.issue_oauth_attempt(...)` (`src/krubit/storage/sqlite.py:2205`)
  — durable, single-use, DB-backed OAuth state issuance. Already used by
  the receiving side; nothing currently calls it to *start* a flow.
- The receiving callback routes (`_build_meta_authorize_route`/
  `_build_tiktok_authorize_route` in `src/krubit/web/wiring.py`) already
  consume that state, exchange the code, verify identity, and persist the
  authorization — fully built and tested against no live credentials, per
  this project's established "code-trace + test-suite verified, live
  canary deferred" convention.

What's missing is exactly two things: (1) code that builds the actual
platform-side authorization URL, and (2) a Discord command that calls
`issue_oauth_attempt` and returns that URL to a member. The older
in-memory `MetaOAuthStates`/`TikTokOAuthStates` classes
(`src/krubit/integrations/meta.py:131`, `tiktok.py:105`) are a separate,
apparently superseded mechanism — the actual wired-up receiving side uses
`issue_oauth_attempt`/`consume_oauth_attempt`, not those classes. This
plan does not touch or resurrect them.

## Confirmed decisions (from conversation)

1. **Command:** `/fetch creator authorize <url> <capability>` — mirrors
   `/fetch creator add`'s existing `url` parameter and catalog-lookup
   pattern exactly, rather than inventing a new account-selection
   mechanism. `capability` is a choice of `account`/`social` (see decision
   4 below for why `live` is excluded).
2. **Authority: staff-or-self.** A member can request an authorize link
   for their own creator account; staff can also request one on behalf of
   another member, matching `/fetch creator add`'s existing optional
   `member` parameter.
3. **Platform scope: Instagram, Threads, TikTok only.** Facebook Page and
   Facebook Profile are excluded — both are already-documented dead ends
   even with a working link (no Page-token exchange implemented for
   Facebook Page; Facebook Profile has no Graph-comparable identity
   surface at all). Attempting to authorize either returns a clear
   rejection before any URL is built, not a link that silently can't be
   completed downstream.
4. **Capability scope: `account`/`social` only, not `live`.** The
   catalog (`src/krubit/integrations/catalog.py`) marks LIVE detection for
   Instagram as `APPROVAL_REQUIRED` and for TikTok as "pending reliable
   detection access" — neither platform has a stable, well-documented
   scope for this today. Guessing at one now risks a silently-wrong value
   that only surfaces once real credentials exist to test against; adding
   `live` later, once a real scope is confirmed, costs nothing today.

## Verified external facts (not assumed — checked against primary/current sources)

- **Meta OAuth dialog:** `https://www.facebook.com/v{version}/dialog/oauth`
  with `client_id`, `redirect_uri`, `state`, `scope`, `response_type=code`.
- **Instagram scopes** (from Meta's own permissions reference,
  `developers.facebook.com/docs/permissions`): `instagram_basic` (account
  info), `instagram_content_publish` (posting/social — requested alongside
  `instagram_basic`, not instead of it, since content operations still need
  basic profile access).
- **Threads scopes** (same source): `threads_basic` (account),
  `threads_content_publish` (social, requested alongside `threads_basic`).
- **TikTok authorize URL:** `https://www.tiktok.com/v2/auth/authorize/`
  with `client_key`, `scope`, `response_type=code`, `redirect_uri`, `state`.
- **TikTok scopes:** `user.info.profile` for account/identity — this
  exact scope name is already load-bearing in this codebase's own receiving
  side (`src/krubit/web/wiring.py:240`'s comment: `identity.username is
  None` happens when "no `user.info.profile` scope granted" — the existing
  code depends on this exact scope already, so reusing it here is
  correctness, not a new guess). `video.list` added for social.
- **Redirect URIs:** must exactly match the existing receiving routes'
  paths — `{settings.meta_callback_base_url}/callbacks/meta/authorize` and
  `{settings.tiktok_callback_base_url}/callbacks/tiktok/authorize`
  (`src/krubit/web/wiring.py:272,373`).

## Implementation approach

- **New pure functions** (framework-independent, matching this codebase's
  convention of keeping URL/business logic separate from the Discord
  layer) in a new module, e.g. `src/krubit/integrations/authorize_urls.py`:
  `build_meta_authorize_url(*, app_id, redirect_uri, state, platform,
  capability) -> str` and `build_tiktok_authorize_url(*, client_key,
  redirect_uri, state, capability) -> str`. Each raises `ValueError` for
  an unsupported platform/capability combination (Facebook Page/Profile,
  or `live`) rather than silently building a URL that can't work.
- **New service-layer method** on `ContentCommandService`
  (`src/krubit/discord/content_commands.py:150`, the same class housing
  `creator_add` at line 175) resolving the caller's `CreatorAccount` for
  the given URL, calling
  `store.issue_oauth_attempt(...)`, building the platform URL, and
  returning a `CommandResult` with the link in its card description
  (ephemeral — this is a personal action link, never posted publicly).
- **New command** `authorize` on `CreatorCommands`
  (`src/krubit/discord/content_commands.py`), following the exact
  `add`/`_actor_context`/staff-or-self pattern already established for
  `/fetch creator add`.
- **No new Settings field** — `meta_app_id`, `meta_callback_base_url`,
  `tiktok_client_key`, `tiktok_callback_base_url` already exist in
  `Settings` (currently unset placeholders in `.env.example`); this
  feature is the first thing that actually reads `meta_app_id`/
  `tiktok_client_key` for outbound URL construction (previously only read
  on the receiving/token-exchange side).
- **Graceful behavior with no credentials configured:** if
  `settings.meta_app_id`/`tiktok_client_key` is `None` (the current live
  state), the command returns a clear `CommandStatus.FAILED` with a
  message explaining the platform isn't configured yet — never a broken
  or malformed link.

## Explicit Exclusions

- No Facebook Page/Facebook Profile support (already-documented gap,
  unrelated to this plan).
- No `live` capability support yet (decision 4).
- No change to `MetaOAuthStates`/`TikTokOAuthStates` (superseded,
  untouched).
- No change to the receiving-side callback routes, token exchange, or
  identity verification — this plan only adds the missing outbound half.
- No live Meta/TikTok credential acquisition — that remains the project
  owner's own task (creating developer apps, obtaining app review),
  tracked separately, not part of this implementation.

## Testing

- Unit tests for `build_meta_authorize_url`/`build_tiktok_authorize_url`:
  correct URL shape and query parameters for each valid
  platform/capability combination; `ValueError` for Facebook Page/Profile
  and for `live`.
- Service-layer tests: staff-or-self authority (denied before any query,
  matching this module's established discipline); correct
  `issue_oauth_attempt` call with the right `guild_id`/`member_id`/
  `account_id`/`platform`/`capability`/`redirect_uri`; correct handling of
  "platform not configured" (missing `app_id`/`client_key`).
- Discord-layer test: command reachable, staff-or-self denial/allow paths,
  ephemeral response.

## Completion Gate

Complete when: `/fetch creator authorize <url> <capability>` builds a
correct, verified-shape authorization URL for Instagram/Threads/TikTok
account/social combinations, correctly rejects Facebook Page/Profile and
`live` before building anything, correctly reports "not configured" when
credentials are absent (today's live state), and the full test suite
passes. Live end-to-end verification against real Meta/TikTok apps remains
a separate, owner-driven follow-up once credentials exist — matching this
project's established convention for every other OAuth-adjacent piece of
work.
