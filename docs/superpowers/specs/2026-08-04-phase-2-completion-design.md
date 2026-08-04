# Krubit Phase 2 Completion Design

**Date:** 2026-08-04  
**Status:** Approved for implementation planning  
**Scope:** Complete the Creator Signal and Notification Hub beyond the delivered Phase 2A Twitch slice

## Purpose

Complete Phase 2 as a multi-platform creator notification system without weakening
Krubit's non-conversational companion identity, Discord-native boundaries, auditability,
or multi-guild isolation. Krubit must accept creator account links, monitor supported
official platform interfaces, classify live and non-live content, route durable cards,
prevent cross-post floods, synchronize scheduled streams, and report unsupported or
unconfigured capabilities honestly.

Phase 2A remains the production baseline: Discord Streaming presence for Twitch,
Twitch verification, `Streaming Now` role ownership, one durable live card,
restart recovery, and staff-only live controls.

## Product Decisions

- Use a link-first creator enrollment experience backed by explicit platform adapters.
- Permit administrators to manage any creator and members with the configured Creator
  role to manage only their own creator profile.
- Detect both Twitch and YouTube live activity from public Discord Streaming presence.
- Use registered platform monitoring to cover uploads, scheduled streams, richer
  verification, and cases where Discord presence is unavailable.
- Route active livestreams to `#live-notifications`.
- Route posts, uploads, Shorts, Reels, and ordinary videos to
  `#social-notifications`.
- Apply the approved Krubit alien-language `@everyone` live policy to every verified
  livestream, subject to quiet hours and mention budgets.
- Do not use `@everyone` for social content by default.
- Build all connector-ready functionality now; credentials and external platform
  approvals may be added later.
- Never use unofficial scraping or browser automation to simulate an unavailable API.
- Treat Fanbase and platform live surfaces without a reliable official detection API as
  pending capabilities rather than operational connectors.

## Architecture

```text
submitted creator URL
        |
platform URL recognizer and normalizer
        |
creator profile + owned platform account
        |
official platform connector
        |
normalized content event
        |
classification + cross-platform correlation
        |
routing, quiet-hours, batching, and mention policy
        |
durable Discord delivery or Scheduled Event update
        |
receipt + operational analytics
```

### Creator Registry

The registry associates a Discord member with one or more approved external accounts in
one guild. Account identity is based on a stable platform identifier after resolution,
not a mutable display name. Public profile URLs, normalized canonical URLs, ownership,
authorization state, routes, templates, and pause state are stored separately from
secrets.

A platform account cannot silently belong to multiple members in the same guild. Staff
may transfer ownership through an explicit audited action. Removing an account stops new
monitoring while preserving bounded receipts under the guild retention policy.

### Connector Catalog and Adapters

The catalog recognizes supported URL families and declares each adapter's capabilities,
credential requirements, authorization mode, polling or webhook behavior, quota model,
and health. Adapters resolve an account, validate access, retrieve or receive content,
persist a cursor, and emit normalized content events. Platform-specific response shapes
do not enter routing or Discord rendering code.

### Normalized Content Event

Every event includes:

- guild, creator, account, platform, and stable external identifiers;
- canonical URL and content type;
- lifecycle state: scheduled, delayed, live, ended, cancelled, published, corrected,
  retracted, or failed;
- title or bounded excerpt, publication/schedule timestamps, and optional category;
- platform-provided preview and attribution metadata;
- correlation evidence for cross-platform deduplication;
- source cursor or webhook identity and observation time;
- capability and confidence facts without inferred sentiment or content quality.

Full post bodies are not required for durable identity or analytics. Storage must retain
only the bounded content necessary to render, correct, audit, and deduplicate a delivery.

### Policy and Delivery

One policy engine owns destination selection, mention behavior, quiet hours, batching,
creator overrides, and retry eligibility. One delivery engine owns idempotent sends,
edits, corrections, cancellations, retractions, recovery, and Discord receipts.

The existing Twitch implementation must migrate behind these shared contracts without
changing its accepted presentation, Discord-presence behavior, or role-ownership safety.

## Platform Capability Matrix

| Platform | Account enrollment | Social content | Live content |
|---|---|---|---|
| Twitch | Public URL and/or Discord presence | Not in scope | Discord presence plus Twitch API verification |
| YouTube | Public channel URL | Videos and Shorts | Discord presence plus API push/polling for scheduled/live/end states |
| X | Public URL plus Krubit application access | Original posts | No promised live surface |
| Instagram | Professional account authorization | Posts and Reels | Authorized live media when official access exposes it during broadcast |
| Facebook Pages | Page authorization | Posts, videos, and Reels | Authorized Page live broadcasts |
| Facebook profiles | Owner authorization and approved Meta access | Authorized owner content only | No promised live surface unless an approved API exposes it |
| Threads | Creator OAuth | Original Threads posts | No promised live surface |
| Bluesky | Public URL; no member OAuth required for public reads | Original public posts | No promised live surface |
| TikTok | Creator OAuth and approved Display API access | Authorized uploaded videos | Pending reliable TikTok detection access; LIVE embed alone is insufficient |
| Fanbase | URL recognition | Pending official API or partner access | Pending official API or partner access |

