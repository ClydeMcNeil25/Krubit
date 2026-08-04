# Krubit Phase Rollout

## Product Definition

Krubit is Zariya Kessari's non-conversational Discord pet and operational companion. He watches the server, fetches information, publishes functional notification cards, performs routine automation, records evidence, and alerts Zariya when human judgment is needed.

Krubit is not an AI employee, community manager, conversational assistant, or replacement for Zariya. His personality is expressed through his dog-like visual identity, `/fetch` command family, status indicators, and concise automated cards rather than generated conversation.

The first deployment target is Krucial Town. The architecture must nevertheless support multiple isolated Discord servers from the beginning so Krubit can become a commercial creator-community product without a multi-tenancy retrofit.

## Primary Outcome

Krubit's initial community outcome is improved new-member activation and 30-day retention. Operational reliability, creator notification delivery, safety detection, and trustworthy measurement are prerequisites for that outcome.

## Ownership Boundaries

| Capability | Discord | Krubit | Zariya | KSHQ |
|---|---|---|---|---|
| Channels, roles, permissions, native onboarding, Rules Screening, AutoMod, events, polls, audit log | System of record | Reads and augments | Advises on community use | Enforces platform access |
| Member-facing conversation | Provides channel | No open-ended conversation | Owns | Routes Zariya's sessions |
| Creator and social monitoring | Limited native integrations | Owns external monitoring, deduplication, and cards | Decides communication strategy | Optional downstream coordination |
| Routine notifications and scheduled cards | Delivers | Owns | May add human context | Not required |
| Member activity measurement | Supplies authorized events | Owns ledger and calculations | Interprets and chooses engagement | May route briefing context |
| Entry sniffing and behavioral signals | Supplies available member/events data and native raid signals | Owns deterministic assessment | Reviews ambiguous cases | Governs sensitive follow-up actions |
| Community sentiment and relationship judgment | Supplies authorized messages | Supplies evidence only | Owns | Provides employee runtime |
| Moderation detection | Provides AutoMod and events | Detects, correlates, and preserves evidence | Owns triage and proportional response decision | Owns approvals, permission enforcement, and sensitive receipts |
| Reversible routine protection | Provides permissions | Executes only explicit pre-authorized policies | Receives notification and may override | Required when policy classifies action as sensitive |
| Server audit | Provides configuration and native audit entries | Owns snapshots, diffs, health rules, and factual findings | Owns prioritization and recommendations | Provides governed mutation path |
| Event strategy | Provides Scheduled Events | Automates logistics and measurement | Owns experience design and member interaction | Coordinates other employees when needed |
| Briefings | N/A | Produces factual metrics, alerts, and evidence packets | Interprets, prioritizes, and communicates | Delivers governed employee context |
| Credentials and employee memory | N/A | Owns only Krubit integration secrets and operational data | Owns her memory and relationship continuity | Owns employee credential/session governance |

## Non-Negotiable Boundaries

- Krubit does not generate open-ended replies or impersonate a staff member.
- Krubit does not mediate disputes, interpret policy publicly, or decide permanent punishment.
- Krubit does not read or store private DMs for activity analytics.
- Krubit does not record voice content; it may record authorized join/leave participation events and duration.
- Krubit does not infer personality, mental state, protected traits, or guilt from activity patterns.
- Krubit never claims access to IP addresses, devices, email addresses, or complete cross-server history.
- Detailed risk, incident, and member-activity views are limited to authorized staff.
- Routine automated actions must be deterministic, explainable, reversible, and receipted.
- Zariya and Krubit must not independently process the same responsibility in production after migration is complete.
- Discord-native systems remain canonical where they already solve the base problem.

## Target Architecture

Krubit is a standalone Discord application and multi-server product. Krucial Town enables an optional companion bridge to Zariya and KSHQ.

```text
Discord + creator/social platforms
                |
              Krubit
  sensors | ledger | rules | cards | /fetch
                |
     structured, versioned handoff
                |
              Zariya
     judgment | strategy | interaction
                |
               KSHQ
 approval | sensitive execution | receipt
```

Krubit may directly execute ordinary actions within its own explicit policy, such as posting a configured live card, assigning a streaming role, updating a scheduled event, or applying an approved temporary safety profile. Sensitive moderation remains subject to Zariya's judgment and KSHQ's approval and execution controls in Krucial Town.

## Rollout Principles

1. Deploy to Krucial Town before offering Krubit to external servers.
2. Start every detection or enforcement system in shadow mode.
3. Separate detection quality from action authority.
4. Prefer Discord-native onboarding, AutoMod, Scheduled Events, polls, roles, and audit data.
5. Add one production responsibility owner at a time; retire the overlapping Zariya path only after parity is proven.
6. Do not market a capability until delivery, failure reporting, permission handling, and recovery are tested.
7. Multi-server isolation, auditability, privacy controls, and data deletion are foundation work rather than paid-product cleanup.

