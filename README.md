# Krubit

Krubit is Zariya's non-conversational Discord pet and operational companion. He fetches
functional system results, records auditable events, and provides a safe foundation for later
creator notifications, server monitoring, Entry Sniffing, and member activity measurement.

## Capabilities reference for moderation support

This section is written for Zariya (or any AI employee using Krubit for moderation support) to
read once and use as a working reference. It summarizes what Krubit can tell you, how to ask for
it, and — just as important — what Krubit will never do.

**The boundary, stated plainly:** Krubit does not moderate. It never kicks, bans, times out,
deletes a message, or removes a role — Phase 3 (Watchdog) enforces this structurally, not just
behaviorally (`tests/test_watchdog_structural_safety.py`). Krubit's job everywhere is to detect,
record, and surface facts and evidence; every punitive or corrective decision belongs to a human
staff member (or to you, Zariya, acting through your own judgment and tools) — never to Krubit
itself. If you need something *done* about a member, Krubit can hand you the evidence; it cannot
take the action.

**Commands most relevant to moderation support** (all staff-only and ephemeral unless noted):

- `/fetch sniff <member>` — on-demand risk assessment for one member (account age, join
  velocity, join-cluster similarity, profile-pattern indicators, allow/block lists).
- `/fetch sniff-report` — recent Entry Sniff assessments across the guild.
- `/fetch incident <incident_id>` and `/fetch evidence <incident_id>` — redacted, evidence-backed
  detail on a raid, spam-wave, webhook-abuse, or permission-risk incident Watchdog flagged.
- `/fetch watchlist` — members currently inside a bounded post-join watch window.
- `/fetch member <member>`, `/fetch activity [member]`, `/fetch milestones [member]` — factual
  participation history (Phase 4); `/fetch newcomers`, `/fetch inactive`, `/fetch retention`,
  `/fetch community-pulse` for guild-wide views. Useful for context (is this a brand-new account
  acting oddly, or a long-tenured member?) but never a behavior verdict.
- `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations` — is
  Krubit itself healthy, and are Watchdog/Activity Ledger even turned on right now.

**What none of this is:** risk bands, incidents, and watch-window flags are inputs to your
judgment, not conclusions. A `/fetch sniff` result is "these signals were observed," never "this
member should be removed." Read the linked operations guides below (especially the Phase 3 and
Phase 4 guides' "known gaps" sections) before relying on any of this in a live moderation
situation — several detection paths have real, documented blind spots (e.g. a lone risky join is
never pushed to staff in real time; only sweep-cycle incidents are).

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

- A guild-scoped creator registry
  (`/fetch creator add|remove|list|show|verify|pause|resume|route|transfer|template`)
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
  only by exact stored event ID — implemented and tested, but **not yet called from
  the running process** (see the production gaps below)
- `/fetch latest`, `/fetch schedule`, `/fetch notifications`, and
  `/fetch notifications preview|retry|retract`
- The Phase 2A Twitch/Discord-presence path migrated behind these same shared contracts
  without changing its accepted presentation or safety guarantees

Every Phase 2 surface defaults off: `KRUBIT_CREATOR_SIGNALS_ENABLED` and
`KRUBIT_SOCIAL_DELIVERY_ENABLED` both default to `false`, and neither flag is
decorative — leaving both at their default means the connector polling scheduler never
starts and no Discord message is ever sent, even if accounts are enrolled, resumed, and
routed. Every missing per-platform credential simply leaves that capability at
`unconfigured` rather than blocking startup. Two production gaps remain even with
both flags enabled: Instagram, Facebook, Threads, and TikTok connectors are fully
implemented and tested but are **not wired into the polling scheduler in this
build** (they need per-account OAuth credential resolution that is not yet built, so
scheduling them with one shared bot-wide token would fetch every creator's content
under one account's credentials); and Discord **Scheduled Event synchronization has
no production call site** — nothing in the running process ever calls it, so
`/fetch schedule` will always report no Krubit-owned events.

The OAuth/push **callback server now starts** with `krubit run` (bound to
`127.0.0.1` by default, access-logging disabled so OAuth codes/state never reach
stdout), and durably, single-use, account-bound OAuth authorization is fully wired
for **TikTok, Instagram, and Threads**, plus Meta's deauthorization and
legally-required data-deletion webhook contract for every Meta capability. Two
things are still missing before this is end-to-end for an operator: the "click to
authorize" link a creator actually clicks is not yet generated anywhere (that's the
per-account credential resolution work above); and **Facebook Page and Facebook
Profile authorization are deliberately not supported** — neither connector can
independently confirm which Facebook account granted access without a proper
Page-token exchange this build doesn't implement, so those two capabilities cleanly
reject every authorization attempt rather than silently accepting one bound to the
wrong account. See the
[Phase 2 operations guide](docs/operations/phase-2-creator-signal-hub.md) for the
full operator runbook, the
[Phase 2 completion audit](docs/operations/phase-2-completion-audit.md) for exactly
what is evidenced versus deferred to a credentialed operator, and the
[Phase 2 callback server design](docs/superpowers/specs/2026-08-07-phase-2-callback-server-design.md)
and
[development log](docs/devlogs/2026-08-08-phase-2-callback-server.md)
for the callback server's own scope and known limitations.

## Phase 3 capabilities

- One-time Entry Sniff join assessment (`entry_sniff_assessments`): deterministic,
  explainable risk-band evaluation from account age, bot/system flags, join velocity,
  join-cluster similarity, invite source where exposed, profile-pattern indicators,
  Rules Screening state, and guild allow/block lists
