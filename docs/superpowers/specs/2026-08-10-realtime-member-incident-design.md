# Real-Time Member-Join Incident Notification Design

**Date:** 2026-08-10
**Status:** Approved for implementation planning
**Scope:** Close Watchdog's single biggest documented functional gap — a
lone `INCIDENT`-band join is currently never notified to staff in real
time, only ever caught (if at all) by the periodic sweep cycle's
guild-wide detectors, none of which look at individual member risk.

## Context

`WatchdogRuntime.on_member_join` (`src/krubit/discord/watchdog_runtime.py:179`)
already calls `EntrySniffService.assess_join` (producing a durable,
banded assessment) and `WatchWindowService.open_if_warranted` for any
band above `CLEAR`. Its own docstring names the gap explicitly:
"Converting an `INCIDENT`-band member assessment into a durable
`Incident`/staff notification is intentionally out of this task's scope."
`IncidentKind.MEMBER` already exists in the domain enum
(`src/krubit/domain/watchdog.py:127`) and is documented as routing "into
the same evidence-packet and notification path" as every other incident
kind — it has simply never been constructed anywhere.

The sweep-cycle detectors (`RaidDetector`, `SpamWaveDetector`,
`WebhookAbuseDetector`, `PermissionRiskDetector`, all in
`src/krubit/services/raid_detection.py`/`webhook_and_permission_risk.py`)
already establish the exact pattern to reuse: build a signal, construct
an `Incident`, persist it via `store.record_incident`, record a
`sniff_receipt`, and call `WatchdogRuntime.notify_staff(incident)` — this
plan mirrors that pattern for member joins rather than inventing a new
one.

## Confirmed decisions (from conversation)

1. **No same-kind dedup guard for `MEMBER` incidents.** The four existing
   sweep detectors each suppress a second notification of the same kind
   while an earlier one is still "open" within their detection window
   (`_has_open_incident_of_kind`) — appropriate for a raid/spam-wave/etc.,
   which is one continuing phenomenon. A member join is not: each
   `INCIDENT`-band join is an independent member and independent
   evidence, so every one gets its own `Incident` and its own staff
   notification, with no suppression regardless of how many other members
   were recently flagged.
2. **Evidence-packet handling matches the existing (incomplete)
   pattern exactly — not fixed here.** Every existing detector uses a
   placeholder evidence-builder (`_default_evidence_builder` in
   `raid_detection.py`) that mints an opaque ID string and discards the
   actual signal data; there is no storage table for a full
   `EvidencePacket` anywhere yet (a separate, already-documented Phase 3
   gap: "`/fetch incident`/`/fetch evidence` honestly reconstruct signal
   names from a receipt rather than showing genuine stored per-signal
   detail"). This plan deliberately keeps using that same placeholder
   pattern for consistency with the other four incident kinds, rather
   than building real evidence-packet storage as a side effect of closing
   an unrelated gap. Building that storage is real, separate,
   meaningfully larger follow-up work (a new table, wiring
   `build_evidence_packet` into five call sites, not one) — explicitly
   out of scope here.
3. **Signals used: `assessment.signals` directly** (already computed by
   `EntrySniffService.assess_join`), not a new synthetic single-signal
   summary — this is more detailed evidence than the sweep detectors'
   pattern (which synthesize one summary signal per incident), since a
   full per-signal breakdown is already sitting on the assessment object
   with no extra computation needed.

## Implementation approach

- In `WatchdogRuntime.on_member_join`, after the existing
  `assess_join`/`open_if_warranted` calls (order unchanged, this is
  purely additive): if `assessment.band is RiskBand.INCIDENT`, construct
  an `Incident` with `kind=IncidentKind.MEMBER`,
  `evidence_packet_id=self._evidence_builder(...)`. `WatchdogRuntime`
  does not currently hold an evidence builder of its own (each detector
  holds a private one internally, defaulted to `raid_detection.py`'s
  `_default_evidence_builder`) — add a new optional constructor parameter
  to `WatchdogRuntime.__init__`, `evidence_builder:
  Callable[[int, tuple[RiskSignal, ...], datetime], str] | None = None`,
  defaulting to a small local placeholder matching the exact same shape
  (`f"evidence:{uuid4().hex}"`, discarding its arguments) rather than
  importing the other module's private underscore-prefixed function.
  `recommended_action` a new, plainly-worded constant (e.g.
  "Review this member's Entry Sniff assessment and signals; consider
  manual verification, timeout, or removal if warranted. No automatic
  action has been taken." — matching `_RAID_RECOMMENDED_ACTION`'s exact
  tone and the module-wide "never a black-box score, never an automatic
  action" convention), `acknowledged_by=None`.
- Persist via `store.record_incident(incident)`, record a
  `sniff_receipt` (`action="incident_recorded"`, matching
  `RaidDetector._record_incident`'s exact receipt shape:
  `{"kind": ..., "signal_names": [...]}`), then call
  `self.notify_staff(saved_incident)` — reusing the existing method
  as-is, no changes to `notify_staff` itself.
- This logic can live directly in `on_member_join` or a small private
  helper method on `WatchdogRuntime` (matching how each existing detector
  has its own private `_record_incident` rather than a shared one across
  detectors — this codebase's established convention favors that
  duplication over a premature shared abstraction).

## Explicit Exclusions

- No evidence-packet storage table or wiring of the real
  `build_evidence_packet` function anywhere (decision 2).
- No dedup/suppression logic for repeated member-join incidents
  (decision 1).
- No change to the four existing sweep-cycle detectors' behavior.
- No change to `notify_staff`'s own implementation.
- No new `/fetch` command — `/fetch sniff incident`/`/fetch sniff
  evidence` already read from the `incidents`/receipt tables generically
  by `incident_id`, and will surface a `MEMBER`-kind incident exactly as
  they already surface `RAID`/`SPAM_WAVE`/etc. ones, with no code change
  needed on that side.

## Testing

- Unit test: an `assess_join` call producing an `INCIDENT`-band
  assessment results in exactly one `Incident` persisted with
  `kind=IncidentKind.MEMBER`, and `notify_staff` is called exactly once
  with it.
- Unit test: a `WATCH`/`SUSPICIOUS`/`CLEAR`-band assessment produces no
  `Incident` and no `notify_staff` call (only the existing watch-window
  behavior, unchanged).
- Unit test: two separate members both producing `INCIDENT`-band
  assessments in quick succession each get their own `Incident` and their
  own `notify_staff` call — no suppression (proves decision 1).
- Unit test: the persisted `Incident`'s signals/evidence trace back to
  `assessment.signals`, not a synthesized summary.
- Test that `watchdog_enabled=False` still short-circuits before any of
  this runs (matching the existing guard already at the top of
  `on_member_join`).

## Completion Gate

Complete when: a member whose Entry Sniff assessment reaches
`RiskBand.INCIDENT` produces a durable `Incident` and a staff
notification immediately at join time (not only via the next sweep
cycle), every other assessment band's existing behavior is unchanged, no
dedup suppresses independent member incidents, and the full test suite
passes with no regressions.
