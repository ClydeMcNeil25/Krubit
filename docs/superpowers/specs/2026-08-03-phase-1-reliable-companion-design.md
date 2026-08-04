# Phase 1 Reliable Companion Design

## Purpose

Phase 1 makes Krubit a useful, non-conversational Discord operations companion. He records factual server changes, captures configuration snapshots, reports health and access gaps, and prepares staff-only functional cards. Zariya remains the Community Manager and owns interpretation, recommendations, member conversation, and approval-gated moderation.

## Scope

Phase 1 delivers:

- member join and leave event logging;
- role, channel, permission, webhook, AutoMod, and Scheduled Event change logging when Discord exposes the event to Krubit;
- versioned snapshots of roles, channels, categories, permission overwrites, Scheduled Events, visible webhooks, visible AutoMod rules, and Krubit settings;
- deterministic, human-readable comparisons between snapshots;
- checks for bot permissions, configured resources, renamed or deleted resources, webhook visibility, and integration coverage;
- staff-only `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations`, and `/fetch backup status` commands;
- a staff-only manual snapshot command and a non-mutating restore preview;
- daily health-summary generation, with delivery disabled unless a staff channel is explicitly configured;
- durable action receipts for every command, collection attempt, snapshot, comparison, and delivery attempt.

Phase 1 does not add member activity scoring, message-content monitoring, risk classification, automated moderation, creator/social feeds, or Discord mutations. It does not claim to back up messages, members, message history, credentials, or deleted content.

## Zariya and Krubit Boundary

Krubit owns mechanical observation and evidence:

- gateway event capture;
- server inventory and snapshots;
- factual configuration diffs;
- permission and integration availability;
- operational health cards;
- backup metadata and restore previews.

Zariya owns human judgment and community presence:

- recommendations and prioritization;
- welcoming and member conversation;
- sentiment and atmosphere interpretation;
- moderation decisions and approved actions;
- announcements and community framing.

Krubit uses his own SQLite records and does not read or write Zariya's community database. Phase 1 shadow reports can be compared with Zariya's server audit, but neither system silently overwrites the other.

## Architecture

The Discord adapter converts gateway events and guild objects into framework-independent observations. An event collector writes deduplicated `GuildEvent` records. A snapshot collector normalizes the current Discord configuration into stable JSON so ordering noise does not create false changes. A comparison service produces typed additions, removals, and modifications. A health service combines current inventory, access limitations, configured-resource checks, and runtime/database state into staff-facing reports.

SQLite adds guild-scoped tables for snapshots, snapshot differences, integration checks, and summary-delivery state. Every public storage operation requires `guild_id`. Snapshot content is redacted before persistence and is hashed so duplicate captures can reuse the existing version.

Discord commands remain thin adapters. They require `Manage Guild`, return ephemeral embeds, and call services that enforce authorization independently of Discord decorators.

## Discord Access and Intents

Krubit enables the Guilds and Guild Members intents. The Server Members Intent must also be enabled for Krubit in the Discord Developer Portal before join/leave collection is considered complete.

The install surface remains least privilege: View Channels, Send Messages, Embed Links, Read Message History, and View Audit Log. Krubit will not request Administrator, Manage Guild, Manage Roles, Manage Channels, Manage Webhooks, or moderation permissions in Phase 1.

Discord endpoints that require mutation-capable permissions are treated as limited coverage. Krubit records and displays the missing capability instead of silently omitting it or requesting broader authority. Webhook and AutoMod inventory are captured only when Discord permits read access with the granted role.

## Event Collection

Gateway handlers capture member joins/leaves, member role changes, role create/update/delete, channel create/update/delete, guild updates, Scheduled Event create/update/delete, AutoMod rule/action events exposed by discord.py, and webhook update notifications. Payloads contain IDs and factual before/after summaries, not message content or secrets.

Event IDs are deterministic from guild, event kind, Discord entity ID, and relevant version/timestamp data. Replayed gateway events produce one stored event and a duplicate receipt rather than duplicate cards.

