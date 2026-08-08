# Phase 1 Operations

Phase 1 runs Krubit as a read-only shadow monitor alongside Zariya. Krubit records factual evidence and functional cards; Zariya remains responsible for interpretation, recommendations, member interaction, and approval-gated moderation.

## Discord prerequisites

1. Open Krubit's application in the Discord Developer Portal.
2. On **Bot**, enable **Server Members Intent**. Presence and Message Content remain disabled.
3. Use `scripts/invoke-krubit.ps1 install-url` and confirm Krubit's role has View Channels, Send Messages, Embed Links, Read Message History, and View Audit Log.
4. Do not grant Administrator, Manage Guild, Manage Roles, Manage Channels, Manage Webhooks, Kick Members, Ban Members, or timeout permissions.

If Server Members Intent is not enabled, Discord will reject the Phase 1 gateway connection. Restore the Phase 0 runtime or enable the intent before retrying.

### Intentional coverage limits

Discord requires **Manage Server** to list AutoMod rules and **Manage Webhooks** to list guild webhooks. Those permissions can modify server resources, so Krubit's read-only Phase 1 role intentionally does not request them. AutoMod and webhook inventory may therefore report `limited` with a Discord `403`; this is a visible least-privilege limitation rather than a silent collection failure.

## Configuration

The launcher reads only Krubit's approved variables from the workspace master `.env`:

```dotenv
DISCORD_KRUBIT_APPLICATION_ID=
DISCORD_KRUBIT_BOT_TOKEN=
KRUBIT_DATABASE_PATH=D:/Dropbox/05 Software Development/Krubit/data/krubit.db
KRUBIT_STAFF_CHANNEL_ID=
```

Leave `KRUBIT_STAFF_CHANNEL_ID` blank during the shadow canary. Krubit will receipt `delivery_disabled` once per guild/day and will not post. To opt in later, use the numeric ID of a private staff text channel that Krubit can view and write.

## Migration

`krubit init-db` is additive. It preserves Phase 0 guild configuration, events, and receipts while adding configuration snapshots and daily-summary claims.

```powershell
& scripts/invoke-krubit.ps1 init-db
& scripts/invoke-krubit.ps1 status 356068206034550784
```

Never delete `data/krubit.db`, its WAL, or its SHM file during a migration or rollback.

## Controlled restart

1. Resolve the existing launcher-managed PowerShell, `uv`, and Python process chain.
2. Stop only that verified chain.
3. Start `scripts/invoke-krubit.ps1 run` in the normal hidden runtime wrapper.
4. Confirm exactly one chain remains and `logs/krubit-stderr.log` is empty.
5. Confirm the database reports healthy for guild `356068206034550784`.

## Staff smoke tests

Run these commands in Krucial Town as a member with Manage Guild:

- `/fetch admin status`
- `/fetch admin test-card`
- `/fetch admin server-health`
- `/fetch admin changes`
- `/fetch admin permissions`
- `/fetch admin integrations`
- `/fetch backup status`
- `/fetch backup create`
- `/fetch backup preview`

All responses are ephemeral. Restore preview may report additions, removals, or modifications, but it must make no Discord change. A successful command creates one action receipt; inventory collection may additionally create a new snapshot version when configuration changed.

All `/fetch` commands, including `/fetch admin status` and `/fetch admin test-card`, require Manage Server authority in Phase 1.

## Zariya comparison

Compare Krubit's snapshot counts with Zariya's latest read-only audit at:

`D:/Dropbox/05 Software Development/KAI-System/agents/zariya_kessari/runtime_memory/community_manager/server_audit_latest.json`

Compare roles, categories/channels, Scheduled Events, AutoMod rules, and access limitations. Record discrepancies as coverage notes. Do not change Zariya's database, recommendations, or Discord configuration to force matching counts.

## Canary and rollback

The canary fails if Krubit produces duplicate cards, loses supported gateway events, hides permission failures, leaks cross-guild records, corrupts a snapshot, or mutates Discord from restore preview.

To roll back, stop the verified Phase 1 process chain and start the previously accepted commit using the same launcher. Keep the database: the added tables are forward-compatible and ignored by Phase 0 code. Preserve logs and receipts for diagnosis.
