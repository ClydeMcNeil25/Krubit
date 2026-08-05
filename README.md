# Krubit

Krubit is Zariya's non-conversational Discord pet and operational companion. He fetches
functional system results, records auditable events, and provides a safe foundation for later
creator notifications, server monitoring, Entry Sniffing, and member activity measurement.

## Phase 1 capabilities

- Guild-isolated SQLite configuration, events, and action receipts
- Idempotent event ingestion
- Recursive credential redaction before durable storage or signal output
- Least-privilege Discord installation URL
- Guilds and Server Members Gateway intents for factual join/leave collection
- Member, role, channel, permission, webhook, AutoMod, and Scheduled Event change records
- Stable configuration snapshots with integrity hashes and human-readable differences
- Factual server, permission, and integration health checks
- `/fetch status` for server-scoped health
- Manage-Guild-only `/fetch test-card`
- Staff-only `/fetch server-health`, `/fetch changes`, `/fetch permissions`, and
  `/fetch integrations`
- Staff-only `/fetch backup status`, `/fetch backup create`, and non-mutating
  `/fetch backup preview`
- Once-daily health summaries with explicit private-channel opt-in
- Versioned `krubit.zariya-signal.v1` foundation test signal
- Administrative CLI for initialization, guild enablement, status, and smoke testing

Phase 1 does not moderate members, profile members, poll creator platforms, read message
content, or mutate Discord server configuration.

## Phase 2A capabilities

- Twitch live-signal detection from Discord's public Streaming presence activity
- Durable, guild-scoped live sessions, delivery claims, checks, and action receipts
- One configured `Streaming Now` role, assigned and removed only when Krubit has a
  receipt proving that Krubit assigned it
- One configured `#live-notifications` destination with a receipted, degradation-aware
  announcement path
- Staff-only `/fetch live status`, `/fetch live test`, and `/fetch live reconcile`

Discord can detect a Twitch stream only when the member chooses to share a Streaming
activity publicly in Discord; Krubit does not read private connected accounts.

## Phase 2 capabilities

- A guild-scoped creator registry (`/fetch creator add|remove|list|show|verify|pause|resume|route|template`)
  with owner/admin authority boundaries and redacted audit receipts
- A per-platform connector catalog covering Twitch, YouTube, X, Instagram, Facebook
  Pages, Facebook profiles, Threads, Bluesky, TikTok, and Fanbase, each reporting an
  honest `ready`/`unconfigured`/`authorization_required`/`approval_required`/
  `degraded`/`quota_limited`/`unsupported` state per capability
- A normalized, idempotent content ledger with cross-platform correlation (exact and
  probabilistic deduplication that never silently suppresses ambiguous content)
- One shared notification policy (quiet hours, mention budgets) and one shared durable
  Discord delivery engine, covering both `#live-notifications` and the new
  `#social-notifications` destination
- Discord Scheduled Event synchronization for supported scheduled streams, recovering
  only by exact stored event ID
- `/fetch latest`, `/fetch schedule`, `/fetch notifications`, and
  `/fetch notification preview|retry|retract`
- The Phase 2A Twitch/Discord-presence path migrated behind these same shared contracts
  without changing its accepted presentation or safety guarantees

Every Phase 2 surface defaults off: `KRUBIT_CREATOR_SIGNALS_ENABLED` and
`KRUBIT_SOCIAL_DELIVERY_ENABLED` both default to `false`, and every missing per-platform
credential simply leaves that capability at `unconfigured` rather than blocking
startup. Instagram, Facebook, Threads, and TikTok connectors are fully implemented and
tested but are **not wired into the polling scheduler in this build** — they need
per-account OAuth credential resolution that is not yet built, so scheduling them with
one shared bot-wide token would fetch every creator's content under one account's
credentials. See the
[Phase 2 operations guide](docs/operations/phase-2-creator-signal-hub.md) for the full
operator runbook, including this and the OAuth-callback-server gap, and the
[Phase 2 completion audit](docs/operations/phase-2-completion-audit.md) for exactly
what is evidenced versus deferred to a credentialed operator.

## Development

```powershell
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run pyright
```

See the [Phase 1 operations guide](docs/operations/phase-1-operations.md),
[Phase 0 setup guide](docs/operations/phase-0-setup.md),
[Phase 2A live-signal operations guide](docs/operations/phase-2a-live-stream-signals.md),
[Phase 2A development log](docs/devlogs/2026-08-04-phase-2a-live-stream-signals.md),
[Phase 2 creator signal hub operations guide](docs/operations/phase-2-creator-signal-hub.md),
[Phase 2 completion development log](docs/devlogs/2026-08-04-phase-2-completion.md),
[Phase 2 completion audit](docs/operations/phase-2-completion-audit.md),
[signal contract](docs/contracts/krubit-zariya-signal-v1.md), and
[product rollout](docs/roadmaps/2026-08-03-krubit-phase-rollout.md).

When using the workspace-level master `.env`, invoke Krubit through
`scripts/invoke-krubit.ps1` so only Krubit's approved environment variables are loaded.

## Legal documents

- [Privacy Policy](docs/PRIVACY_POLICY.md)
- [Terms of Service](docs/TERMS_OF_SERVICE.md)
