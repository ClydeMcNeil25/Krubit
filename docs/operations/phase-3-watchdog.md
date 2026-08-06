# Phase 3 Watchdog — Operations Guide

This guide covers the Phase 3 Watchdog build (Tasks 1-9 of the
[Phase 3 Watchdog design](../superpowers/specs/2026-08-05-phase-3-watchdog-design.md)):
Entry Sniffing, bounded post-join watch windows, raid/spam-wave/webhook-abuse/
permission-risk detection, redacted evidence, AutoMod correlation, live gateway
wiring, and the staff-only `/fetch sniff`-family command surface.

> **Read this before enabling anything in this guide.** Watchdog carries **zero**
> autonomous moderation authority in this phase — it only records, preserves evidence,
> increases monitoring temporarily, notifies staff, and recommends a reversible action
> for a human to take. It cannot kick, ban, timeout, delete a message, or remove a
> role; see [Structural no-moderation-authority proof](#structural-no-moderation-authority-proof)
> below for how that is verified, not merely claimed. Six known limitations change
> what "enabled" actually means for an operator; they are called out prominently in
> [Known limitations](#known-limitations-that-change-what-enabling-watchdog-actually-does)
> below, most importantly
> [a single-member INCIDENT-band join never triggers a real-time notification](#gap-1-a-lone-incident-band-join-is-never-notified-in-real-time-the-single-biggest-functional-gap) —
> this is the largest gap in the build and directly limits the design doc's "notified
> of risk quickly" goal for exactly the case a real raid scout would exploit first: one
> quiet member joining alone.

## What this build adds

- **Entry Sniff**: one durable, versioned risk assessment per join
  (`entry_sniff_assessments`), deterministic from account age, bot/system flags, join
  velocity, join-cluster similarity, invite source where exposed, coarse profile-
  pattern indicators, Rules Screening state where observable, and the guild's own
  allow/block lists.
- **Post-join watch window** (`watch_windows`): a bounded, auto-expiring elevated-
  monitoring state opened for `watch` band or higher, never for `clear`. While open,
  Krubit inspects the member's own guild-channel messages (never DMs) for mass
  mentions, malicious-link shape, near-duplicate repeated messages, and timing
  correlation with other watched members.
- **Four guild-scoped detectors**, run once per sweep cycle (every 60 seconds —
  `KrubitBot.watchdog_sweep_cycle`, `@tasks.loop(seconds=60)` in `src/krubit/discord/
  bot.py`): `RaidDetector`, `SpamWaveDetector`, `WebhookAbuseDetector`,
  `PermissionRiskDetector` — each produces an `Incident` (`incidents` table) and one
  staff notification when it fires, unless a same-kind incident already fired within
  that detector's own correlation window (a per-detector cooldown guard — see
  [Known limitations](#known-limitations-that-change-what-enabling-watchdog-actually-does)
  for its bound).
- **Evidence packets**: named signals, confidence/uncertainty per signal, message
  links, and event IDs, always passed through `redact()` before storage — see
  [Evidence packets are not durably recoverable in full](#gap-4-evidence-packets-are-not-durably-recoverable-in-full)
  for the honest limit on what is actually stored.
- **Staff-only commands**: `/fetch sniff <member>`, `/fetch sniff-report`,
  `/fetch incident <incident_id>`, `/fetch evidence <incident_id>`, `/fetch watchlist`
  — all ephemeral, all read/report-only, gated on Manage Guild.
- **Health integration**: `/fetch server-health` and `/fetch integrations` now report
  Watchdog capability facts (enabled/disabled, notification delivery enabled/disabled,
  Message Content intent available/unavailable) via `HealthService.server_health`/
  `integration_health`'s new `watchdog=` parameter
  (`src/krubit/services/health.py::WatchdogHealthFacts`).
- **AutoMod correlation**: Krubit correlates Discord AutoMod's own
  `on_automod_rule_create`/`on_automod_rule_update`/`on_automod_rule_delete`/
  `on_automod_action` event handlers (`KrubitBot` in `src/krubit/discord/bot.py`;
  referenced by handler name rather than line number here since line numbers drift —
  search the file for these method names) into evidence rather than re-implementing
  keyword/spam enforcement. This needs Discord to actually dispatch the underlying
  `AUTO_MODERATION_ACTION_EXECUTION`/rule-CRUD gateway events, which requires the
  `auto_moderation_configuration`/`auto_moderation_execution` intents —
  `phase_three_intents()` requests both. Both are **non-privileged**: no Developer
  Portal toggle is needed for them, unlike Message Content below. The correlation
  block itself is also gated on `watchdog_enabled`, matching every other Watchdog
  data-producing path — disabling the flag stops it from reading watch windows or
  writing a correlation receipt, even though the underlying `automod_action_executed`
  event ingestion (a Phase 1 behavior, not Watchdog-specific) still runs.

None of this is reachable in production until an operator opts in. Two independent
flags gate it, both fully enforced end to end at the point they matter — not just
parsed:

```dotenv
KRUBIT_WATCHDOG_ENABLED=false
KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=false
```

`KRUBIT_WATCHDOG_ENABLED=false` means every public method on `WatchdogRuntime`
(`on_member_join`, `on_message`, `on_webhooks_update`, `sweep_cycle`,
`bootstrap_guild`) returns immediately as its first statement
(`src/krubit/discord/watchdog_runtime.py`), and `KrubitBot.setup_hook` never starts
`watchdog_sweep_cycle`. `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=false` means
`WatchdogRuntime.notify_staff` — the single choke point every incident notification
runs through — returns before resolving a guild, a channel, or touching the Discord
API at all. Enabling one flag does not enable the other: an operator can run full
detection in shadow (accumulating assessments, watch windows, and incidents) while
never sending a single staff notification, matching the design doc's "shadow mode
first" product decision.

The `/fetch sniff`-family commands themselves are **not** gated by either flag — they
are always registered and will simply report empty/near-empty results (no
assessments, no open windows, no incidents) if Watchdog was never enabled to produce
data in the first place.

## Environment variables (exact names, matching `src/krubit/config.py`)

| Variable | Required | Purpose |
|---|---|---|
| `KRUBIT_WATCHDOG_ENABLED` | Optional, default `false` | Master detection flag — join assessment, watch windows, message inspection, sweep-cycle detectors |
| `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED` | Optional, default `false` | Master staff-notification delivery flag, independent of the flag above |
| `KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS` | Optional, positive integer | Overrides the default 24-hour watch-window duration (`WATCH_WINDOW_DURATION` in `src/krubit/services/watch_window.py`) |
| `KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL` | Optional, must be `https://` if set | **Forward-declared only — see [Gap 5](#gap-5-the-zariya-bridge-url-has-no-consumer) below; nothing reads this value in this build** |

`Settings.from_env` validates `KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS` as a positive
integer and `KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL` as an `https://` URL when either is
set, but a missing or unset value never blocks startup — both fall back to safe
defaults. `.env.example` in the repository root lists the same four names.

## Discord Developer Portal: enabling the Message Content privileged intent

The post-join watch window's message-based signals (mass mentions, malicious-link
shape, repeated messages, spam-wave correlation) require Discord's privileged
**Message Content** intent, which no phase before Phase 3 requests
(`src/krubit/discord/install.py::phase_two_intents()` only requests `guilds`,
`members`, `presences`). `phase_three_intents()` adds `message_content` **and** the
non-privileged `messages` intent additively — `message_content` alone only controls
whether `content`/`embeds`/`attachments`/`components` populate on a dispatched
`MESSAGE_CREATE` event; it does not itself cause that event to be dispatched, so
`messages` must also be requested or `on_message` never fires at all for guild text
channels.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications),
   select Krubit's application, open **Bot**.
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
3. Save changes.
4. Set `KRUBIT_WATCHDOG_ENABLED=true` in the master `.env` and restart Krubit through
   `scripts/invoke-krubit.ps1 run`.

`KrubitBot.__init__` only requests `phase_three_intents()` (which includes
`message_content`) when `settings.watchdog_enabled` is `True`
(`src/krubit/discord/bot.py`'s `KrubitBot.__init__` docstring for
`request_message_content_intent`) — a deployment that never opts into Watchdog is
never asked to enable this privileged intent at all, and never needs Discord bot
verification (required only once a bot serves 100+ guilds, not a near-term concern
here).

**If you forget to enable the intent in the Portal before setting
`KRUBIT_WATCHDOG_ENABLED=true`:** Discord's gateway rejects the connection with
`discord.PrivilegedIntentsRequired`. `krubit.__main__._run_bot` catches exactly this
and retries once with `request_message_content_intent=False`, so Krubit still starts
— but degrades to **join-signal-only detection**: `WatchdogRuntime.on_message` becomes
permanently a no-op (`message_content_available` is derived from
`self.intents.message_content` after the successful connect, so it is `False` for the
life of that process) until you enable the intent in the Portal and restart. This
matches the design doc's "degrades honestly... rather than failing to start" clause.
`/fetch server-health`/`/fetch integrations` will report
`watchdog_message_content_unavailable` (`warning`) whenever this is the case.

## Prerequisites (Discord side, in addition to Phase 1/2A/2 requirements)

1. Message Content privileged intent enabled in the Developer Portal (above), before
   setting `KRUBIT_WATCHDOG_ENABLED=true`.
2. `KRUBIT_STAFF_CHANNEL_ID` configured (already required for Phase 1 daily
   summaries) if `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=true` — `notify_staff`
   resolves this channel and requires **View Channel**, **Send Messages**, and
   **Embed Links** on it; if any is missing, the notification is silently dropped
   rather than raising (matching every other notify-path's degrade-quietly
   convention). No new Discord permission scope is requested for Watchdog itself —
   detection and evidence never require a mutation permission.
3. No allow/block-list UI exists yet in this build; `guild_allow_block_lists` rows
   must be inserted directly against the SQLite database until a `/fetch` command for
   it is built in a future phase.

## Shadow mode

1. Set `KRUBIT_WATCHDOG_ENABLED=true`, leave `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=false`.
2. Entry Sniff assessments, watch windows, and incidents all accumulate normally and
   are queryable via `/fetch sniff`/`/fetch sniff-report`/`/fetch incident`/
   `/fetch evidence`/`/fetch watchlist` (all staff-only, ephemeral) — nothing is ever
   posted publicly or pushed to the staff channel.
3. Use this period to sanity-check false-positive rate against real join/message
   traffic (the design doc's "benign join surge must not blanket-flag clean members"
   requirement) before flipping notifications on.
4. Set `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=true` only once you have reviewed at
   least a few days of shadow-mode `/fetch sniff-report` output and are comfortable
   with what would have been sent.

## Structural no-moderation-authority proof

The Completion Gate requires "Krubit cannot execute an unapproved moderation action"
to be verified **structurally**, not just by behavioral test coverage.
`tests/test_watchdog_structural_safety.py::test_no_watchdog_module_imports_a_moderation_mutation_client_method`
scans the source text of every Watchdog module for a call to `kick(`, `ban(`,
`unban(`, `timeout(`, `delete_messages(`, `remove_roles(`, `add_roles(`, `edit(`,
`delete(`, `purge(`, or `set_permissions(` and fails the build if any is found —
regardless of whether that call would ever actually execute. The set was widened
beyond the original five names (`kick`/`ban`/`timeout`/`delete_messages`/
`remove_roles`) in the final whole-branch review: `add_roles` (auto-assigning a
quarantine/unverified role), `edit` (discord.py's canonical `member.edit(timed_out_
until=...)` timeout API, plus channel mutation via `channel.edit(...)`), `delete`/
`purge` (the common single-message and bulk-message deletion forms, as opposed to
only the bulk `delete_messages` API), and `unban` were all gaps in the original set.

"Every Watchdog module" here is the union of two sets, not just a filename glob: the
five `src/krubit/**/watchdog*.py`-named modules, **plus** an explicitly maintained
list of the Task 3-6 Watchdog service modules whose filenames do not start with
`watchdog` — `services/entry_sniff.py`, `services/watch_window.py` (the module that
reads live message content), `services/raid_detection.py`,
`services/webhook_and_permission_risk.py`, and `services/incident_evidence.py`. An
earlier version of this test scanned only the filename glob and silently missed all
five of those service modules; a sibling test,
`test_explicit_watchdog_modules_still_exist`, guards the explicit list against going
stale on a future rename. This test passed on the first run in this session against
the corrected, widened file set, confirming Tasks 1-8 introduced no forbidden call in
any of the ten modules now covered; it re-runs on every future change to any of them.
**A future task that adds a new Watchdog service module without a `watchdog`-prefixed
filename must add it to `_EXPLICIT_WATCHDOG_MODULES` in the test file** — the glob
alone will not discover it.

## Known limitations that change what "enabling Watchdog" actually does

### Gap 1: A lone INCIDENT-band join is never notified in real time (the single biggest functional gap)

`WatchdogRuntime.on_member_join` (`src/krubit/discord/watchdog_runtime.py`) calls
`EntrySniffService.assess_join` and `WatchWindowService.open_if_warranted` — exactly
Task 4's two calls — and nothing else. **It never constructs an `Incident` or calls
`notify_staff`, no matter how high the resulting risk band is.** Only the sweep-cycle
detectors (`RaidDetector`, `SpamWaveDetector`, `WebhookAbuseDetector`,
`PermissionRiskDetector`) ever produce an `Incident`/staff notification. The domain
enum `IncidentKind.MEMBER` (`src/krubit/domain/watchdog.py`) exists but is
constructed **nowhere** in this codebase — confirmed by grep across `src/krubit`.

**Practical consequence:** a single raid participant who trips the `INCIDENT` band on
join (for example, a brand-new account joining seconds after account creation, with a
default avatar, during an active join-velocity spike) but never posts a follow-up
message and isn't part of a multi-member cluster the sweep-cycle raid detector
notices, sits **silently** in `entry_sniff_assessments` and (if `watch`-band-or-higher)
`watch_windows` — visible only if staff proactively run `/fetch sniff <member>` or
`/fetch sniff-report`. This directly undercuts the design doc's stated goal ("Zariya
and staff are notified of risk quickly") for exactly the single-member case, and is
comparable in severity to Phase 2's "Meta/TikTok connectors not scheduled" gap. A
future task must add an `on_member_join` path that constructs an `Incident` (using
`IncidentKind.MEMBER`) and calls `notify_staff` when `assess_join` returns `INCIDENT`
band, mirroring the sweep-cycle detectors' own pattern.

### Gap 2: Two in-memory, per-process detector caches lose all state on restart

`SpamWaveDetector.record_message` and `WebhookAbuseDetector.record_webhook_event`
(`src/krubit/services/raid_detection.py`, `src/krubit/services/webhook_and_permission_risk.py`)
both accumulate same-window correlation state in an in-memory, per-process cache with
**no persistence** to SQLite. A bot restart mid-attack — the exact moment a raid or
spam wave is most likely to be underway — silently discards everything already
accumulated for both detectors; detection resumes from empty state on the next
message/webhook event after restart, with no durable trace that anything was lost.

### Gap 3: Entry Sniff join-cluster signals depend on the live gateway member cache, not durable storage

`join_velocity`/`join_cluster_similarity` (`src/krubit/discord/watchdog_events.py`,
documented in `src/krubit/services/entry_sniff.py`'s module docstring, "gateway member
cache, not a durable store") read `discord.Guild.members` — the bot's own already-
cached member list from the live gateway connection — not a database table. Right
after a bot restart, before Discord repopulates this cache via `GUILD_CREATE`/member
chunking, these two signals are weak or silent — which is exactly the highest-risk
moment for a real raid to be exploited (an attacker who can predict or trigger a
restart gets a detection blind spot for the join-velocity/cluster signals
specifically; every other Entry Sniff signal — account age, bot flag, invite source,
profile pattern, Rules Screening state, allow/block lists — is unaffected).

### Gap 4: Evidence packets are not durably recoverable in full

No storage table from Tasks 1-7 persists a full `EvidencePacket` — its `signals`
(with per-signal weight/confidence/detail), `message_links`, and `event_ids` are never
written wholesale to any table. `Incident.evidence_packet_id` is only an opaque
identifier (`src/krubit/services/raid_detection.py::_default_evidence_builder` and its
siblings in `webhook_and_permission_risk.py`). The one durable trace of *which named
signals* contributed is the `incident_recorded` `SniffReceipt` every incident-
producing detector writes (`detail["signal_names"]`, already redacted before storage).

`/fetch incident` and `/fetch evidence` (`src/krubit/discord/watchdog_commands.py`)
read that receipt and **reconstruct** one placeholder `RiskSignal` per recovered
signal name, with a fixed, code-authored detail string — never a fabricated number
presented as real data — and the rendered card discloses that this is a
reconstruction, not the original packet. This is the honest choice made in Task 8's
review (present a labeled reconstruction rather than invent per-signal weight/
confidence/detail that was never stored), but it means **real per-signal evidence
detail is not durably recoverable today**. A future task must add a dedicated
evidence-packet storage table before `/fetch evidence` can show genuine stored detail
instead of a reconstruction.

### Gap 5: The Zariya bridge URL has no consumer

`watchdog_zariya_bridge_url` (`Settings`, sourced from
`KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL`) is validated at startup (must be `https://` if
set) but is **never read anywhere else in this codebase** — confirmed by grep; no
module constructs an HTTP client against it or otherwise consumes it. "Notify Zariya,"
one item on the design doc's automatic-authority list, is therefore **not
implemented** in this build. Only "notify staff" (`WatchdogRuntime.notify_staff`,
sending to the configured Discord staff channel) exists. Do not configure this
variable expecting it to do anything; it is a forward declaration for a future task.

### Gap 6: Spam-wave correlation reads message content from EVERY guild member, not just watched ones — a deliberate, but privacy-relevant, exception to the general rule

**Read this before enabling `watchdog_enabled` in a privacy-sensitive deployment.**
The plan's stated global constraint (and this guide's own framing) is "Krubit reads
message content only for a member with an actively open watch window." Spam-wave
detection is a deliberate, design-doc-sanctioned exception to that rule:
`SpamWaveDetector.record_message` (fed by `WatchdogRuntime` from `KrubitBot.
on_message`, `src/krubit/discord/watchdog_runtime.py` / `src/krubit/services/
raid_detection.py`) is called for **every non-bot guild message**, regardless of
whether the sender has an open watch window, a prior Entry Sniff assessment, or has
ever been flagged at all. Each message is normalized (stripped, lowercased) and held
in an in-memory, per-guild cache for up to 5 minutes (`_SPAM_WAVE_WINDOW`) so
near-duplicate posts from otherwise-uninvolved members can be correlated into a single
coordinated spam-wave signal — the whole point of spam-wave detection is catching a
shared payload blasted by several members who individually look unremarkable, which
is structurally impossible if only already-watched members' messages are visible to
it.

### Gap 7: Per-detector cooldown can suppress notification of a genuinely new, distinct incident of the same kind

A whole-branch review found that without any cooldown, a single ongoing incident
(e.g. one raid) could re-fire a fresh `Incident` row and a fresh staff notification
every 60-second sweep cycle for its entire correlation window (`_RAID_WINDOW`,
`_ROLE_GRANT_LOOKBACK`, etc. — up to 30 minutes for `PERMISSION_RISK`), producing a
notification storm for one real event. The fix added `_has_open_incident_of_kind`
(`src/krubit/services/raid_detection.py`, mirrored in
`webhook_and_permission_risk.py`): each detector now skips creating a new incident
and notification if an incident of the *same kind* already exists for the guild with
`opened_at` inside the detector's own correlation window.

**This closes the storm, but reopens risk in the opposite direction.** `Incident` has
no open/closed state — the cooldown check cannot distinguish "this is still the same
ongoing incident" from "a second, entirely unrelated incident of the same kind just
started, coincidentally inside the first one's window." Concretely: if a raid opens
at T=0 (10-minute window), and a second, unrelated raid with different members
crosses the threshold at T=9min, the cooldown sees the first raid's `opened_at`
inside its cutoff and **suppresses the second raid's notification** for up to the
remainder of that window. The same shape applies to spam-wave, webhook-abuse, and
permission-risk detection (worst case up to 30 minutes for the latter).

**Mitigation today:** the first incident's evidence and notification are unaffected —
only a *second, same-kind, same-window* incident goes unnotified in real time. Staff
can still discover it via `/fetch sniff-report` (which reads `entry_sniff_assessments`
and `watch_windows` directly, independent of the `incidents` table) or by noticing
elevated activity manually. This is a real, accepted trade-off between "notification
storm" and "possible missed second alert," not a silent data-loss bug — the
underlying signals that would have produced the second incident are never evaluated
against a dedup key finer than `(guild_id, kind)`, so a future fix (e.g. scoping the
cooldown to overlapping members/channels rather than guild+kind alone) should narrow
this before Phase 3 is trusted for high-volume/high-target guilds.

**What is and is not retained:** nothing from this cache is ever persisted to
`SQLiteStore` — only signal *names* (never message content) reach durable storage via
the `incident_recorded` receipt, matching Gap 4 above. The normalized message excerpt
itself lives only in a transient in-memory `RiskSignal.detail` and the bounded
in-memory cache, both gone on process restart (see Gap 2). This is not a data-
retention problem — it is a genuine, unresolved tension between two governing
documents (the design doc's spam-wave bullet explicitly authorizes "multiple
currently-watched (or even `clear`) members," while the same design doc's own global
constraint says message content is read only for watched members) about reading
message content from members who were never individually flagged. Evaluate this
trade-off explicitly before enabling `watchdog_enabled` anywhere message-content
privacy expectations are strict — this is a rollout-gate decision for a human, not
something the code resolves on its own.

### Also worth a human sanity check: `mass_mentions` HIGH tier / a lone `@everyone` ping can reach `SUSPICIOUS` alone

Every message signal except one requires stacking with another signal to cross the
`SUSPICIOUS` threshold (design intentionally, per `src/krubit/discord/watchdog_events.py`'s
"Message-signal thresholds" section). The one deliberate exception is `mass_mentions`
at its HIGH tier (15+ combined user/role mentions) or any `@everyone`/`@here` ping,
which reaches `SUSPICIOUS` band on its own. **This does not account for the sender's
own `mention_everyone` Discord permission** — a legitimate community manager's
scheduled announcement ping fires this signal identically to an attacker's. This is
flagged, not fixed, in this build: worth a human sanity check (and likely a permission-
aware carve-out) before this signal is trusted to drive an unattended notification/
escalation flow rather than a staff-reviewed one.

## Commands

Staff-only (Manage Guild), all ephemeral:

```text
/fetch sniff <member>
/fetch sniff-report
/fetch incident <incident_id>
/fetch evidence <incident_id>
/fetch watchlist
```

None of these commands can mutate a member, role, message, or channel — they are
read/report surfaces only, backed by the same structural proof in
[Structural no-moderation-authority proof](#structural-no-moderation-authority-proof)
above.

## Rollback

1. Set `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=false` (stop notifications only) and/or
   `KRUBIT_WATCHDOG_ENABLED=false` (stop detection entirely) in the master `.env`.
2. Restart with `& scripts/invoke-krubit.ps1 run`.
3. Confirm `/fetch integrations`/`/fetch server-health` report `watchdog_disabled` (if
   fully disabled) and no new staff notification occurs.
4. Keep `entry_sniff_assessments`, `watch_windows`, `incidents`,
   `guild_allow_block_lists`, and `sniff_receipts` — they are additive and safe to
   retain during rollback, matching every prior phase's rollback discipline. Disabling
   `KRUBIT_WATCHDOG_ENABLED` does not close already-open watch windows in storage;
   they simply stop being swept and will show as open (though stale) via
   `/fetch watchlist` until re-enabled.

## Data deletion

No dedicated `/fetch` command purges Watchdog data for one member. For a full
data-deletion request (member or Discord's own deletion requirement), follow the
existing [Privacy Policy](../PRIVACY_POLICY.md) section 10 process — an operator must
delete the relevant rows directly from `entry_sniff_assessments`, `watch_windows`,
`incidents`, and `sniff_receipts`, scoped by `guild_id`/`member_id`, until a dedicated
deletion command exists. Never delete `data/krubit.db`, its WAL, or its SHM file as a
substitute — that destroys unrelated Phase 0/1/2/2A records for every guild.

## Related documents

- [Phase 3 Watchdog design](../superpowers/specs/2026-08-05-phase-3-watchdog-design.md)
- [Phase 1 operations guide](phase-1-operations.md)
- [Phase 2 creator signal hub operations guide](phase-2-creator-signal-hub.md)
- [Phase 2 completion audit](phase-2-completion-audit.md) (the tone/rigor model this
  guide's known-limitations sections follow)
- [Product rollout](../roadmaps/2026-08-03-krubit-phase-rollout.md)
- [Privacy Policy](../PRIVACY_POLICY.md)
