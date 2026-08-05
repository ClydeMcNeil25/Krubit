# Krubit Phase 3 Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Krubit deterministic, explainable Entry Sniffing, a bounded post-join
watch window, raid/spam-wave/webhook-abuse/permission-risk detection, and incident
evidence packets — detection and evidence only, with zero autonomous moderation
authority, matching the Phase 3 design spec.

**Architecture:** Pure risk-band evaluation over deterministic signals feeds a durable
assessment; `watch`-or-higher bands open a bounded, auto-expiring watch window that
inspects only the watched member's own public messages; any band-changing event or
correlated raid/spam-wave/webhook-abuse/permission-risk signal produces a redacted,
receipted evidence packet and a staff/Zariya notification recommending (never
executing) a reversible action. Staff-only `/fetch sniff`-family commands are the only
read surface.

**Tech Stack:** Python 3.13, discord.py 2.7.1, aiosqlite 0.22.1, pytest 9,
pytest-asyncio 1.4.0, Ruff, Pyright strict — same stack as Phase 2, no new
dependencies expected.

## Global Constraints

- No warning, deletion, timeout, kick, ban, role mutation, channel mutation, or
  public accusation is ever automatic. Structurally: no Phase 3 module may import or
  call a Discord moderation-mutation client method (kick/ban/timeout/message-delete/
  role-remove) at all.
- Every risk-band determination is a pure function of stored, named signals — no
  hidden state, no opaque score. Given the same signal inputs, the same band and
  explanation must always result.
- Krubit reads message content only for a member with an actively open watch window,
  never DMs, never outside the window, and never retains raw content beyond what a
  specific triggering signal requires (redacted before storage).
- Every new table is guild-scoped with `guild_id` as the leading key column, matching
  the existing `sqlite.py` convention (see `creator_accounts`, `guild_events`).
- Every assessment, watch-window transition, and incident write produces a durable,
  redacted receipt, matching the existing `CreatorRegistryReceipt`/`content_receipts`
  pattern.
- Detailed risk/incident/watchlist views are staff-only; no member sees their own or
  another member's band via any command.
- Degrade honestly: if the privileged Message Content intent is not enabled, message-
  content-dependent signals report as unavailable rather than silently skipping or
  failing bot startup — join-signal-only detection continues to function.
- Shadow mode: detection, assessment, and evidence recording run from the first
  commit; staff/Zariya notification delivery is gated behind an explicit, off-by-
  default settings flag, mirroring Phase 2's `social_delivery_enabled` pattern (and
  do NOT repeat Phase 2's mistake — the flag must be genuinely wired into the
  notification-send code path from the task that introduces it, with a test proving
  zero notifications are sent while it is false).

## File Structure

### New production modules

- `src/krubit/domain/watchdog.py`: `RiskBand`, `RiskSignal`, `EntrySniffAssessment`,
  `WatchWindow`, `WatchWindowCloseReason`, `IncidentKind`, `Incident`,
  `EvidencePacket`, `AllowBlockEntry` value objects.
- `src/krubit/services/entry_sniff.py`: deterministic signal extraction and
  `evaluate_risk_band(signals) -> (RiskBand, explanation)` pure function; join-time
  orchestration (`EntrySniffService.assess_join(member, now)`).
- `src/krubit/services/watch_window.py`: `WatchWindowService` — opens/closes windows,
  auto-expiry sweep, message-signal inspection
  (`inspect_message(message, now) -> RiskSignal | None`).
- `src/krubit/services/raid_detection.py`: `RaidDetector`/`SpamWaveDetector` —
  correlate recent assessments/messages across members within a guild.
- `src/krubit/services/webhook_and_permission_risk.py`: correlates Phase 1's existing
  webhook/permission change tracking (`storage/sqlite.py`'s existing guild-events
  tables) into webhook-abuse and permission-risk incidents.
