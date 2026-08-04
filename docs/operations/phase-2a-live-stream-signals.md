# Phase 2A Live-Stream Signal Operations

Phase 2A provides the Twitch/Discord-presence portion of the creator-notification plan.
Krubit watches a member's public Discord Streaming activity, validates Twitch activity,
persists the resulting work, and can assign the dedicated `Streaming Now` role and send a
card to `#live-notifications`.

Discord can expose a Twitch stream only when the member chooses to share a Streaming
activity publicly in Discord. Krubit does not inspect private connected accounts. This
phase does not implement YouTube, other social sources, or the remaining Phase 2
notification features.

## Prerequisites and least privilege

1. In Discord Developer Portal, open **Krubit**, then **Bot**, then **Privileged Gateway
   Intents**, and set **Presence Intent** to **ON**.
2. Install or update the bot with `scripts/invoke-krubit.ps1 install-url`. Confirm the
   generated installation does not request Administrator.
3. Give the Krubit role **Manage Roles** and **Mention @everyone, @here, and All Roles**.
   It also needs the destination permissions **View Channel**, **Send Messages**, **Embed
   Links**, and **Read Message History**. Read Message History is separate from the
   view/send/embed/mention permissions: it supports the bounded crash-recovery nonce scan
   described below.
4. Put Krubit's role above `Streaming Now` in the Discord role hierarchy. The bot will not
   manage a role at or above its own top role.
5. Create the text channel `#live-notifications` and the role `Streaming Now` with those
   exact names before first live enablement. Krubit discovers them once and stores their
   IDs, so later renames do not redirect the configured destination or role.
6. Confirm the workspace master `.env` contains `TWITCH_KRUBIT_CLIENT_ID` and
   `TWITCH_KRUBIT_CLIENT_SECRET`. Do not paste either value into Discord, a command
   response, an issue, or a receipt.

The Phase 2 install surface adds Presence Intent, Manage Roles, and Mention Everyone to
the Phase 1 surface. Do not grant Administrator merely to resolve a missing capability.

## Shadow start and preflight

Start with `KRUBIT_LIVE_SIGNALS_ENABLED=false` in the workspace master `.env`, then use
the established launcher to start or restart the bot:

```powershell
& scripts/invoke-krubit.ps1 run
```

With the flag false, Krubit does not create the Twitch runtime or perform public live
actions. The private preview remains available and is the first smoke check:

```text
/fetch live test
```

Run these staff commands from the target guild as a member with Manage Server:

```text
/fetch permissions
/fetch integrations
/fetch live status
```

`/fetch live test` renders only an ephemeral embed and stream-link view. It does not send
a channel message, change a role, or ping anyone. `/fetch permissions` and `/fetch
integrations` report the Presence Intent, Twitch-credential, runtime-availability,
Manage Roles, role-hierarchy, and mention capability facts separately. `/fetch live
status` reports only guild-scoped session count, states, integration availability, and
check times.

Correct every reported missing prerequisite before proceeding. In shadow mode, `live
status` can report Twitch unavailable because the live runtime is intentionally disabled.

## Enabling the controlled live path

Enabling live signals permits a public `@everyone` announcement. Obtain explicit operator
approval for that real notification before changing the flag.

1. Set `KRUBIT_LIVE_SIGNALS_ENABLED=true` in the master `.env`; both
   `TWITCH_KRUBIT_CLIENT_ID` and `TWITCH_KRUBIT_CLIENT_SECRET` must be present when this
   is true.
2. Restart with `& scripts/invoke-krubit.ps1 run`.
3. Repeat `/fetch permissions`, `/fetch integrations`, and `/fetch live status`.
4. Have a consenting member share a genuine Twitch Streaming activity publicly in Discord.
5. Confirm one `Streaming Now` role assignment and one card in `#live-notifications`.
   When Mention Everyone is available, the card can notify `@everyone`; when it is not,
   Krubit sends the card with no permitted mentions and records a degraded result.