Connector capability is explicit per account and per content class. A platform can be
ready for uploads while its live capability remains unsupported or approval-required.

## Registration and Authorization

### Link-first flow

`/fetch creator add <url>` recognizes the platform, normalizes the URL, resolves the
stable account when possible, checks caller authority, and presents a private
confirmation preview before activation.

### Authority

- Administrators may add, remove, pause, resume, transfer, route, or template any guild
  creator account.
- Members with the configured Creator role may manage only accounts owned by their own
  Discord member identity.
- Public-read adapters may activate after account validation.
- OAuth adapters remain `authorization_required` until the creator completes consent.
- Staff cannot paste user access tokens into Discord commands.
- All ownership, authorization-state, and route changes produce redacted audit receipts.

### Secret handling

OAuth grants and application secrets are stored separately from public registry records.
Tokens never appear in commands, Discord cards, logs, exports, connector diagnostics, or
creator analytics. Revocation and expiry move only the affected capability to
`authorization_required` and do not disable unrelated connectors.

## Classification and Routing

### Live workflow

Active Twitch, YouTube, Facebook Page, officially observable Instagram, approved TikTok,
and future Fanbase broadcasts route to `#live-notifications`. Verified starts use the
approved Krubit alien-language line and `@everyone`, constrained by the live mention
budget and quiet-hours policy. State transitions edit the same card.

Scheduled livestreams may create a Krubit-owned external Discord Scheduled Event.
Delayed, active, completed, and cancelled states update the exact stored event ID.
Krubit never mutates an event it does not own.

### Social workflow

YouTube videos and Shorts, X posts, Instagram posts and Reels, Facebook/Page posts,
videos and Reels, Threads posts, Bluesky posts, TikTok videos, and future supported
Fanbase content route to `#social-notifications`.

Social delivery has no `@everyone` by default. A guild may configure no mention or one
approved role mention per route. Unsupported content classes remain visible in health
and are never described as monitored.

### Default exclusions

- Ignore replies and comments.
- Ignore reposts and ordinary shares.
- Ignore quote posts by default; allow an explicit per-route opt-in for substantial
  original quote text.
- Ignore ephemeral Stories unless an official interface offers stable, policy-compliant
  access and the capability is explicitly enabled.
- Baseline existing content during initial enrollment without announcing it.

## Cross-platform Correlation

Exact platform IDs and canonical URLs are authoritative duplicates. Cross-platform
correlation may also use creator ownership, observation windows, normalized outbound
links, normalized titles, platform-provided source relationships, and media fingerprints
when legally available.

Exact duplicates produce one delivery. Strong campaign matches may produce one card with
multiple platform buttons. Ambiguous matches remain separate; Krubit must not suppress a
legitimate post based on weak similarity. Every suppression or merge records its reason
and contributing event IDs.

## Cards and Notification Policy

### Live card

- Krubit's alien-language announcement and permitted mention;
- creator, platform, title, category or topic, and live state;
- large platform preview and direct watch button;
- optional additional platform buttons for a strongly correlated simulcast.

### Social card

- creator, platform, and content type;
- title or safely bounded excerpt;
- platform-provided thumbnail or preview;
- publication timestamp and direct view/watch button;
- preserved attribution without generated Krubit commentary.

### Defaults

- Live: immediate, not batched, `@everyone`, and priority bypass of social quiet-hour
  queues unless the guild explicitly disables bypass.
- Social: no `@everyone`, short correlation/batching window, queue during quiet hours.
- No fallback delivery into an unrelated channel.
- Failed deliveries remain retryable and alert staff without repeatedly mentioning the
  community.

Quiet hours use the guild timezone. Mention budgets are separate for live and social
routes and record every consumed, suppressed, or bypassed mention.

## Scheduled Event Synchronization

Krubit creates events only for registered creators and supported scheduled streams. The
mapping stores guild ID, creator/account ID, platform content ID, Discord event ID, and
ownership receipt. Safe updates include title, description, external URL/location,
start/end time, state, and cover when supported. Restart recovery reconciles the exact
mapping and never searches by mutable event name alone.

## Commands

The Phase 2 surface includes:

- `/fetch live`
- `/fetch latest`
- `/fetch schedule`
- `/fetch creator add|remove|list|show|verify|pause|resume|route|template`
- `/fetch integrations`
- `/fetch notifications`
- `/fetch notification preview|retry|retract`

Staff-only operations remain guild-only, authorization-checked, and ephemeral unless
they explicitly perform an approved public delivery. Creator self-service commands can
only view or mutate the caller's own creator profile.

