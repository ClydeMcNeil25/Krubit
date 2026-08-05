# Krubit Phase 3 Watchdog Design

**Date:** 2026-08-05
**Status:** Approved for implementation planning
**Scope:** Entry Sniffing, post-join watch window, raid/spam-wave detection, and
incident evidence — detection and evidence only, no autonomous moderation authority.

## Purpose

Give Krubit dependable, explainable safety senses so Zariya and staff are notified of
risk quickly and with complete evidence, without granting Krubit any punishment
authority. Every risk assessment must be deterministic and reproducible from stored
facts; nothing is inferred about a member's identity, mental state, or guilt.

## Product Decisions

- Entry Sniff runs exactly once per member join, producing one durable, versioned
  assessment record. Rejoin after leave produces a new assessment; it does not resume
  or average a prior one.
- The post-join watch window is a bounded, automatically-expiring elevated-monitoring
  state, not a standing surveillance mode. Once a member ages out clean, Krubit
  retains only the original assessment and any receipted incidents — not a running
  behavioral log.
- Risk bands are `clear`, `watch`, `suspicious`, and `incident`, in ascending
  severity, each backed by a plain-language, per-signal explanation. A member never
  sees their own or another member's band; only authorized staff and Zariya do.
- Krubit's own authority in this phase is limited to exactly the "Automatic authority"
  list in the [rollout doc](../../roadmaps/2026-08-03-krubit-phase-rollout.md#phase-3-watchdog-entry-sniffing-and-incident-evidence):
  record the event, preserve permitted evidence, increase monitoring temporarily,
  notify Zariya or staff, and recommend a reversible action. No warning, deletion,
  timeout, kick, ban, role mutation, channel mutation, or public accusation is ever
  automatic — every one of those remains a human (Zariya/staff/KSHQ) decision.
- Krubit integrates with Discord AutoMod's existing rule/action events rather than
  re-implementing keyword/spam enforcement; the codebase already listens to
  `on_automod_rule_create/update/delete` and `on_automod_action`
  (`src/krubit/discord/bot.py:811,820,831,840`) — Phase 3 correlates those events into
  evidence rather than adding a parallel enforcement path.
- Krubit reads public guild message events (content, not DMs) only during a member's
  active watch window, and only for the deterministic signals the design specifies
  (link/domain shape, mention count, repeated-message similarity, coordinated timing).
  It never stores full historical message content outside the watch window, and it
  never reads or stores DM content at all, matching the existing non-negotiable
  boundary.
- Shadow mode first: Phase 3 detection runs and records assessments/incidents from day
  one, but the `/fetch sniff`-family commands and Zariya-notification wiring are the
  only production-visible surface until an operator explicitly enables notification
  delivery — mirroring the shadow-then-enable pattern Phase 2 established for social
  delivery.

## Architecture

```text
member join / message / AutoMod event
                |
    deterministic signal extraction
                |
      risk-band evaluation (pure)
                |
   durable assessment + evidence receipt
                |
  watch-window state (bounded, auto-expiring)
                |
 staff/Zariya notification (recommend only)
                |
        /fetch sniff-family commands
```

### Entry Sniff Assessment

Runs once on `on_member_join`. Inputs are limited to what Discord's Gateway/API
already exposes to an installed bot: account creation timestamp (age), the member's
`bot`/`system` flags, join timing relative to other recent joins in the same guild
(join velocity and join-cluster similarity — members who join within a short shared
window with similar account-age/avatar-absence patterns), invite code used where
Discord exposes it, coarse profile-pattern indicators (default avatar, empty/garbage
username pattern — never inferred personality or protected traits), `Member.pending`/
`MemberFlags` for Rules Screening state where observable, and the guild's own
allow/block lists (Discord user IDs staff have explicitly configured). Each signal
contributes a bounded, named, explainable weight; the assessment is the deterministic
sum mapped to a risk band, never a black-box score.

### Post-Join Watch Window

Opened automatically only for `watch` band or higher (never for `clear`). Bounded
duration, configurable per guild with a safe default, and downgrades/closes
automatically on expiry — Krubit does not require a human action to end a watch
window for a member who caused no further signal. While open, Krubit inspects the
member's own messages in text channels the bot can already see (never DMs) for:
excessive/mismatched mention counts, known malicious-link domain shapes (URL
structure and redirect-shortener detection, not content classification or AI judgment
of intent), near-duplicate repeated messages across channels, and timing correlation
with other currently-watched members (coordinated join-and-post patterns — the raid
signal). None of this becomes a standing log after the window closes; only a
band-changing event produces a durable incident record.

### Risk Bands

`clear` → no watch window, no residual record beyond the one assessment.
`watch` → bounded elevated monitoring, staff-invisible unless queried.
`suspicious` → watch window plus a staff notification (not Zariya-escalation by
default; configurable).
`incident` → watch window, staff and Zariya notification, and a durable evidence
packet. Entering `incident` NEVER auto-executes a moderation action; it always ends in
"recommend a reversible action" per the Non-Negotiable Boundaries.

### Evidence Packets

Contain only authorized facts already available to this system: message links
(jump-URL, not full content unless the specific message triggered the signal, in
which case only that message's redacted content plus the trigger reason), event IDs,
timestamps, the named signals that fired, and an explicit confidence/uncertainty
statement per signal — never a single opaque "risk score" with no explanation. Every
packet passes through the existing `redact()` utility
(`src/krubit/security/redaction.py`) before storage, matching the pattern already
used for guild-event and content receipts.

### Raid / Spam-Wave / Webhook-Abuse / Permission-Risk Detection

Guild-scoped, deterministic, and evidence-producing only:
- **Raid**: join-velocity spike correlated with join-cluster similarity across
  multiple recent joins.
- **Spam-wave**: multiple currently-watched (or even `clear`) members posting
  near-duplicate content within a short guild-wide window.
- **Webhook-abuse**: an existing webhook (already tracked by Phase 1's webhook
  inventory) posting at an anomalous rate or from an unexpected origin, correlated
  against Krubit's own webhook change history.
- **Permission-risk**: a role/permission change that grants elevated access to a
  currently-watched or newly-joined member, correlated against Phase 1's existing
  permission-change tracking.

All four route into the same evidence-packet and notification path as an `incident`-
band member assessment — no separate enforcement path.

## Privileged Intent Requirement

The message-inspection watch window requires Discord's privileged **Message Content**
intent, which the codebase does not currently request
(`src/krubit/discord/install.py` only builds through `phase_two_intents()` —
`guilds`, `members`, `presences`). Phase 3 adds `phase_three_intents()` requesting
`message_content` additively, following the existing phase-ladder pattern. This is a
privileged intent Discord requires explicit application-level enablement for (and bot
verification once a bot serves 100+ guilds, not a near-term concern for Krucial
Town). The operator runbook must document enabling it in the Discord Developer Portal
before Phase 3 message-based signals can function; until enabled, Krubit degrades to
join-signal-only detection (no message-content-dependent signals) rather than failing
to start — matching the existing "optional capability degrades honestly" pattern from
Phase 2's `CapabilityState` vocabulary.

## Data Model (guild-scoped, following the established `sqlite.py` pattern)

- `entry_sniff_assessments`: one row per join, `PRIMARY KEY (guild_id, member_id,
  joined_at)`, band, per-signal breakdown (JSON, redacted), created_at.
- `watch_windows`: `PRIMARY KEY (guild_id, member_id)`, opened_at, expires_at, band,
  closed_at nullable, close_reason (`expired` | `escalated` | `staff_override`).
- `incidents`: `PRIMARY KEY (guild_id, incident_id)`, kind (member | raid | spam_wave
  | webhook_abuse | permission_risk), band, opened_at, evidence packet reference,
  recommended_action (free text, human-reviewed only), acknowledged_by nullable.
- `guild_allow_block_lists`: `PRIMARY KEY (guild_id, discord_user_id)`, list_kind
  (allow | block), reason, set_by, set_at.
- `sniff_receipts`: append-only, mirrors the existing receipt pattern
  (`CreatorRegistryReceipt`/`content_receipts`) for every assessment, watch-window
  transition, and incident evidence write.

## Commands

Staff-only (Manage Guild or a configured moderator role), all ephemeral by default:

- `/fetch sniff <member>` — current or most recent assessment for one member.
- `/fetch sniff-report` — guild-wide summary of open watch windows and recent bands.
- `/fetch incident <incident_id>` — full evidence packet for one incident.
- `/fetch evidence <incident_id>` — raw evidence export (still staff-only, still
  redacted at the storage layer, per Non-Negotiable Boundaries "detailed risk,
  incident, and member-activity views are limited to authorized staff").
- `/fetch watchlist` — currently-open watch windows and the guild's configured
  allow/block lists.

None of these commands can execute a moderation action; they are read/report surfaces
only, consistent with "Krubit cannot execute an unapproved moderation action" from the
Phase 3 exit gate.

## Shadow Comparison Against Zariya

Per the rollout doc's Zariya-overlap handling for this phase, Krubit's detection runs
in shadow alongside Zariya's existing deterministic moderation triage during this
phase; Zariya's existing path remains authoritative for production decisions until
parity is demonstrated (matching the same shadow-then-migrate discipline used for
Phase 1's server audit and Phase 2's Twitch/YouTube presence detection).

## Testing and Rollout

Automated tests must cover: risk-band determinism (same inputs always produce the
same band and explanation), watch-window auto-expiry, benign-join-surge false-positive
rate (a legitimate community growth spike must not blanket-flag clean members),
raid/spam-wave detection against synthetic fixtures, cross-guild isolation for every
new table, redaction of evidence packets before storage, and — critically — that no
code path in this phase can call a Discord moderation-mutation endpoint (kick, ban,
timeout, role removal, message deletion) at all; the absence of that capability should
be structurally enforced (no client method for it imported/available in this phase's
modules), not just behaviorally untested.

## Completion Gate

Phase 3 is complete only when: test raids and benign join surges show acceptable
false-positive behavior; every risk result is explainable from stored per-signal
data; clean members age out of watch state automatically; private findings never
appear publicly or to non-staff; and Krubit cannot execute an unapproved moderation
action — verified structurally, not just by test coverage. This mirrors the Phase 3
exit gate in the rollout doc verbatim.

## Explicit Exclusions

- No warning, deletion, timeout, kick, ban, role mutation, channel mutation, or
  public accusation is ever automatic.
- No DM content is read or stored.
- No inference of personality, mental state, protected traits, or guilt from activity
  patterns.
- No claim of access to IP addresses, devices, email addresses, or complete
  cross-server history.
- No standing behavioral log survives a closed, clean watch window.
- No keyword-based enforcement duplicating Discord AutoMod — Krubit correlates
  AutoMod's own events into evidence instead.

## Official Capability References

- Discord Gateway intents (including Message Content): <https://discord.com/developers/docs/events/gateway#gateway-intents>
- Discord AutoMod: <https://discord.com/developers/docs/resources/auto-moderation>
- Discord Member Rules Screening / onboarding fields: <https://discord.com/developers/docs/resources/guild#guild-member-object>
- Discord Message Content intent policy (privileged, verification threshold): <https://support-dev.discord.com/hc/en-us/articles/4404772028055>
