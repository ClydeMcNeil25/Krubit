# Krubit Development Log: Phase 3 Watchdog

**Date:** August 5, 2026
**Status:** Automated implementation and verification through Task 9 complete. No live
Discord guild or credentialed environment was available in this development session;
see [Known limitations](../operations/phase-3-watchdog.md#known-limitations-that-change-what-enabling-watchdog-actually-does)
in the operations guide for exactly what is and is not evidenced.

## Scope

This effort delivers Phase 3 (Watchdog: Entry Sniffing, bounded post-join watch
windows, raid/spam-wave/webhook-abuse/permission-risk detection, and incident
evidence) per the
[Phase 3 Watchdog design](../superpowers/specs/2026-08-05-phase-3-watchdog-design.md).
Krubit gains deterministic, explainable risk detection with **zero autonomous
moderation authority** — every risk assessment records, preserves evidence, increases
monitoring temporarily, notifies staff, and recommends a reversible action; nothing
in this phase kicks, bans, times out, deletes a message, or mutates a role.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | Domain model: `RiskBand`, `RiskSignal`, `Incident`, `EvidencePacket`, deterministic `evaluate_risk_band` |
| 2 | Storage: `entry_sniff_assessments`, `watch_windows`, `incidents`, `guild_allow_block_lists`, `sniff_receipts` |
| 3 | Entry Sniff join assessment (`EntrySniffService`) |
| 4 | Post-join watch window (`WatchWindowService`) and message-signal extraction |
| 5 | Raid/spam-wave/webhook-abuse/permission-risk detectors |
| 6 | Redacted evidence packets and AutoMod event correlation |
| 7 | Live runtime wiring: `WatchdogRuntime`, gateway event hookup, sweep cycle |
| 8 | Staff-only `/fetch sniff`-family commands, with honest evidence reconstruction |
| 9 | Health integration, structural no-moderation-authority proof, documentation (this task) |

## Task 9: this task's changes

- `src/krubit/services/health.py`: added `WatchdogHealthFacts` (a frozen dataclass of
  `enabled`/`notifications_enabled`/`message_content_available`) and `watchdog=`
  keyword parameters on `HealthService.server_health`/`integration_health`. Passing
  `None` (the default, and what every pre-Phase-3 caller still does) reports nothing
  about Watchdog, preserving every existing test and call site unchanged; passing an
  explicit `WatchdogHealthFacts` surfaces `watchdog_disabled`,
  `watchdog_notifications_disabled`, and/or `watchdog_message_content_unavailable`
  findings.
- `src/krubit/discord/bot.py`: wired real `Settings`/`self.intents.message_content`
  values into `WatchdogHealthFacts` at `FetchCommands`' production construction site
  and the daily-summary health-report call site, so `/fetch server-health`,
  `/fetch integrations`, and the once-daily staff-channel health summary all now
  report genuine Watchdog capability facts rather than the facts existing only in
  tests.
- `tests/test_watchdog_structural_safety.py`: the structural no-moderation-authority
  proof required by the Completion Gate. Scans the union of two sets — the filename
  glob `src/krubit/**/watchdog*.py` **plus** an explicitly maintained
  `_EXPLICIT_WATCHDOG_MODULES` list covering every Task 3-6 Watchdog service module
  whose filename does not start with `watchdog` (`services/entry_sniff.py`,
  `services/watch_window.py`, `services/raid_detection.py`,
  `services/webhook_and_permission_risk.py`, `services/incident_evidence.py`) — for
  every module's source text, for a call to a moderation-mutation client method.
  `test_explicit_watchdog_modules_still_exist` guards that list against going stale on
  a future rename. As widened in the final whole-branch review (post-Task-9), the
  forbidden-call set itself covers `kick`/`ban`/`unban`/`timeout`/`delete_messages`/
  `remove_roles`/`add_roles`/`edit`/`delete`/`purge`/`set_permissions`. **Passed** —
  Tasks 1-9 and the final review's own fixes introduced no such call in any of the ten
  scanned modules.
- `tests/test_health_service.py`: six new tests covering the `None`-means-no-op
  default, the fully-disabled case, the enabled-but-degraded case, and the
  fully-healthy case for both `server_health` and `integration_health`.
- `docs/operations/phase-3-watchdog.md` (new): the full operator runbook — exact env
  var names, Message Content Developer Portal steps, shadow-mode explanation, and five
  named, code-verified known limitations (most importantly: a lone `INCIDENT`-band
  join is never notified in real time — see the runbook's Gap 1).
- `README.md`, `.env.example`, `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`:
  updated to reflect Phase 3 capabilities and the same known limitations, consistently
  with the runbook.

## Known limitations carried into this build (see the operations guide for full detail)

1. **A lone `INCIDENT`-band join is never notified in real time** — `on_member_join`
   never constructs an `Incident`; only the sweep-cycle detectors do.
   `IncidentKind.MEMBER` exists in the domain enum but is constructed nowhere. This is
   the single biggest functional gap in the phase.
2. `SpamWaveDetector`/`WebhookAbuseDetector` use in-memory, per-process caches with no
   persistence across restarts.
3. `join_velocity`/`join_cluster_similarity` depend on the live gateway member cache,
   not durable storage — a weak-detection window exists right after a bot restart.
4. No storage table persists a full `EvidencePacket`; `/fetch incident`/
   `/fetch evidence` honestly reconstruct signal names from a receipt rather than
   showing genuine stored per-signal detail.
5. `KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL` is a forward-declared setting with no consumer
   — "notify Zariya" is not implemented in this phase, only "notify staff."

Also flagged (not a build gap, a design nuance worth a human review): the
`mass_mentions` HIGH-tier/`@everyone` message signal can reach `SUSPICIOUS` band alone,
unlike every other message signal, and does not account for the sender's own
`mention_everyone` Discord permission.

## Automated verification run in this session (Task 9, before the final whole-branch review)

```text
.venv\Scripts\python.exe -m pytest -q                             -> 772 passed, 0 failed
.venv\Scripts\ruff.exe check .                                    -> All checks passed!
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe  -> 0 errors, 0 warnings
git diff --check                                                  -> exit 0, no output
```

No live Discord bot token, no live Discord guild, and no credentialed environment were
available in this session. Live canary verification (a real join, a real raid
simulation, a real notification delivery) is deferred to a credentialed operator, per
the same discipline the [Phase 2 completion audit](../operations/phase-2-completion-audit.md)
established.

## Final whole-branch review fix wave (post-Task-9)

A final review composed all nine tasks together and found seven problems only visible
at that level (three Critical, four Important). All seven were fixed in one pass:

1. **AutoMod correlation could never fire in production** — `phase_three_intents()`
   (`src/krubit/discord/install.py`) never requested `auto_moderation_configuration`/
   `auto_moderation_execution`, so Discord never dispatched the underlying gateway
   events regardless of `on_automod_action`'s own logic. Fixed by adding both
   (non-privileged) intents.
2. **The structural no-moderation-authority proof under-covered its own scope** —
   `tests/test_watchdog_structural_safety.py`'s forbidden-call set widened from five
   names to eleven (`add_roles`/`edit`/`delete`/`purge`/`set_permissions`/`unban`
   added), closing gaps around role-mutation, `member.edit(timed_out_until=...)`-style
   timeout, channel mutation, and single-message deletion. Passed at zero cost, as
   predicted by grepping the ten scanned modules first.
3. **`on_automod_action`'s Watchdog block bypassed `watchdog_enabled`** — the
   `list_open_watch_windows` read and `automod_action_correlated` sniff-receipt write
   in `KrubitBot.on_automod_action` (`src/krubit/discord/bot.py`) ran unconditionally,
   contradicting the ops doc's "disabling the flag means no Watchdog activity" claim.
   Fixed by wrapping that block in `if self._settings.watchdog_enabled:`, applied
   before/together with fix #1 so the widened intent never activated an unguarded path
   even transiently.
4. **No dedupe/cooldown — a single ongoing incident could notify staff every sweep**
   — each of the four detectors' `evaluate` is a pure trailing-window re-scan with no
   memory of a previous fire, so a raid still landing joins (or a spam wave, webhook-
   abuse burst, or permission-risk grant still in its lookback) could mint a fresh
   incident and staff notification on every 60-second sweep for the duration of its
   window. Fixed with `_has_open_incident_of_kind` (added to `raid_detection.py` and
   `webhook_and_permission_risk.py`), which skips firing a new incident if a same-kind
   incident already exists within the detector's own correlation window — reusing
   `list_recent_incidents`, no new storage.
5. This devlog's Task 9 section was stale, describing the pre-fix five-module glob-
   only structural test and the pre-fix `772 passed` count. Corrected above and in
   this section.
6. `docs/operations/phase-3-watchdog.md` misstated the sweep cadence as "every 5
   minutes"; corrected to the actual 60 seconds (`@tasks.loop(seconds=60)`).
7. Spam-wave message-content ingestion from all guild members (not just watched ones)
   is design-doc-sanctioned but was not disclosed to operators. Added a prominent
   "Gap 6" to the ops doc's Known limitations, documenting the trade-off — no behavior
   change, per the review's explicit instruction that this is a rollout-gate decision
   for a human, not a code change.

## Automated verification run after the final review fix wave

```text
.venv\Scripts\python.exe -m pytest -q                             -> 780 passed, 0 failed
.venv\Scripts\ruff.exe check .                                    -> All checks passed!
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe  -> 0 errors, 0 warnings, 0 informations
```