## Analytics

The operational ledger records detections, deliveries, suppressions, merges, failures,
retries, retractions, latency, quota state, and connector health. Available authorized
platform metrics and Discord link interactions may be captured as factual snapshots.
Tracked redirects must be disclosed and configurable.

Krubit does not infer sentiment, quality, creator value, or member intent. Detailed
guild analytics are staff-only; creators may view facts scoped to their own registered
accounts.

## Capability and Failure States

Every connector capability reports one of:

- `ready`
- `unconfigured`
- `authorization_required`
- `approval_required`
- `degraded`
- `quota_limited`
- `unsupported`

Invalid/deleted accounts pause with a staff-visible finding. Outages, malformed
responses, quota limits, and expired authorization are isolated to the affected adapter
and account. Polling uses durable cursors, bounded concurrency, backoff, and jitter.
Webhook/push payloads require supported signature or verification checks before event
ingestion. Malformed events enter a redacted diagnostic receipt rather than delivery.

## Implementation Slices

1. Shared creator registry, account ownership, URL catalog, and capability model.
2. Normalized content ledger, correlation records, routes, policies, and durable
   multi-platform delivery.
3. YouTube presence, uploads, push/poll recovery, and scheduled/live lifecycle.
4. Social cards, quiet hours, mention budgets, batching, and delivery controls.
5. X, Instagram, Facebook, Threads, Bluesky, and TikTok adapters.
6. Discord Scheduled Event synchronization.
7. Creator, content, integration, notification, schedule, and analytics commands.
8. Fanbase dormant adapter and explicit pending capability behavior.
9. Existing Twitch migration behind shared contracts.
10. Shadow, preview, canary, documentation, and completion audit.

Each slice requires focused tests and a commit before the next slice assumes its
contracts.

## Testing and Rollout

Automated tests must cover URL validation, ownership boundaries, cross-guild isolation,
baseline suppression, content lifecycle transitions, cursor recovery, webhook
verification, exact and probabilistic deduplication, quiet hours, mention budgets,
batching, role ownership, Scheduled Event ownership, permission loss, token expiry,
quota limits, outages, retries, corrections, retractions, and durable receipts.

Recorded official response fixtures and synthetic events are used before credentials
exist. New connectors begin disabled, then progress through shadow mode, staff preview,
and controlled production canary after credentials and approvals become available.
Enabling one connector does not enable any other platform or content class.

## Completion Gate

Phase 2 is complete only when evidence proves all of the following:

- The full registry, connector, normalized event, policy, delivery, analytics, and
  command architecture is implemented and tested.
- Every platform and content capability reports an honest operational state.
- Twitch and YouTube Discord-presence detection are durable and duplicate-safe.
- Configured official APIs detect, classify, and route new content correctly without
  announcing enrollment history.
- Strong cross-platform duplicates do not flood Discord, while ambiguous content is not
  silently suppressed.
- Live roles, announcements, and Scheduled Events recover correctly after restarts.
- Quiet hours, batching, routes, and mention budgets are enforced and receipted.
- Connector failures are visible, isolated, and retryable where safe.
- Controlled `#live-notifications` and `#social-notifications` canaries pass for the
  capabilities whose credentials and platform approvals are available.
- Cross-guild reads/mutations and unauthorized creator management are denied.
- Fanbase and unavailable live APIs remain clearly pending and are not counted as
  operational canaries.

External approval that a platform has not granted does not justify false emulation. The
software capability is complete when its adapter, authorization flow, health state,
fixtures, and dormant behavior are implemented; production readiness for that capability
requires a separate successful official-API canary.

## Explicit Exclusions

- No unofficial scraping, browser automation, or credential sharing.
- No monitoring private or unowned social content.
- No replies, comments, DMs, or ephemeral content by default.
- No AI-generated commentary, sentiment, creator ranking, or content-quality judgment.
- No modification of non-Krubit Discord Scheduled Events.
- No claim that pending Fanbase, TikTok LIVE, or restricted Meta access is operational.

## Official Capability References

- Discord Gateway activity objects: <https://docs.discord.com/developers/events/gateway-events>
- Discord Scheduled Events: <https://docs.discord.com/developers/resources/guild-scheduled-event>
- YouTube push notifications: <https://developers.google.com/youtube/v3/guides/push_notifications>
- X user posts: <https://docs.x.com/x-api/users/get-posts>
- Meta Instagram API: <https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api>
- Meta Threads API: <https://www.postman.com/meta/threads/overview>
- Bluesky author feed: <https://docs.bsky.app/docs/api/app-bsky-feed-get-author-feed>
- TikTok Display API: <https://developers.tiktok.com/doc/display-api-overview/>
- TikTok LIVE embed limitation: <https://developers.tiktok.com/doc/embed-live>
