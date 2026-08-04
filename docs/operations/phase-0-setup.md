# Krubit Phase 0 Setup

## Requirements

- Python 3.13
- `uv`
- A Discord application created specifically for Krubit
- A Discord test server where the installer has Manage Server permission

## Discord Developer Portal

1. Create an application and add a bot user.
2. Configure the app for Guild Install. Krubit is not a User Install app in Phase 0.
3. Use the `bot` and `applications.commands` scopes.
4. Grant only View Channels, Send Messages, Embed Links, and Read Message History.
5. Leave Presence Intent, Server Members Intent, and Message Content Intent disabled.
6. Copy the application ID. Reset/copy the bot token only when placing it into the runtime
   environment; never save it in repository files or Discord messages.

Discord recommends its provided install link for most apps. Krubit's CLI also generates an
explicit least-privilege Guild Install URL so the Phase 0 permissions can be audited.

## Local installation

```powershell
uv sync --all-groups
Copy-Item .env.example .env
```

Set these environment variables in the process environment or your deployment secret store:

```text
DISCORD_KRUBIT_APPLICATION_ID=<numeric application id>
DISCORD_KRUBIT_BOT_TOKEN=<secret bot token>
KRUBIT_DATABASE_PATH=data/krubit.db
```

The application loads environment variables supplied to the process. It does not parse `.env`
itself; use your deployment platform or shell to load the file without printing the token.

If a Miniconda installation overrides `SSL_CERT_FILE` with a stale certificate bundle, dependency
installation may fail with `UnknownIssuer`. Clear that override for the `uv` invocation and use
`uv --system-certs sync --all-groups`; do not disable TLS verification.

## Initialize and install

```powershell
uv run python -m krubit init-db
uv run python -m krubit install-url
uv run python -m krubit enable-guild <guild-id>
uv run python -m krubit status <guild-id>
uv run python -m krubit emit-test-signal <guild-id> --actor-id <operator-user-id>
```

Open the generated install URL, select the test server, and approve the displayed permissions.
Then start the gateway:

```powershell
uv run python -m krubit run
```

Krubit globally synchronizes `/fetch status` and `/fetch test-card` during startup. Discord may
take time to propagate global application-command changes. `/fetch test-card` is both declared
with Manage Server defaults and checked again by the application service.

## Smoke checks

1. `/fetch status` returns an ephemeral functional card scoped to the current server.
2. A member without Manage Server cannot run `/fetch test-card` successfully.
3. An administrator receives the Phase 0 test card.
4. Restarting Krubit does not create duplicate records for the same submitted event ID.
5. Disabling a guild with `enable-guild <guild-id> --disable` makes ingestion fail closed.
6. `status` for a second test guild shows independent event and receipt counts.
7. Search the database and logs for a test assignment such as `api_key=canary`; only
   `[REDACTED]` may remain.

## Data and recovery

The SQLite database stores guild enablement, redacted event envelopes, and redacted action
receipts. Back up the database with a SQLite-aware backup process while the service is running,
or stop the service before copying the database plus any WAL state. Phase 0 does not claim to
back up Discord messages, members, roles, or server configuration.

## Phase 0 exclusions

- No moderation or protective actions
- No member activity ledger or Entry Sniffing
- No message-content or member privileged intents
- No Twitch, YouTube, or social monitoring
- No KSHQ transport or automatic Zariya delivery
- No Discord channel, role, permission, event, or AutoMod mutation