- `src/krubit/services/incident_evidence.py`: builds redacted `EvidencePacket`s from
  raw signals/messages, using `security/redaction.py`'s existing `redact()`.
- `src/krubit/discord/watchdog_events.py`: `on_member_join`/`on_message` extraction
  helpers (mirrors `discord/live_signals.py`'s pattern of pure Discord-object-to-
  domain-object extraction functions).
- `src/krubit/discord/watchdog_runtime.py`: wires join/message/AutoMod events into the
  services above, applies watch-window sweeps on a scheduled loop (mirrors
  `discord/content_runtime.py`'s `ConnectorScheduler` loop-isolation pattern), and
  sends staff/Zariya notifications (recommend-only) gated behind the settings flag.
- `src/krubit/discord/watchdog_commands.py`: `/fetch sniff`, `sniff-report`,
  `incident`, `evidence`, `watchlist`.

### Existing modules to modify

- `src/krubit/config.py`: add `KRUBIT_WATCHDOG_ENABLED`,
  `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED`, watch-window duration, and Zariya-bridge
  notification target settings — all optional, safe defaults.
- `src/krubit/discord/install.py`: add `phase_three_intents()` requesting
  `message_content` additively; add `phase_three_permissions()` if any new permission
  scope is required (likely none beyond existing read access).
- `src/krubit/storage/sqlite.py`: additive Phase 3 schema and guild-scoped persistence
  methods; move Phase 3 row-decoding helpers into `src/krubit/storage/watchdog_rows.py`
  to limit further growth of `sqlite.py` (already 2903 lines), matching the precedent
  set by `storage/creator_rows.py` in Phase 2.
- `src/krubit/discord/bot.py`: register the new event listeners and commands,
  following the existing direct-listener (no cog) pattern.
- `src/krubit/services/health.py`: surface watchdog capability state (message-content
  intent available/unavailable, watchdog enabled/disabled, notification delivery
  enabled/disabled).
- `README.md`, `.env.example`, `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`:
  operator instructions and roadmap status.

---

### Task 1: Watchdog domain model and pure risk-band evaluation

**Files:**
- Create: `src/krubit/domain/watchdog.py`
- Test: `tests/test_watchdog_domain.py`
- Test: `tests/test_risk_band_evaluation.py`

**Interfaces:**
- Produces: `RiskBand` (StrEnum: CLEAR, WATCH, SUSPICIOUS, INCIDENT), `RiskSignal`
  (frozen dataclass: name, weight, detail, confidence), `EntrySniffAssessment`,
  `WatchWindow`, `WatchWindowCloseReason` (StrEnum: EXPIRED, ESCALATED,
  STAFF_OVERRIDE), `IncidentKind` (StrEnum: MEMBER, RAID, SPAM_WAVE, WEBHOOK_ABUSE,
  PERMISSION_RISK), `Incident`, `EvidencePacket`, `AllowBlockEntry`, and
  `evaluate_risk_band(signals: tuple[RiskSignal, ...]) -> tuple[RiskBand, str]`.
- Consumes: no Phase 3 interfaces yet.

- [ ] **Step 1: Write failing domain and evaluation tests**

```python
def test_evaluate_risk_band_is_deterministic_for_identical_signals() -> None:
    signals = (RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),)
    first = evaluate_risk_band(signals)
    second = evaluate_risk_band(signals)
    assert first == second


def test_evaluate_risk_band_explanation_names_every_contributing_signal() -> None:
    signals = (
        RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),
        RiskSignal(name="join_velocity", weight=4, detail="12 joins in 60s", confidence=0.8),
    )
    band, explanation = evaluate_risk_band(signals)
    assert band is RiskBand.SUSPICIOUS
    assert "account_age" in explanation
    assert "join_velocity" in explanation


def test_evaluate_risk_band_with_no_signals_is_clear() -> None:
    assert evaluate_risk_band(()) == (RiskBand.CLEAR, "no signals observed")
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_domain.py tests\test_risk_band_evaluation.py -q`

Expected: FAIL during import because `src/krubit/domain/watchdog.py` does not exist.

- [ ] **Step 3: Implement frozen enums/value objects and deterministic band evaluation**

Follow the `@dataclass(frozen=True, slots=True)` + `__post_init__` validation
convention from `src/krubit/domain/models.py` and `creator_signals.py`. Band
thresholds are fixed named constants (not configuration) so evaluation stays pure and
reproducible; document each threshold's rationale in a docstring since this is
safety-sensitive logic a future maintainer must be able to audit.

- [ ] **Step 4: Run focused tests, Ruff, and Pyright**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_domain.py tests\test_risk_band_evaluation.py -q`

Run: `.venv\Scripts\ruff.exe check src\krubit\domain\watchdog.py tests\test_watchdog_domain.py tests\test_risk_band_evaluation.py`

Run: `.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the domain model**

```powershell
git add src/krubit/domain/watchdog.py tests/test_watchdog_domain.py tests/test_risk_band_evaluation.py
git commit -m "feat: add watchdog domain model and risk-band evaluation"
```

### Task 2: Watchdog storage schema and guild-scoped persistence

**Files:**
- Create: `src/krubit/storage/watchdog_rows.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_watchdog_storage.py`

**Interfaces:**
- Consumes: value objects from Task 1.
- Produces: `SQLiteStore.save_entry_sniff_assessment`, `.get_entry_sniff_assessment`,
  `.open_watch_window`, `.close_watch_window`, `.list_open_watch_windows`,
  `.record_incident`, `.get_incident`, `.list_recent_incidents`,
  `.save_allow_block_entry`, `.list_allow_block_entries`, `.record_sniff_receipt`,
  `.list_sniff_receipts`.

- [ ] **Step 1: Write failing storage tests for isolation and idempotency**

```python
@pytest.mark.asyncio
async def test_watch_windows_are_guild_scoped(store: SQLiteStore) -> None:
    await store.open_watch_window(watch_window(guild_id=111, member_id=222))
    await store.open_watch_window(watch_window(guild_id=999, member_id=222))
    assert len(await store.list_open_watch_windows(111)) == 1
    assert len(await store.list_open_watch_windows(999)) == 1


@pytest.mark.asyncio
async def test_closing_a_watch_window_twice_is_idempotent(store: SQLiteStore) -> None:
    window = watch_window(guild_id=111, member_id=222)
    await store.open_watch_window(window)
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=NOW)
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=LATER)
    closed = (await store.list_open_watch_windows(111))
    assert closed == ()
```

- [ ] **Step 2: Run focused tests and confirm missing persistence methods**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_storage.py -q`

Expected: FAIL because the watchdog tables and methods are absent.

- [ ] **Step 3: Add additive schema and guild-scoped methods**

Add tables `entry_sniff_assessments`, `watch_windows`, `incidents`,
`guild_allow_block_lists`, `sniff_receipts` per the design doc's Data Model section.
Every table's primary/unique key leads with `guild_id`, matching the existing
convention. Move row-decoding helpers into `storage/watchdog_rows.py`, matching the
`storage/creator_rows.py` precedent — add only persistence *methods* to `sqlite.py`.

- [ ] **Step 4: Run storage and existing regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_storage.py tests\test_storage.py -q`

Expected: PASS with no changes to existing Phase 0-2 records.

- [ ] **Step 5: Commit storage**

```powershell
git add src/krubit/storage/watchdog_rows.py src/krubit/storage/sqlite.py tests/test_watchdog_storage.py
git commit -m "feat: persist watchdog assessments, watch windows, and incidents"
```

### Task 3: Entry Sniff signal extraction and join-time orchestration

**Files:**
- Create: `src/krubit/discord/watchdog_events.py` (join-signal extraction only in this
  task; message extraction is Task 4)
- Create: `src/krubit/services/entry_sniff.py`
- Modify: `src/krubit/storage/sqlite.py` (if a helper query is needed for join-cluster
  similarity — recent joins in a bounded window)
- Test: `tests/test_entry_sniff_extraction.py`
- Test: `tests/test_entry_sniff_service.py`

**Interfaces:**
- Consumes: Task 1 domain types, Task 2 storage methods.
- Produces: `extract_join_signals(member, recent_joins, now) -> tuple[RiskSignal,
  ...]`, `EntrySniffService.assess_join(member, now) -> EntrySniffAssessment`.

- [ ] **Step 1: Write failing extraction and orchestration tests**

```python
def test_extract_join_signals_flags_new_account_and_default_avatar() -> None:
    signals = extract_join_signals(member(created_hours_ago=1, has_avatar=False), recent_joins=(), now=NOW)
    assert any(s.name == "account_age" for s in signals)
    assert any(s.name == "default_avatar" for s in signals)


def test_extract_join_signals_flags_join_cluster_similarity() -> None:
    cluster = tuple(member(created_hours_ago=1) for _ in range(8))
    signals = extract_join_signals(member(created_hours_ago=1), recent_joins=cluster, now=NOW)
    assert any(s.name == "join_cluster_similarity" for s in signals)


@pytest.mark.asyncio
async def test_assess_join_persists_exactly_one_assessment_per_join(store: SQLiteStore) -> None:
    service = EntrySniffService(store)
    assessment = await service.assess_join(member(), now=NOW)
    assert (await store.get_entry_sniff_assessment(assessment.guild_id, assessment.member_id, NOW)) == assessment
```

- [ ] **Step 2: Run focused tests and confirm missing modules**

Run: `.venv\Scripts\python.exe -m pytest tests\test_entry_sniff_extraction.py tests\test_entry_sniff_service.py -q`

Expected: FAIL during import.

- [ ] **Step 3: Implement deterministic extraction and orchestration**

Extraction reads only Discord fields already available on `discord.Member`/its
`created_at`/`avatar`/`pending`/`flags` — no network calls, no inference beyond named,
bounded signals. `assess_join` calls `extract_join_signals`, `evaluate_risk_band`
(Task 1), persists the assessment and a receipt, and returns it. It does not yet open
a watch window — that is `WatchWindowService`'s job in Task 4, kept separate so each
service has one responsibility per the plan's file-structure discipline.

- [ ] **Step 4: Run extraction, service, and full regression suite**

Run: `.venv\Scripts\python.exe -m pytest tests\test_entry_sniff_extraction.py tests\test_entry_sniff_service.py tests\test_watchdog_storage.py -q`

Expected: PASS, including a test that two joins by the same member (leave then
rejoin) produce two independent assessments, not one updated in place.

- [ ] **Step 5: Commit Entry Sniff**

```powershell
git add src/krubit/discord/watchdog_events.py src/krubit/services/entry_sniff.py src/krubit/storage/sqlite.py tests/test_entry_sniff_extraction.py tests/test_entry_sniff_service.py
git commit -m "feat: add entry sniff join-time assessment"
```

### Task 4: Watch window service and message-signal inspection

**Files:**
- Modify: `src/krubit/discord/watchdog_events.py` (add message-signal extraction)
- Create: `src/krubit/services/watch_window.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_watch_window_service.py`
- Test: `tests/test_message_signal_extraction.py`

**Interfaces:**
- Consumes: Task 1/2/3 interfaces.
- Produces: `extract_message_signals(message, now) -> tuple[RiskSignal, ...]`,
  `WatchWindowService.open_if_warranted(assessment, now)`,
  `.inspect_message(message, now) -> RiskSignal | None`, `.sweep_expired(guild_id,
  now) -> tuple[WatchWindow, ...]`.

- [ ] **Step 1: Write failing tests for window lifecycle and message signals**

```python
@pytest.mark.asyncio
async def test_clear_band_never_opens_a_watch_window(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.CLEAR), now=NOW)
    assert await store.list_open_watch_windows(111) == ()


@pytest.mark.asyncio
async def test_expired_watch_window_is_swept_and_closed(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)
    closed = await service.sweep_expired(111, now=NOW + WATCH_WINDOW_DURATION + ONE_SECOND)
    assert closed[0].close_reason is WatchWindowCloseReason.EXPIRED
    assert await store.list_open_watch_windows(111) == ()


def test_extract_message_signals_flags_mass_mentions_and_repeated_content() -> None:
    signals = extract_message_signals(message(mention_count=25, content="buy now buy now buy now"), now=NOW)
    assert any(s.name == "mass_mentions" for s in signals)
    assert any(s.name == "repeated_content" for s in signals)
```

- [ ] **Step 2: Run focused tests and confirm missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watch_window_service.py tests\test_message_signal_extraction.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement bounded, auto-expiring watch windows and message inspection**

`inspect_message` must only be called for a member with a currently-open watch
window (the caller in Task 6's runtime enforces this — but this service must also
defend against being called for a member with no open window, returning `None`
rather than raising, since a race between window expiry and an in-flight message is
expected). Link/domain-shape detection uses structural URL parsing (matching
`normalize_twitch_channel`-style parsing already used in `domain/live_signals.py`),
not a fetched blocklist. Repeated-content detection uses a bounded similarity check
against the member's own last few messages within the window, never a cross-member
comparison (that's `RaidDetector`'s job in Task 5).

- [ ] **Step 4: Run watch-window, message-signal, and regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watch_window_service.py tests\test_message_signal_extraction.py tests\test_entry_sniff_service.py -q`

Expected: PASS, including a benign-surge test (many clean joins in a short window
must not, by themselves, push every member past `watch`).

- [ ] **Step 5: Commit watch window**

```powershell
git add src/krubit/discord/watchdog_events.py src/krubit/services/watch_window.py src/krubit/storage/sqlite.py tests/test_watch_window_service.py tests/test_message_signal_extraction.py
git commit -m "feat: add bounded watch window and message-signal inspection"
```

### Task 5: Raid, spam-wave, webhook-abuse, and permission-risk detection

**Files:**
- Create: `src/krubit/services/raid_detection.py`
- Create: `src/krubit/services/webhook_and_permission_risk.py`
- Modify: `src/krubit/storage/sqlite.py`
- Test: `tests/test_raid_detection.py`
- Test: `tests/test_webhook_and_permission_risk.py`

**Interfaces:**
- Consumes: Task 1-4 interfaces, plus Phase 1's existing guild-event/webhook/
  permission-change tracking already in `sqlite.py`.
- Produces: `RaidDetector.evaluate(guild_id, now) -> Incident | None`,
  `SpamWaveDetector.evaluate(guild_id, now) -> Incident | None`,
  `WebhookAbuseDetector.evaluate(guild_id, now) -> Incident | None`,
  `PermissionRiskDetector.evaluate(guild_id, now) -> Incident | None`.

- [ ] **Step 1: Write failing detector tests against synthetic fixtures**

```python
@pytest.mark.asyncio
async def test_raid_detector_fires_on_correlated_join_velocity_and_similarity(store: SQLiteStore) -> None:
    for _ in range(10):
        await seed_assessment(store, guild_id=111, band=RiskBand.WATCH, joined_within_seconds=30)
    incident = await RaidDetector(store).evaluate(111, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.RAID


@pytest.mark.asyncio
async def test_raid_detector_does_not_fire_on_organic_growth(store: SQLiteStore) -> None:
    for i in range(10):
        await seed_assessment(store, guild_id=111, band=RiskBand.CLEAR, joined_within_seconds=i * 600)
    assert await RaidDetector(store).evaluate(111, now=NOW) is None
```

- [ ] **Step 2: Run focused tests and confirm missing detectors**

Run: `.venv\Scripts\python.exe -m pytest tests\test_raid_detection.py tests\test_webhook_and_permission_risk.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement correlation detectors over existing + new tables**

Webhook-abuse and permission-risk detectors read Phase 1's existing webhook/
permission-change guild-event tables (already in `sqlite.py` from Phase 1) — do not
duplicate that tracking, only correlate it against Task 2's `watch_windows`/
`entry_sniff_assessments`. Every detector's positive result calls
`incident_evidence`'s packet builder (Task 6) — for this task, produce the `Incident`
record and leave packet assembly abstracted behind an injected builder so this task's
tests don't need Task 6's redaction wiring yet.

- [ ] **Step 4: Run detector and full-suite regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_raid_detection.py tests\test_webhook_and_permission_risk.py tests\test_discord_events.py -q`

Expected: PASS, including the organic-growth false-positive guard.

- [ ] **Step 5: Commit detectors**

```powershell
git add src/krubit/services/raid_detection.py src/krubit/services/webhook_and_permission_risk.py src/krubit/storage/sqlite.py tests/test_raid_detection.py tests/test_webhook_and_permission_risk.py
git commit -m "feat: add raid, spam-wave, webhook-abuse, and permission-risk detection"
```

### Task 6: Evidence packet assembly and AutoMod correlation

**Files:**
- Create: `src/krubit/services/incident_evidence.py`
- Modify: `src/krubit/discord/bot.py` (correlate existing `on_automod_action` into
  evidence, not a new enforcement path)
- Test: `tests/test_incident_evidence.py`
- Test: `tests/test_automod_correlation.py`

**Interfaces:**
- Consumes: `redact()` from `security/redaction.py`, Task 1-5 interfaces, the
  existing `on_automod_action` handler in `bot.py:840`.
- Produces: `build_evidence_packet(incident, raw_signals, raw_messages) ->
  EvidencePacket`, `correlate_automod_action(action, now) -> RiskSignal | None`.

- [ ] **Step 1: Write failing evidence and correlation tests**

```python
def test_evidence_packet_redacts_raw_message_content_before_storage() -> None:
    packet = build_evidence_packet(
        incident(), raw_signals=(...,), raw_messages=(message(content="token=secret-abc"),)
    )
    assert "secret-abc" not in str(packet.to_storage_dict())


def test_automod_action_becomes_a_correlated_risk_signal_not_a_new_enforcement() -> None:
    signal = correlate_automod_action(automod_action(rule_trigger="spam"), now=NOW)
    assert signal is not None
    assert signal.name == "automod_correlated_spam"
```

- [ ] **Step 2: Run focused tests and confirm missing modules**

Run: `.venv\Scripts\python.exe -m pytest tests\test_incident_evidence.py tests\test_automod_correlation.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement redacted evidence assembly and AutoMod correlation**

`build_evidence_packet` calls `redact()` on every free-text field before it ever
reaches a dataclass meant for storage — verify by construction (the packet's storage
representation is only ever produced by a code path that redacts first), not by
convention. AutoMod correlation reads the *existing* `on_automod_action` payload
(already wired in `bot.py`) and turns it into a `RiskSignal` feeding the same
evaluation path — it must not add any new AutoMod rule creation or enforcement call.

- [ ] **Step 4: Run evidence, correlation, and regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_incident_evidence.py tests\test_automod_correlation.py tests\test_discord_events.py tests\test_redaction.py -q`

Expected: PASS.

- [ ] **Step 5: Commit evidence assembly**

```powershell
git add src/krubit/services/incident_evidence.py src/krubit/discord/bot.py tests/test_incident_evidence.py tests/test_automod_correlation.py
git commit -m "feat: build redacted incident evidence and correlate AutoMod events"
```

### Task 7: Privileged intent, settings, and watchdog runtime wiring

**Files:**
- Modify: `src/krubit/discord/install.py`
- Modify: `src/krubit/config.py`
- Create: `src/krubit/discord/watchdog_runtime.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_discord_install.py`
- Test: `tests/test_watchdog_runtime.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: Task 1-6 interfaces.
- Produces: `phase_three_intents()`, `Settings.watchdog_enabled`,
  `.watchdog_notifications_enabled`, `WatchdogRuntime.on_member_join(member, now)`,
  `.on_message(message, now)`, `.sweep_cycle(now)`, `.notify_staff(incident)`
  (no-op unless `watchdog_notifications_enabled`).

- [ ] **Step 1: Write failing intent, settings, and gating tests**

```python
def test_phase_three_intents_adds_message_content_to_phase_two() -> None:
    intents = phase_three_intents()
    assert intents.message_content is True
    assert intents.members is True  # inherited from phase_two_intents()


def test_watchdog_settings_default_disabled() -> None:
    settings = Settings.from_env(base_env())
    assert settings.watchdog_enabled is False
    assert settings.watchdog_notifications_enabled is False


@pytest.mark.asyncio
async def test_notify_staff_sends_nothing_when_notifications_disabled(runtime) -> None:
    await runtime.notify_staff(incident())
    assert runtime.channel.sent == ()
```

- [ ] **Step 2: Run focused tests and confirm missing settings/gating**

Run: `.venv\Scripts\python.exe -m pytest tests\test_discord_install.py tests\test_watchdog_runtime.py tests\test_config.py -q`

Expected: FAIL for the new settings/intent/gate.

- [ ] **Step 3: Wire the runtime with the flag genuinely enforced at the send boundary**

Learn from Phase 2's final-review finding: `watchdog_enabled`/
`watchdog_notifications_enabled` must be checked at the actual notification-send call
site inside `notify_staff`/`WatchdogRuntime`, not only at construction time in
`bot.py` — mirror `ContentRuntime.apply_plan`'s early-return-on-disabled-flag pattern
exactly, and add the equivalent zero-send test `ContentRuntime` now has. `on_message`
must check message intent availability and only call `WatchWindowService.
inspect_message` for a member with a currently open window (per Task 4's contract) —
never for every guild message.

- [ ] **Step 4: Run runtime, install, config, and full regression suite**

Run: `.venv\Scripts\python.exe -m pytest tests\test_discord_install.py tests\test_watchdog_runtime.py tests\test_config.py tests\test_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit runtime wiring**

```powershell
git add src/krubit/discord/install.py src/krubit/config.py src/krubit/discord/watchdog_runtime.py src/krubit/discord/bot.py tests/test_discord_install.py tests/test_watchdog_runtime.py tests/test_config.py
git commit -m "feat: wire watchdog runtime behind explicit settings flags"
```

### Task 8: Staff-only `/fetch sniff`-family commands

**Files:**
- Create: `src/krubit/discord/watchdog_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_watchdog_commands.py`

**Interfaces:**
- Consumes: Task 1-7 interfaces.
- Produces: `/fetch sniff`, `/fetch sniff-report`, `/fetch incident`,
  `/fetch evidence`, `/fetch watchlist` — all staff-only (Manage Guild or configured
  moderator role), all ephemeral.

- [ ] **Step 1: Write failing command authorization and content tests**

```python
@pytest.mark.asyncio
async def test_non_staff_member_is_denied_sniff_command(commands) -> None:
    result = await commands.sniff(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_evidence_command_never_renders_unredacted_content(commands) -> None:
    result = await commands.evidence(actor=staff_member(), incident_id=seeded_incident_with_secret())
    assert "secret" not in result.card.description
```

- [ ] **Step 2: Run focused tests and confirm missing command surface**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_commands.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement grouped `/fetch` commands with mandatory staff gating**

Every command checks authority before any query, matching `live_commands.py`'s
existing staff-only pattern. All output renders through the redacted `EvidencePacket`
representation (Task 6) — never raw stored signal/message data.

- [ ] **Step 4: Run command, bot, and existing `/fetch` regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_commands.py tests\test_live_signal_commands.py tests\test_phase_one_commands.py tests\test_content_commands.py -q`

Expected: PASS with no command-name collisions.

- [ ] **Step 5: Commit commands**

```powershell
git add src/krubit/discord/watchdog_commands.py src/krubit/discord/bot.py tests/test_watchdog_commands.py
git commit -m "feat: add staff-only watchdog commands"
```

### Task 9: Health integration, structural no-moderation-authority proof, and documentation

**Files:**
- Modify: `src/krubit/services/health.py`
- Create: `tests/test_watchdog_structural_safety.py`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`
- Create: `docs/operations/phase-3-watchdog.md`
- Create: `docs/devlogs/2026-08-05-phase-3-watchdog.md`
- Test: `tests/test_health_service.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: watchdog capability facts in `/fetch server-health`/`/fetch integrations`
  (message-content intent available/unavailable, watchdog enabled/disabled), and the
  structural proof the Completion Gate requires.

- [ ] **Step 1: Write the failing structural safety test first**

```python
def test_no_watchdog_module_imports_a_moderation_mutation_client_method() -> None:
    forbidden = {"kick", "ban", "timeout", "delete_messages", "remove_roles"}
    for module_path in glob("src/krubit/**/watchdog*.py", recursive=True):
        source = Path(module_path).read_text()
        for name in forbidden:
            assert f".{name}(" not in source, f"{module_path} calls forbidden {name}()"
```

This test is the load-bearing proof for the Completion Gate's "Krubit cannot execute
an unapproved moderation action" — it must fail loudly if any later change
accidentally introduces a moderation-mutation call anywhere in the watchdog modules,
not rely on someone remembering to check.

- [ ] **Step 2: Run the structural test and confirm it currently passes (nothing to fix yet) or fails (something to remove)**

Run: `.venv\Scripts\python.exe -m pytest tests\test_watchdog_structural_safety.py -q`

Expected: PASS if Tasks 1-8 were built correctly; if it FAILS, stop and remove the
offending call before proceeding — this is the single most important check in the
whole phase.

- [ ] **Step 3: Add health facts and write the operator guide**

Follow the `docs/operations/phase-2-creator-signal-hub.md` structure: exact env var
names, the Message Content privileged-intent enablement steps in the Discord
Developer Portal, shadow-mode explanation, and — matching Phase 2's hard-won lesson —
an explicit statement of what the enable flags actually gate, verified against the
code, not assumed.

- [ ] **Step 4: Run full automated verification**

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\ruff.exe check .`

Run: `.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe`

Run: `git diff --check`

Expected: all tests pass; Ruff and Pyright report zero findings; diff check exits 0.

- [ ] **Step 5: Commit documentation and health integration**

```powershell
git add src/krubit/services/health.py tests/test_watchdog_structural_safety.py tests/test_health_service.py README.md .env.example docs/roadmaps/2026-08-03-krubit-phase-rollout.md docs/operations/phase-3-watchdog.md docs/devlogs/2026-08-05-phase-3-watchdog.md
git commit -m "docs: close Phase 3 watchdog"
```

## Final Integration Gate

- [ ] Re-read `docs/superpowers/specs/2026-08-05-phase-3-watchdog-design.md` line by
  line and confirm every Completion Gate item has direct evidence, especially the
  structural no-moderation-authority proof from Task 9.
- [ ] Confirm `git status --short` contains no unintended or secret-bearing files.
- [ ] Confirm exactly one Krubit process tree is running from the reviewed build.
- [ ] Use `superpowers:verification-before-completion` before any completion claim.
- [ ] Use `superpowers:finishing-a-development-branch` and let the user choose local
  merge, PR, or branch preservation.
- [ ] Devlog, commit, and push only when explicitly requested — this build is running
  unattended overnight per explicit user authorization to continue through the
  roadmap; still do not push to any remote without that separately having been
  granted.
