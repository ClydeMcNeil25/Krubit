# Phase 2A Live Stream Signals Design

## Purpose

Phase 2A gives Krubit an automatic, non-conversational live-stream sense. Krubit observes the streaming presence that Discord already publishes for server members, enriches Twitch streams through the official Twitch API, posts one functional live card, and maintains a temporary `Streaming Now` role. Creators do not register a Twitch username with Krubit and do not authorize Krubit to inspect their Discord connections.

This is the first bounded slice of Phase 2. YouTube feeds, scheduled streams, new-video feeds, other social platforms, creator dashboards, quiet hours, campaign analytics, and cross-platform campaign deduplication remain later Phase 2 work.

## Approved Experience

When a non-bot server member begins exposing a Discord activity of type `Streaming` with a valid Twitch URL, Krubit will:

1. recognize the Discord presence transition automatically;
2. identify the Twitch login from the validated activity URL;
3. immediately add the existing `Streaming Now` role to that Discord member;
4. request public stream details from Twitch Helix;
5. post exactly one live announcement in `#live-notifications`;
6. mention `@everyone` once for that stream;
7. track the active stream until the termination rules determine that it has ended; and
8. remove only the `Streaming Now` role, preserving every other member role.

Krubit will not silently enumerate private Discord connections. Discovery depends on Discord actually showing the member's streaming activity. A member who is invisible, has activity sharing disabled, or does not expose a Discord `Streaming` activity cannot be discovered automatically through this path.

## Approved Notification Card

The public message content uses Krubit's fictional creature language while leaving the Discord mention and Twitch display name readable:

`⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone {twitch_display_name} ⌇⊑⏃ ⌰⟟⎐⏃!`

Its intended meaning is: “When you get the chance, @everyone, `{twitch_display_name}` is now live!” The English translation is design documentation only and is not included in the Discord post.

The purple-accented embed follows the approved creature signal card:

- `LIVE SIGNAL FOUND` heading with Krubit's crystal motif;
- `Krubit detected a creator streaming` functional subtitle;
- Twitch display name and platform;
- stream title;
- Twitch category;
- `Streaming Now` status;
- Twitch-provided live thumbnail when available;
- `Fetch the Stream` link button; and
- `Automated creature signal • Twitch` footer.

Krubit's existing Discord bot avatar supplies his creature appearance beside the message. External creator text is confined to escaped message/embed fields. `allowed_mentions` explicitly permits the intended `@everyone` ping while preventing creator names, stream titles, or other external text from creating additional user or role mentions.

## Zariya and Krubit Boundary

Krubit owns the automatic mechanics:

- observing Discord streaming presence;
- validating and enriching Twitch stream state;
- posting the approved functional card;
- assigning and removing `Streaming Now`;
- deduplication, recovery, receipts, and integration-health facts.

Zariya remains the human Community Manager. She owns conversational celebration, campaign tone, special-event framing, creator relationships, follow-up discussion, and exceptions requiring human judgment. An ordinary live signal does not require KSHQ or Zariya approval.

## Architecture

The Discord adapter receives presence updates and converts only relevant streaming activities into framework-independent observations. It does not retain unrelated games, music, custom statuses, or other member activities.

A Twitch URL parser accepts Discord-validated Twitch streaming URLs, normalizes the channel login, and rejects unsupported hosts or malformed paths. A Twitch client obtains and caches an app access token through the client-credentials flow, queries Helix for public stream details, refreshes expired application tokens, observes rate-limit headers, and exposes typed success, offline, unavailable, and malformed responses.

A `LiveSignalService` owns the state machine and is independent of Discord and HTTP objects. Its stored states are:

- `detected`: Discord has exposed a Twitch streaming activity;
- `live`: the role and announcement have been applied;
- `ending`: one source no longer reports the stream and reconciliation is in progress;
- `ended`: Twitch is offline or the Discord signal is gone beyond the bounded recovery window;
- `failed`: a required Discord action could not be completed and the failure is visible to staff.

Separate delivery and role adapters perform Discord mutations. The service issues idempotent intents such as “announce this stream,” “add the streaming role,” and “remove the streaming role.” Adapters return durable receipts rather than deciding state themselves.

## Data and Identity

SQLite adds guild-scoped records for:

- active live-signal sessions;
- Discord member ID to observed Twitch login for the lifetime of the detected session;
- Twitch stream ID when Helix supplies it;
- announcement channel and message IDs;
- role-add and role-remove receipts;
- Helix checks, retries, and last successful reconciliation; and
- exactly-once delivery keys.

The preferred announcement identity is `(guild_id, twitch_stream_id)`. Before Helix supplies a stream ID, the service uses a deterministic presence-session key derived from guild, Discord member, normalized Twitch login, and the Discord activity start time when present. When Helix enrichment arrives, the provisional identity is merged into the Twitch stream identity without sending a second announcement.

No Twitch password, stream key, creator OAuth token, Discord connection list, unrelated presence activity, or message content is stored. The Twitch Client Secret remains only in the master `.env` and never enters SQLite, logs, cards, or receipts.

## Detection and Reconciliation Flow

On a transition into Twitch streaming presence, Krubit adds `Streaming Now` and starts Helix enrichment. The first announcement waits no more than five seconds for Twitch: if Helix succeeds within that budget, the full enriched card is posted; otherwise, Krubit posts a reduced card from the Discord activity and records the limitation. When Twitch details recover and the original Discord message still exists, Krubit edits that message in place. Recovery never produces a second `@everyone` ping.

Repeated Discord presence updates, status changes, reconnect replays, title changes, and Helix polling for the same stream reuse the stored session. They may refresh card fields but cannot create another announcement or role assignment receipt.

