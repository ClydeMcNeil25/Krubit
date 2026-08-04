# Krubit

Krubit is Zariya's non-conversational Discord pet and operational companion. He fetches
functional system results, records auditable events, and provides a safe foundation for later
creator notifications, server monitoring, Entry Sniffing, and member activity measurement.

## Phase 0 capabilities

- Guild-isolated SQLite configuration, events, and action receipts
- Idempotent event ingestion
- Recursive credential redaction before durable storage or signal output
- Least-privilege Discord installation URL
- Non-privileged Guilds Gateway intent
- `/fetch status` for server-scoped health
- Manage-Guild-only `/fetch test-card`
- Versioned `krubit.zariya-signal.v1` foundation test signal
- Administrative CLI for initialization, guild enablement, status, and smoke testing

Phase 0 does not moderate members, profile members, poll creator platforms, read message
content, or mutate Discord server configuration.

## Development

```powershell
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run pyright
```

See the [Phase 0 setup guide](docs/operations/phase-0-setup.md),
[signal contract](docs/contracts/krubit-zariya-signal-v1.md), and
[product rollout](docs/roadmaps/2026-08-03-krubit-phase-rollout.md).