6. End the public Streaming activity and confirm Krubit removes only the dedicated role it
   recorded as assigning. It must leave every other member role unchanged.

The initial role action is persisted before Twitch enrichment. Twitch lookup is bounded;
if the provider cannot supply stream details in time, Krubit can send a reduced card from
the public Discord activity and later enrich the existing card. Recovery and the
60-second reconciliation loop use durable sessions and delivery claims to avoid a second
announcement for the same tracked stream.

## Staff controls and recovery

Use the following command for an idempotent, guild-scoped reconciliation:

```text
/fetch live reconcile
```

The command reports the number of plans successfully applied, not the number merely
considered. Use it after restoring a missing role permission, role hierarchy, destination
permission, or Twitch availability. It also helps resume durable work after a bot restart.

If a prerequisite is unavailable, correct the underlying Discord configuration or Twitch
credential configuration, restart through the launcher if configuration changed, rerun
the preflight commands, then run `/fetch live reconcile`. Do not manually resend a live
card or assign a role to compensate for a delayed check: reconciliation preserves the
durable delivery and ownership safeguards.

When the bot cannot mention everyone, delivery degrades to a no-mention card and records
that condition. If the destination is unavailable or cannot accept messages or embeds,
the live delivery remains failed or pending for a later reconciliation; do not use a
different channel as an undocumented substitute.

Each send uses a deterministic nonce. During recovery, Krubit scans at most 25 recent
messages authored by Krubit in the configured destination for that nonce before deciding
whether to send. discord.py 2.7.1 exposes `nonce` but not `enforce_nonce`, so an external
exactly-once guarantee is not possible if Krubit crashes after Discord accepts a send but
before Krubit records its receipt and that message is outside the bounded scan. If Read
Message History is missing or the scan fails, Krubit contains the error, leaves the
delivery claim retryable, and does not blindly resend. Operators do not need to search
channel history manually.

## Rollback

To stop new live-signal actions while preserving Phase 1 monitoring:

1. Set `KRUBIT_LIVE_SIGNALS_ENABLED=false` in the master `.env`.
2. Restart Krubit with `& scripts/invoke-krubit.ps1 run`.
3. Run `/fetch live reconcile` as the reconciliation and cleanup check. With the runtime
   disabled it performs no live mutation, so an applied count of zero is expected.
4. Review the durable action receipts before any manual cleanup. Remove a stale
   `Streaming Now` role only when its receipt proves Krubit assigned that exact role for
   the relevant session. Do not remove pre-existing roles or any other member roles.
5. Keep Phase 1 monitoring online. Do not delete the SQLite database, its WAL, or its SHM
   file; the live tables and receipts are needed for safe review and remain additive.

If a code regression, rather than configuration, requires rollback, restart the last
accepted Phase 1 or Phase 2A commit through the same launcher and preserve the database,
logs, and receipts. Phase 1 monitoring remains the fallback operational surface.

## Secret rotation

1. Set `KRUBIT_LIVE_SIGNALS_ENABLED=false` and restart Krubit to stop live Twitch work.
2. Rotate the Twitch Client Secret using the authorized Twitch developer controls. If the
   client identity is replaced, update `TWITCH_KRUBIT_CLIENT_ID` as well.
3. Update only the workspace master `.env`; do not place secret values in source files,
   command text, receipts, logs, screenshots, or public cards.
4. Restart with `& scripts/invoke-krubit.ps1 run` while the flag remains false.
5. Run `/fetch integrations`, `/fetch permissions`, `/fetch live status`, and `/fetch
   live test` to confirm the bot remains healthy without issuing a public notification.
6. Obtain explicit approval again before setting the flag true and resuming the controlled
   live path.

## Evidence boundaries

Keep operational records to command outcomes, health facts, timestamps, and receipt
identifiers. Do not record Twitch response bodies, member names, message content,
credentials, token values, or raw environment values in documentation or incident notes.