## Phase 0: Contracts, Boundaries, and Product Foundation

### Goal

Create the safe technical and organizational foundation on which every later capability depends.

### Deliverables

- Standalone Krubit Discord application and bot identity.
- Multi-server tenant model keyed by Discord guild ID.
- Per-guild configuration, feature flags, channel mappings, role mappings, timezone, and retention policy.
- Discord OAuth installation flow with least-privilege permission calculation.
- Explicit Gateway intent inventory, including privileged-intent requirements and verification thresholds.
- Event ingestion with idempotency, retry, ordering tolerance, and dead-letter handling.
- Scheduled-job infrastructure for polling external platforms and running maintenance tasks.
- Versioned audit log for every Krubit decision, configuration change, attempted action, and Discord receipt.
- Secret storage that never places tokens in profiles, logs, exports, or alert cards.
- Authorization layer for member, creator, staff, moderator, and administrator commands.
- `/fetch help`, `/fetch status`, and an internal test-card command.
- Branded card primitives for fetched, detected, guarded, failed, and healthy states.
- Versioned Krubit-to-Zariya signal, incident, briefing, and action-proposal contracts.
- Data export, deletion, and channel-exclusion contracts before member tracking begins.
- Simulation harness using recorded/synthetic Discord events; live tests must not perform moderation.

### Explicit exclusions

- No live creator feeds.
- No member risk classification.
- No activity scoring.
- No autonomous moderation.
- No migration of Zariya's existing observer yet.

### Exit gate

Advance only when two test guilds cannot read or mutate each other's configuration or records; duplicate events produce one outcome; missing permissions fail visibly; secrets are absent from logs; and every attempted action produces a durable success or failure receipt.

## Phase 1: Reliable Companion MVP

### Goal

Make Krubit useful every day through reliable Discord-native monitoring and functional `/fetch` outputs.

### Deliverables

- Member join and leave logging.
- Role, channel, permission, webhook, AutoMod, and Scheduled Event change logging where Discord exposes the event.
- Current configuration snapshots for roles, channels, categories, permissions, AutoMod rules, webhooks visible to the bot, and Krubit settings.
- Snapshot comparison with human-readable diffs.
- Integration and permission health checks.
- Detection of missing configured channels, renamed resources, broken webhooks, and lost bot permissions.
- Staff-only health and change cards.
- `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations`, and `/fetch backup status`.
- Manual configuration snapshot and restore preview.
- Restore remains selective and approval-gated; no claim of full message/member backup.
- Daily operational health summary for administrators.

### Zariya overlap handling

Zariya's existing server audit remains the production source during this phase. Krubit runs equivalent collection and factual checks in shadow mode. Reports are compared for inventory, access limitations, and factual findings; Zariya continues to own recommendations.

### Exit gate

Advance when Krubit can run continuously for an agreed canary period without duplicate cards, lost events, silent permission failures, cross-guild leakage, or unreconciled snapshot corruption. Restore previews must never mutate Discord.

## Phase 2: Creator Signal and Notification Hub

### Goal

Consolidate the current creator-notification bots into Krubit and prove reliable external integration behavior.

### Deliverables

- Twitch live/offline monitoring.
- YouTube live, scheduled stream, and new-video monitoring.
- Additional social connectors only when their official APIs and terms permit reliable access.
- Multi-creator registry with creator-owned routing and templates.
- Cross-platform deduplication for the same content or campaign.
- Live, delayed, cancelled, ended, and failed-feed states.
- Automatic `Streaming Now` role assignment and removal.
- Discord Scheduled Event synchronization where appropriate.
- Quiet hours, mention budgets, batching, and per-channel routing.
- Notification preview and test delivery.
- Retry, correction, retraction, and delivery receipts.
- Expired-token, quota, and integration-failure alerts.
- Notification engagement and post-stream performance records.
- `/fetch live`, `/fetch latest`, `/fetch schedule`, and `/fetch creator`.

### Zariya overlap handling

Krubit owns detection and functional cards. Zariya owns campaign tone, community framing, special announcements, and any conversational follow-up. KSHQ remains optional for ordinary creator notifications.

### Exit gate

Advance when recorded and live canary tests demonstrate exactly-once announcements across reconnects and restarts, correct role cleanup after stream termination, visible connector failures, respected quiet hours, and no duplicate cross-platform cards.

## Phase 3: Watchdog, Entry Sniffing, and Incident Evidence

### Goal

Give Krubit dependable safety senses without granting premature punishment authority.

### Deliverables

