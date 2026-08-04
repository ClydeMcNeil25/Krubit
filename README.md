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

This is only the Twitch/Discord-presence slice of Phase 2. Discord can detect a Twitch
stream only when the member chooses to share a Streaming activity publicly in Discord;
Krubit does not read private connected accounts. YouTube, other social platforms, and the
remaining Phase 2 notification features are not implemented yet.

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
[signal contract](docs/contracts/krubit-zariya-signal-v1.md), and
[product rollout](docs/roadmaps/2026-08-03-krubit-phase-rollout.md).

When using the workspace-level master `.env`, invoke Krubit through
`scripts/invoke-krubit.ps1` so only Krubit's approved environment variables are loaded.

## Legal documents

- [Privacy Policy](docs/PRIVACY_POLICY.md)
- [Terms of Service](docs/TERMS_OF_SERVICE.md)
