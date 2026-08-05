# Phase 2 Completion Audit

**Audit date:** 2026-08-05
**Audited build:** this branch, through Task 14 (`docs: close Phase 2 creator signal
hub`), on top of Tasks 1-13 of the
[Phase 2 completion design](../superpowers/specs/2026-08-04-phase-2-completion-design.md).
**Auditor environment:** local development sandbox. No Discord bot token, no live
Discord guild, and no real platform (Twitch/YouTube/X/Meta/TikTok/Bluesky) credentials
were available in this session.

## How to read this audit

Every "Completion Gate" line from the design doc is copied verbatim as a heading below,
followed by a verdict and its evidence. Evidence is one of:

- **Evidenced**: a specific automated test name (all of which were run in this session
  — see [Automated verification run in this session](#automated-verification-run-in-this-session)
  below), a specific code path with a file/line reference, or a specific command whose
  output is quoted.
- **Partially evidenced**: the software capability is implemented and tested in
  isolation, but the specific claim in the gate (e.g. an end-to-end live canary) is not
  evidenced by this session; the honest partial status is explained.
- **Requires operator verification (not evidenced here)**: the gate needs a live
  Discord guild, a live Discord bot token, and/or real platform credentials that this
  sandboxed development session did not have access to. No result is claimed for these
  — they are explicitly deferred to a credentialed operator, per the Task 14 brief's
  instruction that overclaiming here would defeat the audit's purpose.

## Automated verification run in this session

```text
.venv\Scripts\python.exe -m pytest -q          -> 575 passed, 0 failed
.venv\Scripts\ruff.exe check .                 -> All checks passed!
.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe  -> 0 errors, 0 warnings
git diff --check                               -> exit 0, no output
```

`scripts\invoke-krubit.ps1 doctor` **does not exist** as a subcommand in this build.
Running `python -m krubit doctor` in this session produced:

```text
usage: krubit [-h]
              {run,init-db,install-url,enable-guild,status,emit-test-signal} ...
krubit: error: argument command: invalid choice: 'doctor' (choose from run, init-db, install-url, enable-guild, status, emit-test-signal)
```

The task brief's Step 5 (`scripts\invoke-krubit.ps1 doctor` and live `/fetch` commands
in a Discord guild) and Step 6 (per-connector production canaries with real
credentials) both require a real Discord bot process connected to a live guild and, for
Step 6, real per-platform credentials. Neither is available in this sandboxed
development session. **These two steps are not evidenced by this session and are not
claimed as complete.** They are operator actions required post-merge; see
[Deferred to operator verification](#deferred-to-operator-verification-not-evidenced-in-this-session)
below.

## Completion Gate line-by-line evidence

### "The full registry, connector, normalized event, policy, delivery, analytics, and command architecture is implemented and tested."

**Evidenced.** Each subsystem has a dedicated storage/service/test module:

- Registry: `src/krubit/services/creator_registry.py`, `src/krubit/storage/creator_rows.py`;
  `tests/test_creator_registry_service.py` (10 tests), `tests/test_creator_registry_storage.py`.
- Connector protocol + catalog: `src/krubit/integrations/base.py`,
  `src/krubit/integrations/catalog.py`; `tests/test_connector_base.py`,
  `tests/test_connector_catalog.py`.
- Normalized content event / ledger: `src/krubit/domain/creator_signals.py`,
  `src/krubit/services/content_signals.py`; `tests/test_content_signal_domain.py`,
  `tests/test_content_signal_service.py`, `tests/test_content_signal_storage.py`.
- Policy: `src/krubit/services/notification_policy.py`;
  `tests/test_notification_policy.py` (32 tests covering quiet hours, mention budgets,
  template validation, atomic claim races).
- Delivery: `src/krubit/discord/content_runtime.py`; `tests/test_content_runtime.py`,
  `tests/test_content_recovery.py`, `tests/test_content_cards.py`.
- Analytics: `src/krubit/services/creator_analytics.py`; `tests/test_creator_analytics.py`.
- Commands: `src/krubit/discord/content_commands.py`, `src/krubit/discord/live_commands.py`,
  `src/krubit/discord/bot.py`; `tests/test_content_commands.py`,
  `tests/test_live_signal_commands.py`.

All listed test files are part of the 575 passing tests recorded above.

### "Every platform and content capability reports an honest operational state."

**Evidenced.** `CATALOG` in `src/krubit/integrations/catalog.py` declares
account/social/live `CapabilityState` for all 10 platforms (`Platform` enum has 10
members: Twitch, YouTube, X, Instagram, Facebook, Facebook Page, Threads, Bluesky,
TikTok, Fanbase). This is now asserted directly against the design doc's platform
capability matrix by `tests/test_phase_2_rollout.py::test_every_catalog_capability_appears_even_when_unconfigured`
(new in this task) and
`tests/test_phase_2_rollout.py::test_no_catalog_capability_is_silently_reported_ready_by_default`,
plus the pre-existing `tests/test_connector_catalog.py::test_every_platform_has_a_connector_descriptor_with_all_three_capabilities`.
Per-connector runtime health is surfaced through `ConnectorHealth` and
`HealthService.creator_health` (`src/krubit/services/health.py`), tested by
`tests/test_creator_health.py`, and rendered without leaking connector-internal detail
by `tests/test_discord_events.py`/`render_connector_health` tests. Fanbase and every
"pending" live capability are `unsupported`/`approval_required`, never `ready`, per the
same tests.

### "Twitch and YouTube Discord-presence detection are durable and duplicate-safe."

**Evidenced for Twitch** (Phase 2A baseline, migrated behind shared contracts in Task
13): `tests/test_live_signal_runtime.py` covers exactly-once announcement
(`test_apply_plan_adds_only_the_dedicated_role_and_announces_once`), bounded-history
recovery (`test_recovered_history_forbidden_keeps_claim_pending_without_sending`), and
restart-safe reconciliation (`test_reconcile_all_applies_plans_without_retaining_background_tasks`).
`tests/test_twitch_content_migration.py::test_migration_is_idempotent_when_run_twice`
proves the migration itself (run on every `krubit run` boot per
`src/krubit/__main__.py::_run_bot`) is safe to repeat.

**Partially evidenced for YouTube.** Presence detection
(`extract_youtube_presence`/`extract_streaming_observation` in
`src/krubit/domain/live_signals.py`) is unit-tested in `tests/test_youtube_presence.py`,
but `extract_streaming_observation` — the function meant to prefer either Twitch or
YouTube presence — **has no production call site yet**;
`src/krubit/discord/live_runtime.py`'s `handle_presence` still calls
`extract_twitch_observation` directly (carried-forward Task 7 limitation). YouTube
duplicate-safety for uploads/live polling is exercised through the shared content
scheduler's cursor/idempotency tests (`tests/test_content_scheduler.py`,
`tests/test_content_recovery.py::test_restarted_scheduler_still_delivers_exactly_once`),
not through a live Discord-presence path for YouTube. **YouTube Discord-presence
detection specifically is implemented but not wired into the running presence handler
in this build** — this is an honest gap, not a passed gate.