- One-time Entry Sniff assessment on member join.
- Deterministic join signals: account age, bot flag, join velocity, invite source where available, profile-pattern indicators, current join-cluster similarity, Rules Screening state where observable, and server-local allow/block lists.
- Post-join watch window for immediate spam, malicious links, mass mentions, repeated messages, and coordinated behavior.
- Raid, spam-wave, suspicious-link, webhook-abuse, and permission-risk detection.
- Explainable risk bands: clear, watch, suspicious, and incident.
- Automatic expiration and downgrade of temporary watch state.
- Evidence packets containing only authorized facts, message links, event IDs, timestamps, reasons, and confidence/uncertainty.
- Staff-only `/fetch sniff`, `/fetch sniff-report`, `/fetch incident`, `/fetch evidence`, and `/fetch watchlist`.
- Integration with Discord AutoMod events rather than duplicate keyword enforcement.
- Shadow comparison against Zariya's existing deterministic moderation triage.
- Configurable incident-data retention and access audit.

### Automatic authority in this phase

- Record the event.
- Preserve permitted evidence.
- Increase monitoring temporarily.
- Notify Zariya or staff.
- Recommend a reversible action.

No warning, deletion, timeout, kick, ban, role mutation, channel mutation, or public accusation is automatic in this phase.

### Exit gate

Advance when test raids and benign join surges show acceptable false-positive behavior; every risk result is explainable; clean members age out of watch state; private findings never appear publicly; and Krubit cannot execute an unapproved moderation action.

## Phase 4: Member Activity Ledger and Retention Intelligence

### Goal

Measure community participation and new-member activation without turning Krubit into a surveillance or relationship-judgment system.

### Deliverables

- Per-member authorized event ledger for joins, onboarding signals, messages, reactions, voice participation duration, event attendance, roles, milestones, and moderation receipts.
- No DM ingestion for analytics and no voice-content recording.
- First meaningful action and time-to-activation calculation.
- Seven-day and 30-day retention cohorts.
- Active-day, return, inactivity, and participation-trend calculations.
- Channel and event contribution to activation and retention.
- Newcomer, inactive-member, returning-member, milestone, and recognition-candidate views.
- Staff-only detailed member profiles.
- Member-accessible self view for their own milestones and retained activity data.
- Configurable excluded channels, retention windows, deletion, export, and tracking-disclosure settings.
- `/fetch member`, `/fetch activity`, `/fetch newcomers`, `/fetch inactive`, `/fetch milestones`, `/fetch retention`, and `/fetch community-pulse`.
- Baselines that distinguish message spam from varied healthy participation.

### Zariya overlap handling

Krubit calculates facts and trends. Zariya owns sentiment, relationship context, interpretation, outreach decisions, recognition wording, and the conclusion that a person may need support. Krubit must not assign personality, loyalty, mental-health, or guilt labels.

### Exit gate

Advance when cohort calculations reproduce known fixtures, channel exclusions are enforced before storage, member deletion removes derived records as specified, detailed profiles are access-controlled, and the system cannot expose private-channel activity to unauthorized viewers.

## Phase 5: Zariya Companion Bridge and Supervised Protection

### Goal

Make Krubit materially improve Zariya's effectiveness and remove permanent overlap with her existing deterministic observation tooling.

### Deliverables

- Structured immediate alerts for critical safety and server-health conditions.
- Daily briefing payload: new members needing review, active incidents, failed integrations, important configuration changes, upcoming creator events, and recognition candidates.
- Weekly factual community-pulse payload with observation windows, sources, confidence, and missing evidence.
- Incident handoff with exact event provenance and recommended urgency.
- Governed action proposals compatible with KSHQ's approval and receipt model.
- Pre-authorized reversible actions: temporary slowmode, quarantine role, mass-mention restriction, compromised-webhook disablement, and restoration of the prior recorded state.
- Explicit incident modes with configured triggers and maximum duration.
- Zariya override, dismissal, escalation, and correction feedback returned to Krubit for audit and rule tuning.
- `/fetch briefing`, `/fetch attention`, `/fetch weekly-report`, `/fetch pack`, and `/fetch clear`.

### Migration from Zariya

1. Run Krubit and Zariya's current observer/auditor in parallel shadow mode.
2. Compare event coverage, redaction, deduplication, factual classifications, snapshots, and access limitations.
3. Make Krubit the source for server snapshots, deterministic detection, factual metrics, and evidence packets.
4. Change Zariya's community runtime to consume Krubit contracts rather than duplicate raw collection.
5. Retain Zariya's community judgment, moderation triage, engagement design, sentiment interpretation, member conversation, and memory.
6. Disable the superseded Zariya collection path only after rollback and parity tests pass.

### Exit gate