- Bounded, automatically-expiring post-join watch window (`watch_windows`) inspecting
  only guild-channel messages (never DMs) for mass mentions, malicious-link shape,
  repeated messages, and coordinated timing
- Guild-scoped raid, spam-wave, webhook-abuse, and permission-risk detection, each
  producing a redacted evidence-backed `Incident` and one staff notification
- AutoMod event correlation instead of duplicate keyword/spam enforcement
- Staff-only `/fetch sniff <member>`, `/fetch sniff-report`,
  `/fetch incident <incident_id>`, `/fetch evidence <incident_id>`, and
  `/fetch watchlist`, all ephemeral and read-only
- Watchdog capability facts (enabled/disabled, notification delivery
  enabled/disabled, Message Content intent available/unavailable) surfaced through
  `/fetch server-health` and `/fetch integrations`

Phase 3 carries **zero autonomous moderation authority**: it can never kick, ban,
timeout, delete a message, or remove a role — verified structurally, not just by
behavioral test coverage, by
`tests/test_watchdog_structural_safety.py::test_no_watchdog_module_imports_a_moderation_mutation_client_method`.
Both `KRUBIT_WATCHDOG_ENABLED` and `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED` default to
`false` and are independently enforced — detection can run in shadow mode with
notifications still off. **The single biggest known gap in this build:** a lone member
whose join alone crosses the `INCIDENT` risk band is never notified to staff in real
time — only the periodic sweep-cycle detectors (raid/spam-wave/webhook-abuse/
permission-risk) ever send a staff notification; `on_member_join` only records the
assessment. Four further gaps — in-memory-only spam-wave/webhook-abuse correlation
state, join-signal reliance on the live gateway member cache (weak right after a
restart), evidence packets that are reconstructed rather than durably stored in full,
and a `KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL` setting with no consumer ("notify Zariya" is
not implemented, only "notify staff") — are documented in the
[Phase 3 operations guide](docs/operations/phase-3-watchdog.md), which every operator
should read before enabling this phase.

## Phase 4 capabilities

- Per-member, guild-scoped factual participation event ledger (`ledger_events`):
  join, onboarding, message (channel + timestamp only, never text), reaction
  (emoji shape only, never inferred sentiment), voice session (join/leave + computed
  duration, never audio), event attendance (Scheduled Event RSVP), role change,
  milestone, and moderation-receipt pointer
- Deterministic, fixture-reproducing activation/retention/trend calculation
  (`time_to_activation`, `cohort_membership`, `participation_trend`) — pure
  functions, never a black-box engagement score
- Newcomer, inactive-member, returning-member, milestone, recognition-candidate,
  and community-pulse views (six total, matching the design doc's Views section)
- Staff-only `/fetch member <member>`, `/fetch newcomers`, `/fetch inactive`,
  `/fetch retention`, `/fetch community-pulse`, plus staff-or-self
  `/fetch activity [member]` and `/fetch milestones [member]`, all ephemeral
- Channel exclusion enforced structurally *before* storage (verified by
  `tests/test_activity_privacy_structural_safety.py`, not just behavioral coverage),
  a retention sweep, member deletion with a minimal content-free receipt, and
  member data export — see the gaps below for what is and is not reachable from
  a command today
- Activity-ledger capability facts (enabled/disabled, default retention window
  configured/unconfigured) surfaced through `/fetch server-health` and
  `/fetch integrations`

`KRUBIT_ACTIVITY_LEDGER_ENABLED` defaults to `false` and is fully enforced at every
real ingestion call site. **Three known gaps in this build, all given the same
prominence as Phase 3's biggest gap:** (1) `returning_member_view` is fully built
and fully tested but has **zero** production caller — no `/fetch` command in this
nine-task plan surfaces it, so a member who went inactive and then resumed activity
is invisible to staff through any command; (2) `recognition_candidates` has the
identical defect shape — a fully built, fully tested factual shortlist of
notable-activity members with **zero** production caller either; (3) member
deletion, export, and channel-exclusion configuration are fully implemented and
tested at the service/storage layer but likewise have **no** `/fetch` command —
today all three require direct database access. Five further, lower-severity gaps
(stale-presence detection in `/fetch inactive` for high-volume guilds, no
atomicity between deletion and its receipt, an uncapped export in a
never-retention-configured guild, an unseeded env var for channel exclusion, and a
pure calculation function's caller-contract windowing requirement) are documented
in the [Phase 4 operations guide](docs/operations/phase-4-activity-ledger.md),
which every operator should read before enabling this phase.

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
[Phase 3 Watchdog operations guide](docs/operations/phase-3-watchdog.md),
[Phase 3 Watchdog development log](docs/devlogs/2026-08-05-phase-3-watchdog.md),
[Phase 4 Activity Ledger operations guide](docs/operations/phase-4-activity-ledger.md),
[Phase 4 Activity Ledger development log](docs/devlogs/2026-08-06-phase-4-activity-ledger.md),
[signal contract](docs/contracts/krubit-zariya-signal-v1.md), and
[product rollout](docs/roadmaps/2026-08-03-krubit-phase-rollout.md).

When using the workspace-level master `.env`, invoke Krubit through
`scripts/invoke-krubit.ps1` so only Krubit's approved environment variables are loaded.

## Legal documents

- [Privacy Policy](docs/PRIVACY_POLICY.md)
- [Terms of Service](docs/TERMS_OF_SERVICE.md)