Active sessions are reconciled through Helix every 60 seconds. If the Discord activity temporarily disappears but Helix still reports the stored Twitch stream as live, Krubit preserves `Streaming Now` and the active session. If Helix explicitly reports the channel offline, Krubit ends the session and removes `Streaming Now`. If both Discord presence and Twitch are temporarily unavailable, Krubit preserves the role for a bounded five-minute recovery window, then removes it and records that the end was inferred from missing evidence.

At startup or gateway resume, Krubit reconciles stored active sessions before issuing new public messages. Stored Twitch stream IDs and existing announcement message IDs prevent duplicate announcements across process restarts. Current Discord streaming presences are then evaluated for streams that began while Krubit was offline.

If a member leaves the server, the session is closed without attempting a role edit. If a member changes from one Twitch URL to another, the prior session is reconciled and a new stream is announced only when it has a distinct Twitch stream ID or a distinct presence-session identity.

## Discord Access and Configuration

Phase 2A enables the privileged `GUILD_PRESENCES` intent in code. The Presence Intent must also be enabled on Krubit's Bot page in the Discord Developer Portal. The existing Guilds and Guild Members intents remain enabled.

Krubit requires these Discord permissions for this slice:

- View Channel;
- Send Messages;
- Embed Links;
- Read Message History;
- Mention `@everyone`, `@here`, and All Roles; and
- Manage Roles.

The existing Krubit role must remain above the existing `Streaming Now` role. Krubit never edits, replaces, snapshots, or restores the member's other roles as part of this workflow. The configured `#live-notifications` channel and `Streaming Now` role are resolved to Discord IDs and stored per guild so renames do not break routing.

Startup preflight checks the presence intent, Twitch credentials, destination channel, role hierarchy, channel send/embed/mention access, and role-management access. A missing capability is reported through health checks and receipts. If the live card can be sent but the `@everyone` permission is absent, Krubit sends the card without an effective mass mention and records a degraded-delivery finding rather than dropping the announcement.

## Twitch Access

Phase 2A uses the registered confidential Twitch application and the existing master-environment values:

- `TWITCH_KRUBIT_CLIENT_ID`;
- `TWITCH_KRUBIT_CLIENT_SECRET`.

Krubit uses Twitch's app access token and public Helix stream lookup. Individual creators do not supply Twitch credentials or authorize Krubit. Token values are held in memory, refreshed as required, validated according to Twitch requirements, and redacted from all errors and logs.

## Commands and Staff Visibility

Phase 2A adds a small staff-only `/fetch live` command group:

- `/fetch live status` shows the active sessions, last Discord signal, last Helix result, role state, and delivery state;
- `/fetch live test` renders a private preview without assigning a role or mentioning `@everyone`; and
- `/fetch live reconcile` requests an idempotent reconciliation of active sessions.

These commands are guild-only, ephemeral, restricted to members with Manage Guild, and receipted. The existing `/fetch permissions`, `/fetch integrations`, and `/fetch server-health` surfaces gain factual Phase 2A checks.

## Error Handling and Safety

- Twitch timeouts, rate limits, invalid responses, and expired tokens use bounded retry with jitter and produce redacted receipts.
- Discord presence remains the automatic discovery trigger; Twitch enrichment failure cannot expose private data or create a duplicate announcement.
- Discord `Forbidden` and `NotFound` responses identify the failed channel, role, member, or message by safe ID and become staff-visible findings.
- Announcement, role-add, message-edit, and role-remove operations are independently idempotent.
- Only the dedicated `Streaming Now` role is mutated.
- A role already present before a newly detected session is recorded as pre-existing and is not removed by that session unless Krubit has an earlier active-session receipt proving that Krubit assigned it.
- External strings are escaped, length-limited to Discord constraints, and excluded from mention parsing.
- No test or preview command can produce a real `@everyone` ping.

## Testing and Rollout

Implementation follows test-first cycles. Unit and adapter tests cover:

- Discord activity-type and Twitch-URL filtering;
- ignoring unrelated presence activities and bot users;
- provisional identity merging into Twitch stream IDs;
- exactly-once announcements across duplicate events, reconnects, and restarts;
- safe `allowed_mentions` behavior;
- full and reduced card rendering within Discord limits;
- immediate role assignment and preservation of all other roles;
- pre-existing `Streaming Now` role ownership;
- Twitch token, timeout, rate-limit, offline, and malformed-response paths;
- active-session reconciliation and the five-minute missing-evidence window;
- message enrichment by edit without a second ping;
- guild isolation and authorization for every command; and
- secret redaction in settings, logs, errors, database records, and receipts.

Live rollout proceeds in this order:

1. enable the Presence Intent in the Discord Developer Portal;
2. confirm the master Twitch credentials without displaying them;
3. resolve and store the `#live-notifications` channel and `Streaming Now` role IDs;
4. preflight `Manage Roles`, role hierarchy, and `Mention @everyone` in the destination channel;
5. deploy presence detection with public delivery disabled and verify a real Discord Streaming transition;
6. run the private live-card preview;
7. enable one canary public announcement and confirm exactly one `@everyone` ping;
8. confirm `Streaming Now` is added without changing other roles;
9. end the canary stream and confirm the role is removed correctly;
10. restart Krubit during a second canary stream and verify that no duplicate announcement is created; and
11. review health and delivery receipts before enabling the workflow generally.

## Acceptance Criteria

Phase 2A is complete when an eligible Discord member requires no manual creator registration; one Twitch streaming presence produces one public creature-language card and one mass mention; Twitch details enrich the card without delaying the signal indefinitely; repeated events, reconnects, message edits, and restarts cannot duplicate the announcement; `Streaming Now` is assigned and removed without touching any other role; failures are visible and secrets remain redacted; previews cannot ping the server; supported commands are staff-only and receipted; and automated plus live canary tests pass without disrupting Zariya.