Advance when one event produces one owner and one receipt; Zariya can trace every briefing claim to Krubit evidence; KSHQ rejects stale, mismatched, or unauthorized proposals; reversible protection restores the exact prior state; and disabling Krubit leaves Zariya conversationally available without fabricated monitoring claims.

## Phase 6: Community Operations Automation

### Goal

Extend Krubit from monitoring into non-conversational community logistics that support Zariya's plans.

### Deliverables

- Event reminders, attendance, temporary roles/channels, cleanup, and post-event impact reports.
- Member milestone and join-anniversary qualification.
- Staff-approved branded recognition cards.
- Showcase submission, attribution, review, scheduling, and archive workflow.
- Looking-for-group and creator-collaboration boards using forms, buttons, and structured listings rather than chat.
- Role automation for external membership, events, creators, achievements, and expiring access.
- Invite/campaign attribution.
- Onboarding and notification experiments with non-overlapping cohorts.
- Native Discord polls used for simple voting; Krubit adds workflow only when approval, anonymity, branching, export, or analysis is required.

### Zariya overlap handling

Zariya designs events, rituals, recognition standards, and engagement strategy. Krubit schedules, qualifies, publishes approved cards, manages temporary infrastructure, and measures results.

### Exit gate

Advance when scheduled automation is idempotent, temporary resources always have bounded cleanup, member recognition is reviewable, attribution is reproducible, and experiments can be stopped without corrupting the activity ledger.

## Phase 7: Commercial Beta and General Availability

### Goal

Turn the validated Krucial Town companion into a supportable creator-community product.

### Beta deliverables

- Guided installation and permission health wizard.
- Web control center for channels, roles, creators, policies, cards, retention, backups, and integrations.
- Free, Pro, and Business entitlement enforcement.
- Usage metering without storing unnecessary message content.
- Custom card branding and creator-community templates.
- Import/migration helpers for supported notification and role configurations from common bots where technically permitted.
- Billing integration, cancellation behavior, grace periods, and data export.
- Operational dashboards, rate-limit monitoring, backup/restore drills, and support diagnostics.
- Tenant-aware incident response and data-deletion procedures.
- Public privacy documentation, retention defaults, command disclosures, and administrator responsibilities.
- External beta cohort with explicit feedback and rollback procedures.

### General-availability gate

Krubit is ready for general sale only when external beta servers demonstrate tenant isolation, predictable notification delivery, bounded support burden, successful uninstall/data deletion, understandable permission requests, stable cost per active guild, and no unresolved high-severity safety or privacy findings.

## Recommended Product Packaging

### Free

- Limited creator feeds.
- Basic live cards.
- `/fetch live`, `/fetch latest`, and basic health.
- Short configuration-change history.

### Pro

- Multiple creators and social feeds.
- Streaming roles and Scheduled Event synchronization.
- Notification controls and analytics.
- Configuration snapshots and extended history.
- Milestones, showcase, and basic member-retention reports.

### Business or Community Operations

- Entry Sniffing and incident evidence.
- Advanced Member Activity Ledger and retention cohorts.
- Reversible safety automation.
- Staff workflows and extended audit retention.
- Custom policies, exports, and KSHQ/Zariya-style companion integrations.

Safety essentials should not be deliberately weakened to force an upgrade. Paid differentiation should primarily come from scale, retention depth, workflow sophistication, integrations, customization, and reporting.

## Initial Success Measures

### Product reliability

- Notification delivery success rate.
- Duplicate-notification rate.
- Time to surface connector or permission failures.
- Scheduled-role and event cleanup success.
- Action receipt completeness.

### Community outcome

- Onboarding completion.
- Time to first meaningful action.
- Seven-day and 30-day newcomer retention.
- Percentage of newcomers reaching an activation milestone.
- Event participation and repeat participation.

### Safety quality

- Entry Sniff false-positive review rate.
- Detection-to-Zariya alert time.
- Percentage of alerts containing complete evidence.
- Temporary protections automatically cleared on time.
- Unauthorized sensitive-action attempts successfully denied.

### Zariya effectiveness

- Briefing claims traceable to evidence.
- Reduction in raw events Zariya must inspect.
- Percentage of surfaced items Zariya marks useful.
- Absence of duplicate Krubit/Zariya responses or decisions.
- Time from significant event to informed human interaction.

## First Build Slice

The first implementation plan should cover Phase 0 only. It should produce a minimal installable Krubit that can connect to isolated test guilds, ingest and deduplicate events, authorize `/fetch status`, render test cards, store redacted receipts, and emit a schema-valid test signal for Zariya without performing moderation or member profiling.

Creator feeds, Entry Sniffing, the Member Activity Ledger, and Zariya runtime migration should each receive separate implementation plans after the preceding phase passes its exit gate.