### "Configured official APIs detect, classify, and route new content correctly without announcing enrollment history."

**Evidenced for the wiring that exists.** Baseline suppression (no announcement of
pre-existing content on enrollment) is a stated product decision covered by content
ledger baseline/cursor tests (`tests/test_content_signal_service.py`,
`tests/test_creator_registry_service.py::test_new_account_starts_paused` — new accounts
start paused so no content routes until an operator explicitly resumes them). Only
YouTube, X, Bluesky, and Twitch are scheduled in this build
(`src/krubit/__main__.py::_build_content_connectors`); Instagram, Facebook (Page and
profile), Threads, and TikTok are fully implemented and unit-tested against recorded
fixtures (`tests/test_meta_connectors.py`, `tests/test_tiktok_connector.py`) but **have
no production scheduler call site** — see the prominent gap documented in the
[operator runbook](phase-2-creator-signal-hub.md#meta-and-tiktok-are-not-scheduled-in-this-build).
Classify/route correctness for the scheduled platforms is evidenced by
`tests/test_content_runtime.py` and `tests/test_content_scheduler.py`; it is **not**
evidenced end-to-end against a real official API response in this session (recorded
fixtures only — no live credentials available).

### "Strong cross-platform duplicates do not flood Discord, while ambiguous content is not silently suppressed."

**Evidenced.** `tests/test_content_correlation.py` (11 tests) directly covers this
gate: `test_identical_content_identity_merges`,
`test_identical_canonical_url_merges_across_platforms`,
`test_strong_simulcast_matches_on_shared_outbound_link`,
`test_strong_simulcast_matches_on_media_fingerprint` for the "must merge" side, and
`test_ambiguous_crosspost_is_not_merged`, `test_title_similarity_alone_never_merges`,
`test_different_creators_never_merge_even_with_matching_fingerprint`,
`test_outside_correlation_window_never_merges`,
`test_missing_published_at_on_either_side_never_merges_on_probabilistic_grounds` for the
"must not silently suppress" side. Every merge/suppression records its reason via
`CorrelationDecision.reason` (`src/krubit/services/notification_policy.py`).

### "Live roles, announcements, and Scheduled Events recover correctly after restarts."

**Evidenced for live roles/announcements**: `tests/test_live_signal_runtime.py::test_reconcile_all_applies_plans_without_retaining_background_tasks`,
`test_permission_restoration_after_member_terminal_state_stays_quiescent`,
`test_apply_plan_removes_only_the_exact_owned_configured_role`. **Evidenced for
Scheduled Events**: `tests/test_scheduled_event_sync.py::test_restart_recovery_finds_mapping_by_exact_id_never_by_name`
proves recovery never falls back to a mutable-name search, matching the design doc's
"never searches by mutable event name alone" requirement; `test_sync_skips_when_bot_lacks_scheduled_event_permissions`
and `test_sync_permission_loss_blocks_further_mutation_of_an_owned_event` cover
permission loss. All of this is evidenced by unit/integration tests against a fake
Discord layer, not by an actual bot-process restart against a live guild — see
[Deferred to operator verification](#deferred-to-operator-verification-not-evidenced-in-this-session).

### "Quiet hours, batching, routes, and mention budgets are enforced and receipted."

**Evidenced for quiet hours and mention budgets.** `tests/test_notification_policy.py`
covers DST-correct quiet-hours boundaries (`test_quiet_hours_spring_forward_boundary_uses_correct_wall_clock`,
`test_quiet_hours_fall_back_boundary_uses_correct_wall_clock`), atomic mention-budget
claims under concurrency (`test_claim_mention_budget_is_atomic_under_concurrency`,
`test_evaluate_and_claim_never_double_awards_everyone_under_concurrency`), and receipt
recording for every outcome (`test_mention_receipts_record_every_outcome`). Routes are
covered by `tests/test_content_commands.py`'s route tests and
`src/krubit/discord/content_commands.py`'s `creator_route` command.

**Not evidenced: guild-level configuration of quiet hours or mention-budget limits.**
`ContentRuntime` currently uses a `policy_factory` that defaults every guild to
unlimited mention budgets and no quiet hours (Task 6 carried-forward limitation,
documented prominently in the [operator runbook](phase-2-creator-signal-hub.md#notification-policy-guild-configuration-is-not-wired-yet)).
The policy *math* is fully enforced and receipted; a guild's actual chosen quiet-hours
window or budget limit is not yet persisted or loaded anywhere in this build. This is a
partial pass: the mechanism is correct, but there is no operator-facing way to
configure it away from the maximally-permissive default yet.

Batching is not separately evidenced as a distinct implemented mechanism beyond
correlation-window merging (`tests/test_content_correlation.py`'s correlation-window
tests); the design doc's "short correlation/batching window" for social content maps to
the same correlation-window mechanism audited above, not a separate batching queue.

### "Connector failures are visible, isolated, and retryable where safe."

**Evidenced.** `tests/test_content_scheduler.py::test_one_connector_failure_does_not_cancel_other_guild_or_platform_jobs`
and `test_one_guilds_account_listing_failure_does_not_prevent_other_guilds_jobs` prove
isolation. `test_repeated_failures_back_off_and_never_exceed_the_connector_retry_hint`
and `test_real_x_connector_429_header_is_honored_end_to_end` prove real backoff/retry
behavior including honoring a real connector-reported `retry_after_seconds`.
`ConnectorFailure`/`ConnectorHealth` states are visible through `/fetch creator show`
and `/fetch creator verify` (`src/krubit/discord/content_commands.py`) and never leak
raw upstream error text (`tests/test_creator_health.py::test_integration_status_exposes_state_not_token_or_raw_api_body`).
One carried-forward gap: `ConnectorHealth.detail` itself has no `safe_detail`-style
redaction guarantee the way `ConnectorFailure.safe_detail` does — `HealthService.creator_health`'s
own docstring states it never reads `.detail` for exactly this reason, so the gap is
contained rather than exploited by first-party code, but a future caller reading
`.detail` directly would need to redact it itself.

### "Controlled `#live-notifications` and `#social-notifications` canaries pass for the capabilities whose credentials and platform approvals are available."

**Requires operator verification — not evidenced here.** No live Discord guild or bot
token was available in this session. The `#live-notifications` path has unit/
integration-level canary-equivalent coverage (`tests/test_live_signal_runtime.py`,
`tests/test_content_recovery.py`) against a fake Discord layer, and the Phase 2A
production canary status is separately tracked in the
[Phase 1 closeout](phase-1-closeout.md)/Phase 2A devlog as "has not run" per the
roadmap doc's existing Phase 2 status note. No `#social-notifications` production
canary has been run against any real platform in this or any prior session covered by
this repository's history. This gate is **not claimed as passing**; it is deferred to
an operator with real credentials and a live guild, per Task 14's Step 6 procedure
documented in the [operator runbook](phase-2-creator-signal-hub.md#shadow-preview-and-canary-controls).

### "Cross-guild reads/mutations and unauthorized creator management are denied."

**Evidenced.** `tests/test_creator_registry_service.py`:
`test_creator_role_can_add_self_but_not_another_member`,
`test_self_service_without_creator_role_is_denied`,
`test_non_owner_cannot_pause_another_members_account`,
`test_transfer_requires_administrator_authority`,
`test_every_authority_decision_produces_a_redacted_receipt`.
`tests/test_creator_registry_storage.py::test_creator_accounts_are_guild_scoped_and_stable_id_unique`
and `test_list_creator_accounts_for_owner_is_guild_scoped` cover storage-level guild
scoping. `tests/test_content_correlation.py::test_correlate_rejects_candidates_from_different_guilds`
extends the same boundary to correlation. One carried-forward gap: there is no
dedicated service-level test for cross-guild denial specifically on
pause/resume/transfer_account (Task 2 limitation) — the guild-scoped storage layer
denies it structurally (a lookup by `guild_id` + `account_id` simply returns nothing
for another guild's account), but no test names that specific cross-guild
pause/resume/transfer path directly. Also carried forward: `/fetch notification
preview` performs no authority check at all (Task 12 limitation, documented in the
[operator runbook](phase-2-creator-signal-hub.md#cross-guild-and-authority-boundaries)) —
any guild member, not just the account owner or an admin, can preview any account's
card. This is a real, currently-open authorization gap and should be tightened before
general availability.

### "Fanbase and unavailable live APIs remain clearly pending and are not counted as operational canaries."

**Evidenced.** `CATALOG[Platform.FANBASE]` declares both `SOCIAL` and `LIVE` as
`CapabilityState.UNSUPPORTED` (`src/krubit/integrations/catalog.py`), and
`tests/test_content_scheduler.py::test_fanbase_is_never_scheduled_even_if_enrolled`
proves Fanbase cannot be polled even if an operator enrolls an account. TikTok's LIVE
capability is `APPROVAL_REQUIRED` (`tests/test_connector_catalog.py::test_connector_descriptor_capability_lookup_and_known_baseline_states`
asserts this exactly), never `READY`. No test or code path anywhere in this build
reports Fanbase or TikTok LIVE as `ready`/operational —
`tests/test_phase_2_rollout.py::test_no_catalog_capability_is_silently_reported_ready_by_default`
enforces this as a standing invariant against the full catalog, not just these two
platforms.

## Deferred to operator verification (not evidenced in this session)

The following require a live Discord guild, a real Discord bot token, and/or real
per-platform credentials that this development sandbox does not have. They are
explicitly **not** claimed as complete by this audit:

1. `scripts\invoke-krubit.ps1 doctor` — does not exist as a subcommand in this build
   (confirmed above); there is no credential-independent doctor-style health command to
   run today. The closest existing credential-independent checks are the automated test
   suite, `krubit status <guild_id>` (requires a running database and guild), and
   `krubit install-url`.
2. Live `/fetch integrations`, `/fetch creator add <url>`, `/fetch notifications
   preview`, `/fetch live`, `/fetch latest`, `/fetch schedule` run against a real
   Discord guild with delivery flags false, confirming ephemeral responses and zero
   public side effects.
3. Per-connector production canaries (Task 14 Step 6): baseline, one controlled
   publish/schedule, one normalized event, zero public deliveries in shadow, then one
   authorized production delivery — for every connector whose credentials become
   available (Twitch is Phase 2A-canary-pending per the roadmap; YouTube, X, Bluesky
   are schedulable but unattempted with real credentials in this session; Instagram/
   Facebook/Threads/TikTok cannot be canaried at all until the scheduling and callback-
   server gaps documented in the operator runbook are closed).
4. An actual bot-process restart against a live guild to observe role/announcement/
   Scheduled Event recovery in production, as opposed to the unit/integration-level
   recovery tests cited above.
5. A live YouTube push notification, Meta signed webhook, or Meta/TikTok OAuth
   authorization-code redirect reaching Krubit — impossible in any environment right
   now, live or sandboxed, because `CallbackServer` is never started by `krubit run`
   (see the operator runbook's callback-server section). This is a build gap, not
   solely a credentials gap, but it is listed here because closing it requires an
   operator/future-task action rather than anything achievable by re-running tests.

## Summary

- 575/575 automated tests pass; Ruff and Pyright report zero findings; `git diff
  --check` is clean.
- Every completion-gate line has direct evidence for the parts of the system that are
  implemented and wired into the running process (Twitch, YouTube/X/Bluesky polling,
  registry, ledger, correlation, policy, delivery, analytics, commands, authorization
  boundaries). Discord Scheduled Event synchronization is implemented and tested but,
  as of this audit, was **not yet wired into the running process** — see the
  [final-review addendum](#addendum-final-whole-branch-review-fix-wave) below, which
  corrects this line.
- Two build gaps materially limit what "complete" means in production today and are
  documented prominently in the [operator runbook](phase-2-creator-signal-hub.md):
  Meta/TikTok connectors are not scheduled, and the OAuth/push callback server is never
  started. Both are deliberate, reviewed safety choices (not wiring a shared credential
  across accounts) rather than oversights, but they mean Instagram, Facebook, Threads,
  and TikTok are not operational in this build regardless of credentials configured.
  **A third gap — Scheduled Event sync having no production call site — was found by
  the whole-branch final review after this audit was written; see the addendum.**
- No production canary — live or social — has been run in this session. Every claim
  requiring a live Discord guild or real platform credentials is explicitly deferred to
  an operator, per this audit's mandate not to fabricate results for steps this
  environment cannot perform.

## Addendum: final whole-branch review fix wave

This audit (dated 2026-08-05, above) was written from a task-scoped review of Tasks
1-14 in isolation. A subsequent **whole-branch** review — looking at every task's
change composed together, which no single task-scoped review could see — found four
additional issues, now fixed on top of the state this audit describes:

1. **`KRUBIT_CREATOR_SIGNALS_ENABLED`/`KRUBIT_SOCIAL_DELIVERY_ENABLED` were parsed and
   validated but never actually read anywhere in `src/`.** With both at their `false`
   default, the connector scheduler still started (`BlueskyConnector` needs no
   credential) and `ContentRuntime` still delivered publicly. Both flags are now
   enforced: `KrubitBot` never builds connectors or starts the scheduler unless
   `creator_signals_enabled` is `true`, and `ContentRuntime.apply_plan` — the one
   choke point every send/edit path runs through, including `/fetch notifications
   retry`, which now shares the exact same `ContentRuntime` instance — is a no-op
   unless `social_delivery_enabled` is `true`.
2. **The idempotent Twitch-to-content-ledger migration
   (`migrate_all_twitch_content`, called on every `krubit run` boot) could raise and
   crash the entire boot sequence** if the deterministic Twitch account identity had
   been re-registered to a different owner (e.g. via a legitimate
   `/fetch creator transfer`). It also **silently re-paused an already-resumed Twitch
   account on every boot**, because its upsert overwrote `paused`. Both are fixed:
   `SQLiteStore.save_migrated_creator_account` never raises on an owner conflict (it
   returns `None` and the caller logs and skips that one session) and never overwrites
   an existing row's `paused` state.
3. **Discord Scheduled Event synchronization has no production call site** — a third
   build gap this audit's Summary line above did not count. Corrected in the [operator
   runbook](phase-2-creator-signal-hub.md#scheduled-event-synchronization-has-no-production-call-site),
   [README.md](../../README.md), and the
   [roadmap](../roadmaps/2026-08-03-krubit-phase-rollout.md).
4. **`notification_preview` had no authority check**, and `/fetch creator transfer`
   was documented as available but never registered as a Discord command. Both are
   fixed: `notification_preview` now applies the same `_require_authority` gate every
   other per-account command uses, and `/fetch creator transfer` is now a registered
   command following the same confirm/authority pattern as `/fetch creator route`.

Every `/fetch notification ...` reference in this audit, the operator runbook, and
README.md was also corrected to the actual registered command group name,
`/fetch notifications ...`.