## Snapshots and Differences

A snapshot contains:

- guild identity and capture time;
- roles with IDs, names, positions, colors, managed state, and permission bitsets;
- categories and channels with IDs, names, types, positions, parent IDs, topics where visible, NSFW state, and normalized permission overwrites;
- Scheduled Events and their status;
- visible AutoMod rule metadata;
- visible webhook metadata without tokens or URLs containing credentials;
- Krubit configuration references and required-permission expectations;
- explicit coverage limitations and API failures.

Comparisons key resources by Discord ID so a rename is reported as a modification rather than a removal plus addition. The diff renderer limits card output to Discord embed constraints and reports overflow counts.

## Health and Integration Checks

Health checks classify findings as healthy, limited, warning, or critical. Checks cover database availability, enabled guild state, gateway readiness, required bot permissions, configured channel existence, renamed configured resources, integration visibility, snapshot freshness, failed collection attempts, and duplicate-delivery prevention.

No finding contains a recommendation in Phase 1. Cards state what was observed, what is inaccessible, and when it was checked so Zariya or staff can interpret it.

## Commands

All Phase 1 commands are guild-only, ephemeral, and restricted to members with Manage Guild:

- `/fetch server-health` shows overall factual health, snapshot age, collection failures, and coverage limits.
- `/fetch changes` shows the latest snapshot difference and recent gateway changes.
- `/fetch permissions` shows granted, missing, and intentionally unrequested permissions.
- `/fetch integrations` shows webhook, AutoMod, Scheduled Event, and configured-channel visibility.
- `/fetch backup status` shows latest snapshot version, integrity hash, and capture status.
- `/fetch backup create` captures a manual snapshot.
- `/fetch backup preview` compares a selected snapshot with current state and describes potential changes without applying them.

Existing `/fetch status` and `/fetch test-card` remain available and are relabeled for Phase 1.

## Daily Summary

A background scheduler evaluates each enabled guild once per UTC day. It generates the same factual health model used by `/fetch server-health`. If `KRUBIT_STAFF_CHANNEL_ID` is absent, generation is receipted as `delivery_disabled` and nothing is posted. If configured, Krubit validates that the channel belongs to the guild and is writable before sending one embed. A guild/day uniqueness key prevents duplicate summaries across reconnects or restarts.

## Error Handling and Safety

- Permission and API failures become explicit coverage records and receipts.
- A failed subsection does not invalidate other successfully collected snapshot sections.
- Snapshot writes and their integrity metadata use a single transaction.
- Restore preview reads snapshots and current state only; Phase 1 contains no apply/mutation path.
- Stored payloads pass through existing secret redaction.
- Guild scope is mandatory in schema keys, queries, services, and commands.
- Staff cards are ephemeral except the explicitly configured daily-summary channel.

## Testing and Rollout

Implementation follows test-first cycles. Unit tests cover normalization, event deduplication, guild isolation, snapshot hashing/versioning, diffs, health classification, authorization, embed limits, coverage failures, and restore-preview non-mutation. Adapter tests use representative discord.py objects or narrow fakes only at the Discord boundary.

Rollout steps are:

1. migrate the existing database non-destructively;
2. enable the Server Members Intent in the Developer Portal;
3. reinstall or update Krubit's role only if the View Audit Log permission is missing;
4. deploy collectors in shadow mode with automatic daily delivery disabled;
5. capture an initial snapshot and compare it with Zariya's latest audit inventory;
6. run every staff-only `/fetch` command;
7. observe a canary window for duplicates, silent failures, cross-guild leakage, and snapshot corruption;
8. configure a private staff channel only after manual cards are accepted.

## Acceptance Criteria

Phase 1 is complete when supported gateway changes are stored exactly once; snapshots are deterministic and guild-isolated; changes are readable; missing access is visible; every command is staff-only and receipted; restore previews cannot mutate Discord; daily summaries cannot duplicate; all automated tests and static checks pass; and live smoke checks confirm the commands against Krucial Town without disrupting Zariya.
