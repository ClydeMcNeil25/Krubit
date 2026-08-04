# Krubit Development Log: Phase 2A Live-Stream Signals

**Date:** August 4, 2026
**Status:** Automated implementation and verification through Task 8 complete; controlled live canary not run

## Scope

Phase 2A implements only the Twitch/Discord-presence live-signal slice of Phase 2. Krubit
can observe a Twitch Streaming activity that a member shares publicly in Discord, persist
the signal, enrich it through Twitch, manage the dedicated `Streaming Now` role, and
deliver a receipted card to the configured live-notification channel.

Krubit does not read private Discord connected accounts. A member must share Streaming
activity publicly in Discord before this slice can detect the stream. YouTube, other
social connectors, and the remaining Phase 2 features are still pending.

## Delivered implementation

- Added opt-in live-signal settings, Twitch credential validation, Presence Intent, and
  the least additional Discord permissions: Manage Roles and Mention Everyone.
- Added Twitch URL normalization, immutable live-signal domain values, and a TLS-backed
  Twitch Helix client with bounded token, rate-limit, unavailable, and retry handling.
- Added additive SQLite tables for live configuration, sessions, deliveries, and checks,
  including guild-scoped indexes and atomic terminal retirement of claimed deliveries.
- Added durable role and announcement plans, five-minute missing-evidence handling,
  restart recovery, and a 60-second reconciliation loop.
- Added Discord Streaming extraction for validated Twitch activity only, safe reduced and
  enriched cards, explicit allowed mentions, and the `Streaming Now` ownership rule.
- Added `/fetch live status`, `/fetch live test`, and `/fetch live reconcile`, plus Phase
  2 health facts for configuration, Presence Intent, Twitch, role, hierarchy, and mention
  capabilities.

Krubit removes `Streaming Now` only when its durable receipt records that Krubit assigned
that role. A pre-existing role is left in place. Reduced delivery and later enrichment
reuse the existing announcement rather than create a replacement message.

## Commit sequence

| Commit | Change |
|---|---|
| `88c0cb3` | Hardened live-signal domain validation |
| `8ec29e9`, `9af4d9a` | Added and hardened durable live-signal persistence |
| `6c0caee`, `6a1539c` | Added Twitch queries and hardened token lifecycle |
| `37324ff`, `555ccc9`, `74d87a5` | Added and hardened reconciliation and liveness handling |
| `1e457b1` | Rendered Discord live-signal cards |
| `5b8085e` through `3f135a3` | Added Discord runtime, restart recovery, terminal retirement, and transaction hardening |
| `71aaa7e`, `14c21b5` | Added staff controls and corrected reconciliation applied-plan counting |

## Schema and operational surfaces

The additive schema includes `live_signal_config`, `live_signal_sessions`,
`live_signal_deliveries`, and `live_signal_checks`. Live configuration stores the selected
channel and role by ID after exact-name bootstrap, so later renames do not redirect
execution. Sessions and delivery claims are guild-scoped and retain action evidence.

The operator surface is documented in the [Phase 2A operations guide](../operations/phase-2a-live-stream-signals.md). The public command names are `/fetch live status`,
`/fetch live test`, and `/fetch live reconcile`; all are guild-only, Manage-Server
authorized, ephemeral staff responses. The test preview has no public content, role
adapter, or channel-send path.

## Automated verification through Task 8

The final Task 8 verification recorded:

- Full test suite: `200 passed in 12.80s`.
- Full Ruff: `All checks passed!`.
- Full Pyright: `0 errors, 0 warnings, 0 informations`.
- `git diff --check`: exit `0`; Windows emitted only informational line-ending notices.

Task-focused verification also covered configuration and install permissions, live-signal
domain validation, persistence, Twitch transport behavior, reconciliation, Discord card
rendering, runtime lifecycle and recovery, staff commands, inventory, and health facts.
The Task 8 command and health suite reported `26 passed`; the reconciliation-count
correction's named regressions reported `2 passed` and its focused runtime/command/health
suite reported `40 passed`.

No automated test used a real Twitch request, changed a Discord role, sent a Discord
message, or issued an `@everyone` notification.

## Controlled live canary status

The controlled live canary has not run. It requires explicit user go-ahead before setting
the live-signal flag true for a real `@everyone` ping. Therefore there are no live Discord
message receipts, role-change receipts, stream observations, or canary timestamps to
record in this development log.

Until that authorization is given, the supported validation path is shadow mode with
`KRUBIT_LIVE_SIGNALS_ENABLED=false`, `/fetch live test`, `/fetch permissions`, `/fetch
integrations`, and `/fetch live status`. The preview and preflight are available without
publishing a card or changing a role.

## Safety and recovery notes

The role and destination are checked for existence, permissions, and hierarchy before
execution. If mention permission is unavailable, Krubit sends a no-mention degraded card
and records the condition. If role execution fails, enrichment and announcement work for
that session are deferred for recovery. Reconciliation is idempotent and counts only
plans it actually applies.

Rollback disables the live runtime, retains the additive database records, and preserves
Phase 1 monitoring. Manual removal is limited to stale `Streaming Now` roles whose
receipts prove Krubit assigned them; no other roles should be changed.

This log intentionally excludes credentials, token values, raw environment values, Twitch
response bodies, member names, message content, and unobserved canary claims.
