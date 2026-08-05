# Phase 2 Creator Signal Hub — Operations Guide

This guide covers the full multi-platform Creator Signal and Notification Hub built in
the 2026-08-04 Phase 2 completion effort (Tasks 1-13), on top of the Phase 2A Twitch/
Discord-presence baseline documented separately in the
[Phase 2A live-stream signal guide](phase-2a-live-stream-signals.md). Read that guide
first if Twitch live signals are not already running.

> **Read this before enabling anything in this guide.** Three production gaps are
> called out prominently below because they change what "enabled" actually means for
> an operator: the Meta and TikTok connectors do not run in the scheduler yet (see
> [Meta and TikTok are not scheduled](#meta-and-tiktok-are-not-scheduled-in-this-build)),
> the OAuth/push callback server is not started by `krubit run` yet (see
> [The callback ingress server is not wired into the running process](#the-callback-ingress-server-is-not-wired-into-the-running-process)),
> and Discord Scheduled Event synchronization has no production call site at all (see
> [Scheduled Event synchronization has no production call site](#scheduled-event-synchronization-has-no-production-call-site)).
> `KRUBIT_CREATOR_SIGNALS_ENABLED` and `KRUBIT_SOCIAL_DELIVERY_ENABLED` themselves are
> fully enforced — leaving both at their `false` default guarantees the connector
> polling scheduler never starts and no Discord message is ever sent — but the three
> gaps above apply even once both are `true`.

## What this build adds over Phase 2A

- A creator registry:
  `/fetch creator add|remove|list|show|verify|pause|resume|route|transfer|template`,
  guild-scoped, owner- and admin-authorized.
- A connector catalog covering Twitch, YouTube, X, Instagram, Facebook Pages, Facebook
  profiles, Threads, Bluesky, TikTok, and Fanbase, each declaring an honest
  `ready` / `unconfigured` / `authorization_required` / `approval_required` /
  `degraded` / `quota_limited` / `unsupported` state per capability
  (account/social/live).
- A normalized content ledger, cross-platform correlation, quiet hours, mention
  budgets, and a shared delivery engine used by every connector, including the
  migrated Twitch/Discord-presence path.
- `#social-notifications` delivery for posts/uploads/Shorts/Reels/videos, alongside the
  existing `#live-notifications` live-card path.
- Discord Scheduled Event synchronization for supported scheduled streams — implemented
  and tested, but **not called from any production code path yet**; see
  [Scheduled Event synchronization has no production call site](#scheduled-event-synchronization-has-no-production-call-site).
- `/fetch notifications`, `/fetch notifications preview|retry|retract`, `/fetch latest`,
  `/fetch schedule`, and an expanded `/fetch integrations`.

None of this is reachable until an operator opts in. Two independent flags gate it, and
both are now fully enforced end to end — not just parsed and validated:

```dotenv
KRUBIT_CREATOR_SIGNALS_ENABLED=false
KRUBIT_SOCIAL_DELIVERY_ENABLED=false
```

`Settings.from_env` (`src/krubit/config.py`) defaults both to `false`
(`tests/test_phase_2_rollout.py::test_new_connectors_default_disabled_and_can_be_enabled_independently`
and `tests/test_config.py::test_missing_social_settings_all_default_to_none_or_disabled`
enforce this). `KRUBIT_CREATOR_SIGNALS_ENABLED=false` means `krubit run` never builds a
connector, never starts the polling scheduler, and `_content_scheduler_enabled` on
`KrubitBot` stays `False`
(`tests/test_cli.py::test_content_scheduler_never_runs_when_creator_signals_disabled`).
`KRUBIT_SOCIAL_DELIVERY_ENABLED=false` means `ContentRuntime.apply_plan` — the single
choke point every send/edit path (`apply_plans`, `recover_pending`, `retry_delivery`,
and `/fetch notifications retry`, which shares the exact same `ContentRuntime` instance
`KrubitBot` polls into) runs through — returns immediately without resolving a route,
deciding a mention, or touching Discord at all
(`tests/test_content_runtime.py::test_apply_plan_sends_nothing_when_social_delivery_is_disabled`).
Enabling one flag does not enable the other, and neither retroactively enables Phase
2A's separate `KRUBIT_LIVE_SIGNALS_ENABLED` flag or any individual connector's
credentials — a missing per-platform credential leaves that platform's capabilities at
`unconfigured` regardless of these two flags.

## Environment variables (exact names)

All variables are read only from the workspace master `.env` through
`scripts/invoke-krubit.ps1`, which allowlists every name below — do not add a new
variable to `src/krubit/config.py` without also adding it to the launcher's
`$allowedNames` list, or the running process will never see it.

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_KRUBIT_APPLICATION_ID` | Always | Discord application snowflake |
| `DISCORD_KRUBIT_BOT_TOKEN` | Always to run | Bot token, never logged (`repr=False`) |
| `KRUBIT_DATABASE_PATH` | Always | SQLite database path |
| `KRUBIT_STAFF_CHANNEL_ID` | Optional | Daily summary destination |
| `TWITCH_KRUBIT_CLIENT_ID` / `TWITCH_KRUBIT_CLIENT_SECRET` | If `KRUBIT_LIVE_SIGNALS_ENABLED=true` | Twitch Helix app credentials |
| `KRUBIT_LIVE_SIGNALS_ENABLED` | Optional, default `false` | Phase 2A Twitch/Discord-presence live path |
| `YOUTUBE_KRUBIT_API_KEY` | For YouTube uploads/live polling | YouTube Data API v3 key |
| `YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET` | For YouTube push (not wired — see below) | PubSubHubbub callback verification secret |
| `X_KRUBIT_BEARER_TOKEN` | For X polling | X API app-only bearer token |
| `META_KRUBIT_APP_ID` / `META_KRUBIT_APP_SECRET` | For Instagram/Facebook/Threads OAuth (not wired to the scheduler — see below) | Meta app credentials |
| `META_KRUBIT_CALLBACK_BASE_URL` | For Meta OAuth (not currently validated as `https://` — see Known limitations) | Meta OAuth redirect base |
| `TIKTOK_KRUBIT_CLIENT_KEY` / `TIKTOK_KRUBIT_CLIENT_SECRET` | For TikTok OAuth (not wired to the scheduler — see below) | TikTok Login Kit / Display API credentials |
| `TIKTOK_KRUBIT_CALLBACK_BASE_URL` | For TikTok OAuth (not currently validated as `https://`) | TikTok OAuth redirect base |
| `KRUBIT_CREATOR_SIGNALS_ENABLED` | Optional, default `false` | Master creator-registry/connector-polling flag |
| `KRUBIT_SOCIAL_DELIVERY_ENABLED` | Optional, default `false` | Master `#social-notifications` delivery flag |
| `KRUBIT_CREDENTIAL_ENCRYPTION_KEY` | To store/read any creator OAuth grant | AES-GCM key for `CredentialVault`; generate with `openssl rand -hex 32`, never a guessable password |
| `KRUBIT_CALLBACK_PUBLIC_BASE_URL` | To bind the callback server (not currently started — see below) | Must be `https://`; validated eagerly |
| `KRUBIT_CALLBACK_PORT` | To bind the callback server (not currently started — see below) | `1..65535` |

`.env.example` in the repository root lists every variable above with the same names.
Every social credential is optional at the `Settings` layer: a missing credential never
prevents Krubit from starting, it only holds the corresponding capability at
`unconfigured` or `authorization_required` (`tests/test_config.py::test_missing_social_credentials_do_not_prevent_bot_startup`).

## Prerequisites and least privilege (Discord side)

In addition to the Phase 1 and Phase 2A Discord role/intent requirements:

1. Create the text channel `#social-notifications` with that exact name before
   enabling `KRUBIT_SOCIAL_DELIVERY_ENABLED`. Krubit resolves it once by exact name and
   stores the ID (`_NOTIFICATION_CHANNEL_NAME = "social-notifications"` in
   `src/krubit/discord/content_commands.py`); a later rename does not redirect
   delivery.
2. Create the role `Creator` with that exact name (`_CREATOR_ROLE_NAME = "Creator"`,
   same file) before relying on Creator self-service. Members holding this role may
   manage only creator accounts they own; administrators may manage any account.
3. `#live-notifications` and `Streaming Now` remain the Phase 2A resources
   (`src/krubit/discord/live_runtime.py`); this build does not rename or duplicate
   them.
4. If Discord Scheduled Event synchronization is exercised, Krubit needs **Manage
   Events**. Krubit only mutates a Scheduled Event it created and recorded ownership
   for; it never searches by event name and never touches an event it does not own.
5. No new privileged Gateway intent is required beyond Phase 2A's Presence Intent.

## Platform developer setup

Set up each platform's developer application only for the connectors you intend to
run. None of this is required to start Krubit; every platform capability simply stays
`unconfigured`/`authorization_required` until configured.

- **YouTube**: create a Google Cloud project, enable the YouTube Data API v3, and
  create an API key for `YOUTUBE_KRUBIT_API_KEY`. See
  <https://developers.google.com/youtube/v3/getting-started#quota> for the (midnight
  Pacific-reset) daily quota Krubit's `YouTubeConnector` already respects via
  `retry_after_seconds`.
- **X**: apply for API access at <https://docs.x.com/x-api/users/get-posts> and
  generate an app-only bearer token for `X_KRUBIT_BEARER_TOKEN`. X's rate-limit reset
  is read from the `x-rate-limit-reset` response header when present.
- **Meta (Instagram / Facebook Pages / Facebook profiles / Threads)**: create a Meta
  app at <https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api>
  (Instagram) and <https://www.postman.com/meta/threads/overview> (Threads). Register
  `META_KRUBIT_APP_ID`/`META_KRUBIT_APP_SECRET`. `META_KRUBIT_CALLBACK_BASE_URL` is the
  base URL Meta redirects the OAuth authorization code to — see the callback-server
  caveat below before assuming this redirect is reachable in this build.
- **TikTok**: register a TikTok developer app for Login Kit and the Display API
  (<https://developers.tiktok.com/doc/display-api-overview/>) and set
  `TIKTOK_KRUBIT_CLIENT_KEY`/`TIKTOK_KRUBIT_CLIENT_SECRET`/`TIKTOK_KRUBIT_CALLBACK_BASE_URL`.
  TikTok's LIVE embed does not expose a reliable detection API
  (<https://developers.tiktok.com/doc/embed-live>); TikTok's live capability stays
  `approval_required` by design, never operational.
- **Bluesky**: no developer application or member OAuth is required — `BlueskyConnector`
  reads only public author feeds (<https://docs.bsky.app/docs/api/app-bsky-feed-get-author-feed>).
- **Fanbase**: URL recognition only. There is no official API or partner access to
  connect to yet; both social and live capabilities stay `unsupported` by design and
  must never be reported as operational.

## Known limitations that change what "enabling a flag" actually does

### Meta and TikTok are NOT scheduled in this build

`src/krubit/__main__.py`'s `_build_content_connectors` only wires YouTube, X, and
Bluesky into the polling scheduler:

```python
def _build_content_connectors(
    settings: Settings, session: aiohttp.ClientSession
) -> dict[Platform, Connector]:
    """... Only the platforms with a single bot-wide credential in `Settings` are wired
    here: YouTube (API key), X (bearer token), and Bluesky (no credential at all). Meta
    and TikTok connectors need one access token *per enrolled creator account* resolved
    from `connector_authorizations`/`CredentialVault` at poll time, not one fixed
    bot-wide token — that per-account credential resolution is a distinct feature this
    task does not build, so those platforms are intentionally left unscheduled for now
    rather than wired with a token that cannot be correct for more than one account."""
```

This is a deliberate safety choice, not an oversight: `InstagramConnector`,
`FacebookPageConnector`, `FacebookProfileConnector`, `ThreadsConnector`, and
`TikTokConnector` each accept one fixed token in their constructor. Wiring a single
bot-wide token would mean every enrolled creator's Instagram/Facebook/Threads/TikTok
content gets fetched under one account's credentials — actively wrong, not just
incomplete.

**Practical consequence**: setting `META_KRUBIT_APP_ID`/`META_KRUBIT_APP_SECRET` or
`TIKTOK_KRUBIT_CLIENT_KEY`/`TIKTOK_KRUBIT_CLIENT_SECRET`, and even completing
`/fetch creator add` for an Instagram/Facebook/Threads/TikTok URL, does **not** result
in Krubit ever polling that account for content in this build. The connectors,
authorization-state model, and OAuth grant classes are fully implemented and covered
by dedicated tests (`tests/test_meta_connectors.py`, `tests/test_tiktok_connector.py`,
`tests/test_meta_authorization.py`), but there is no production call site yet. A future
task must add per-account credential resolution (reading the sealed grant from
`CredentialVault`/`connector_authorizations` per poll) before scheduling these
platforms is safe. Do not work around this by passing a shared token — that would
recreate the exact cross-account credential leak this design avoids.

### The callback ingress server is not wired into the running process

`src/krubit/web/callbacks.py`'s `CallbackServer`, and the route builders
`build_push_route` (YouTube push), `build_oauth_redirect_route` (Meta/TikTok OAuth),
and `build_signed_webhook_route` (Meta signed webhooks) are fully implemented and
tested against a real `aiohttp.web.Application`
(`tests/test_callback_ingress.py`, `tests/test_meta_authorization.py`,
`tests/test_tiktok_connector.py`, `tests/test_youtube_push.py`). `Settings` already
parses and validates `KRUBIT_CALLBACK_PUBLIC_BASE_URL` (must be `https://`) and
`KRUBIT_CALLBACK_PORT`.

However, `src/krubit/__main__.py` and `src/krubit/discord/bot.py` never construct a
`CallbackServer` or start it — `krubit run` does not bind any callback listener in this
build, no matter what `KRUBIT_CALLBACK_PUBLIC_BASE_URL`/`KRUBIT_CALLBACK_PORT` are set
to. Practical consequences:

- YouTube push notifications (PubSubHubbub) cannot reach Krubit; YouTube can still be
  monitored by polling (`YouTubeConnector`'s `playlistItems.list` path), just not the
  lower-latency push path.
- A Meta or TikTok OAuth authorization-code redirect has nowhere to land, so an
  operator cannot complete Instagram/Facebook/Threads/TikTok creator OAuth consent
  end-to-end yet, independent of the scheduling gap above.
- Meta signed webhooks cannot be received.

A future task must instantiate and start `CallbackServer` from `_run_bot` (or
equivalent) with the platform-specific routes registered, before any push/OAuth/webhook
path can be exercised outside of tests.

### Scheduled Event synchronization has no production call site

`src/krubit/discord/scheduled_events.py`'s `ScheduledEventSynchronizer` — creating,
updating, and exact-ID-recovering a Krubit-owned Discord Scheduled Event for a
supported scheduled stream — is fully implemented and tested
(`tests/test_scheduled_event_sync.py`), including restart-safe recovery that never
falls back to searching by mutable event name. However, nothing in
`src/krubit/discord/bot.py`, `src/krubit/__main__.py`, or
`src/krubit/discord/content_runtime.py` ever constructs a `ScheduledEventSynchronizer`
or calls `.apply(...)` on one. **Practical consequence:** `scheduled_event_mappings`
is never written by the running process, so `/fetch schedule` will always report "No
Krubit-owned Scheduled Events," regardless of what content is enrolled, routed, or
scheduled on any platform. A future task must call `ScheduledEventSynchronizer.apply`
from the same place `ContentRuntime.apply_plan` is called (or an equivalent hook) for
every content event carrying schedulable state, before this capability does anything
in production.

### `X_KRUBIT_BEARER_TOKEN` is the only currently reachable OAuth-free social connector

Of the platforms wired into the scheduler (YouTube, X, Bluesky), only X requires a
credential Krubit reads directly from `Settings` (`X_KRUBIT_BEARER_TOKEN`) with no
per-creator OAuth step; YouTube uses a single API key the same way. Neither requires
the callback server. Both are the connectors an operator can realistically exercise
end-to-end today, alongside the Twitch/Discord-presence path from Phase 2A.

## Notification policy: guild configuration is not wired yet

**This is a known, currently-open limitation, separate from the three production gaps
called out at the top of this guide** — it does not block `KRUBIT_SOCIAL_DELIVERY_ENABLED`
from being genuinely enforced (see above); it means that once delivery is enabled, every
guild gets the same maximally-permissive policy rather than one it can configure. It has
carried forward through the final review of this build without a fix, deliberately: a
minimal guild-config mechanism was judged out of scope for a review-fix pass, and this
section exists so it is never mistaken for resolved.

`ContentRuntime` uses a `policy_factory` that currently defaults every guild to
**unlimited mention budgets and no quiet hours** — there is no guild-level
`NotificationPolicy` configuration storage yet (`NotificationPolicy` itself, quiet
hours, and mention-budget math are fully implemented and unit-tested in
`src/krubit/services/notification_policy.py`/`tests/test_notification_policy.py`, but
nothing yet persists a guild's chosen quiet-hours window or budget limits and loads
them into that factory).

**This must be replaced with real guild-config wiring before general availability.**
Until then, every guild that enables `KRUBIT_SOCIAL_DELIVERY_ENABLED` gets the
unlimited-budget/no-quiet-hours default, not whatever quiet hours or budgets an
operator might assume are configurable today. Do not advertise quiet hours or mention
budgets as configurable to end users until this wiring exists.

## URL enrollment

```text
/fetch creator add <url>
```

Recognizes a supported profile URL host and path exactly (see
`src/krubit/integrations/catalog.py`'s `recognize_account_url`; also documented per
platform in the [platform developer setup](#platform-developer-setup) section above),
normalizes it to a canonical HTTPS URL, and presents a private (ephemeral)
confirmation before activation — it never announces the account publicly at enrollment
time, and baselines existing content silently rather than treating history as new.

Authority: administrators may add/remove/pause/resume/transfer/route/template any
account in the guild; a member with the `Creator` role may only manage accounts they
own. `/fetch creator verify <account_id>` shows the platform's static baseline
capability declaration for that account (not a live per-account authorization check —
no connector instance is wired into the command layer for a live check yet).

## Shadow, preview, and canary controls

1. **Shadow**: leave `KRUBIT_SOCIAL_DELIVERY_ENABLED=false` (and, for live,
   `KRUBIT_LIVE_SIGNALS_ENABLED=false`) while `KRUBIT_CREATOR_SIGNALS_ENABLED=true`.
   Enrollment, connector polling where wired (YouTube/X/Bluesky/Twitch), and cursor
   advancement can proceed, but no public Discord delivery occurs.
2. **Staff preview**: `/fetch notifications preview` renders an ephemeral card for a
   registered account's next-would-be delivery without sending anything publicly or
   consuming a mention budget.
3. **Controlled canary**: enable only the one connector's shadow flag, baseline an
   approved test account, publish or schedule one controlled item on that platform,
   verify exactly one normalized event and zero public deliveries appear, then
   authorize preview, then one production delivery. Record: external content ID,
   detection time, route ID, Discord message/event ID, mention decision, delivery
   receipt, the lifecycle edit (if the item transitions), and the cleanup result —
   never secrets or full private payloads. This build's [completion audit](phase-2-completion-audit.md)
   records that no such live canary has run in this development session and marks it
   an operator action for post-merge, credentialed environments.

Retry, retraction, and status:

```text
/fetch notifications status
/fetch notifications retry <delivery_id>
/fetch notifications retract <delivery_id>
/fetch latest
/fetch schedule
/fetch integrations
```

## Quota, expiry, and failure remediation

- **Quota-limited** (`CapabilityState.QUOTA_LIMITED`): YouTube and X connectors report
  `retry_after_seconds` computed from the real quota-reset time (midnight Pacific for
  YouTube; the `x-rate-limit-reset` header for X) — do not manually retry sooner than
  that; the scheduler's own backoff already respects it.
- **Authorization required / expired** (`CapabilityState.AUTHORIZATION_REQUIRED`): an
  OAuth grant is missing, expired, or revoked. Revocation and expiry move only the
  affected capability to `authorization_required`; unrelated connectors on the same
  account or other accounts are unaffected. Re-run the platform's OAuth consent flow
  once the callback server is wired (see the known-limitations section above); until
  then this state is expected and cannot be cleared for Meta/TikTok accounts.
- **Degraded**: a connector call failed in a way that is not quota/authorization —
  check `/fetch creator show <account_id>` for delivery counts and cursor staleness;
  retry after the underlying condition (network, malformed upstream response) clears.
- **Cursor missing/stale**: `/fetch creator show` reports "No cursor yet" or a stale
  `updated_at`. A missing cursor is expected right after enrollment before the first
  successful poll; a cursor stale beyond 26 hours (`HealthService.creator_health`'s
  `_CURSOR_STALE_AFTER`) indicates the connector has stopped advancing and should be
  investigated as a connector or scheduler failure, not manually reset.
- **Invalid/deleted account**: paused automatically with a staff-visible finding per
  the design doc; use `/fetch creator show`/`/fetch creator list` to locate it, then
  `/fetch creator resume` only after confirming the underlying account is valid again.

## Rollback

1. Set `KRUBIT_SOCIAL_DELIVERY_ENABLED=false` (and `KRUBIT_CREATOR_SIGNALS_ENABLED=false`
   if connector polling itself must stop) in the master `.env`.
2. Restart with `& scripts/invoke-krubit.ps1 run`.
3. Confirm `/fetch integrations`, `/fetch notifications status` report the expected
   disabled state and no new public delivery occurs.
4. Keep the database, WAL, and SHM files — the content ledger, correlation, delivery,
   and mention-budget tables are additive and safe to retain during rollback, exactly
   like Phase 2A's tables.
5. If a code regression rather than configuration requires rollback, restart the last
   accepted commit through the same launcher and preserve the database and logs.
   Phase 1 and Phase 2A remain the fallback operational surface.

## Data deletion

- `/fetch creator remove <account_id>` pauses (stops new monitoring for) an account
  while preserving bounded receipts under the guild retention policy, per the design
  doc — it does not itself purge historical receipts.
- OAuth grants are stored separately from public registry records in
  `CredentialVault`-sealed form; revoking a grant moves the affected capability to
  `authorization_required` without deleting unrelated data.
- For a full data-deletion request (a creator or member request, or Discord's own
  deletion requirement), follow the existing [Privacy Policy](../PRIVACY_POLICY.md)
  section 10 process: email `krucial.studios.llc@gmail.com` with the Discord user ID
  and guild ID. There is no dedicated `/fetch` command yet that purges a creator
  account's historical content-ledger rows or delivery receipts; an operator must
  perform that deletion directly against the SQLite database (`content_events`,
  `content_delivery_attempts`, `creator_registry_receipts`, and related tables scoped
  by `guild_id`/`account_id`) until a dedicated deletion command exists.
- Never delete `data/krubit.db`, its WAL, or its SHM file as a substitute for a scoped
  deletion — that would also destroy unrelated Phase 0/1/2A records for every guild.

## Connector-specific limitations summary

| Platform | Scheduled in this build | Live capability | Notable limitation |
|---|---|---|---|
| Twitch | Yes (Discord presence + Helix) | Operational (Phase 2A) | Discord presence only; no private connected-account reads |
| YouTube | Yes | `unconfigured` until API key set; push not reachable (callback server not wired) | Duplicated uploads-playlist extraction between `resolve_account`/`_uploads_playlist`; `extract_streaming_observation` has no production call site yet |
| X | Yes | `unsupported` (no promised live surface) | — |
| Bluesky | Yes | `unsupported` | Watermark can be a foreign-author URI when the newest feed entry is a repost (theoretical missed stop-point); quote-posts intentionally unfiltered by product decision |
| Instagram | No — not scheduled (see above) | `approval_required` | OAuth grant flow implemented but callback server not wired; no production call site |
| Facebook Pages | No — not scheduled | `authorization_required` | `fetch_page` always returns `next_cursor=None`, re-polls 3 bounded edges rather than paginating |
| Facebook profiles | No — not scheduled | `unsupported` unless an approved API exposes it | — |
| Threads | No — not scheduled | `unsupported` | — |
| TikTok | No — not scheduled | `approval_required` (LIVE embed insufficient) | `_consumed_nonces` grows unboundedly, never pruned after TTL; non-numeric cursor raises unhandled `ValueError` |
| Fanbase | Dormant by design | `unsupported` (pending official API/partner access) | URL recognition only; never counted as an operational canary |

## Cross-guild and authority boundaries

Cross-guild reads/mutations and unauthorized creator management are denied at the
service layer (`src/krubit/services/creator_registry.py::_require_authority` and
guild-scoping in every store method) and covered by
`tests/test_creator_registry_service.py`. `notification_preview`
(`src/krubit/discord/content_commands.py`) now applies the same `_require_authority`
gate as every other per-account command — only the account's owner or an admin may
preview its card — closing the previously-open gap where any guild member could
preview any account's canonical URL and mention role
(`tests/test_content_commands.py::test_notification_preview_denies_a_non_owning_non_admin_actor`).

## Related documents

- [Phase 2A live-stream signal operations](phase-2a-live-stream-signals.md)
- [Phase 2 completion design doc](../superpowers/specs/2026-08-04-phase-2-completion-design.md)
- [Phase 2 completion audit](phase-2-completion-audit.md)
- [Privacy Policy](../PRIVACY_POLICY.md)
