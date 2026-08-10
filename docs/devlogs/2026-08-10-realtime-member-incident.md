# Krubit Development Log: Real-Time Member-Join Incident Notification

**Date:** August 10, 2026
**Status:** Implementation, fix wave, and final review complete. Merged to `main` and live-configured (`KRUBIT_WATCHDOG_ENABLED`, `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED`, `KRUBIT_STAFF_CHANNEL_ID` all set on Railway, redeploy confirmed successful).

## Scope

Watchdog's single biggest documented functional gap — a lone
`INCIDENT`-band member join was previously never flagged to staff in
real time, only ever caught (if at all) by the periodic sweep cycle's
guild-wide detectors (raid, spam wave, webhook abuse, permission risk),
none of which evaluate an individual member's own risk. See
[the design spec](../superpowers/specs/2026-08-10-realtime-member-incident-design.md).

## Delivered

`WatchdogRuntime.on_member_join` now constructs and persists an
`Incident` (kind `MEMBER`) and calls the existing `notify_staff` when an
Entry Sniff assessment reaches `RiskBand.INCIDENT`, mirroring
`RaidDetector`'s existing incident-construction pattern exactly. Evidence
handling deliberately matches the existing placeholder pattern used by
every other detector (no new storage table) — building real
evidence-packet persistence was explicitly scoped out as separate,
larger follow-up work, not silently expanded into this task.

## Final review and fix wave

The final whole-branch review found a real Critical defect — not in the
implementation, but in the **design's own premise**. The plan explicitly
chose no notification suppression, reasoning that "each `INCIDENT`-band
join is an independent member and independent evidence." That premise is
mathematically false for two of the signals that can drive a join to
`INCIDENT`: `join_velocity` and `join_cluster_similarity` are guild-wide
correlation signals computed from *other* recent joiners, not the
joining member's own attributes — and their high tiers alone sum to 8.7
effective weight, clearing the 6.0 `INCIDENT` threshold with zero
individual-member signal contribution at all. During an actual raid
(exactly the scenario `RaidDetector`'s own sweep-cycle detection exists
to catch as one incident), every participant past roughly the 10th join
would have independently reached `INCIDENT` band, producing 10-20
separate staff pings ahead of — and drowning out — the single `RAID`
alert. This reintroduced the exact notification-storm problem an earlier
Watchdog review's `_has_open_incident_of_kind` guard was built to
prevent for the other four detectors, at the one moment staff most need
a clean signal.

Fixed without touching the recording behavior at all: every
`INCIDENT`-band join is still durably recorded unconditionally, but the
`notify_staff` call is now gated — suppressed only when the band is
reached purely via the two raid-correlation signals (checked by
re-running the existing pure `evaluate_risk_band` function on the signal
set with those two names excluded; if the member's own remaining signals
still independently reach `INCIDENT`, the notification fires as before).
Independently re-derived and confirmed correct during the scoped
re-review, including tracing that the fix doesn't over-suppress a
genuinely risky lone joiner who happens to also trigger the correlation
signals.

Three Important findings were fixed in the same pass: the staff embed
never identified which member it was about (fixed by encoding the member
id into `incident_id` itself, which already appears in the live embed
and in `/fetch incident`/`/fetch evidence`); the new incident-construction
step had no exception isolation, unlike `sweep_cycle`'s per-detector
`try`/`except` boundary, meaning an unhandled failure here could have
blocked the Activity Ledger and membership-announcement steps for the
same join (fixed by matching that same isolation pattern); and the
operator runbook plus a docstring cross-reference still asserted the
*opposite* of the shipped behavior (updated to accurately describe the
correlation-gated notification, not just "it's fixed now").

## Live configuration

Post-merge, the feature was wired up live on Railway: `KRUBIT_WATCHDOG_ENABLED`
and `KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED` (both previously `false`, now
`true`) and a new `KRUBIT_STAFF_CHANNEL_ID` pointing at `#mod-room` — the
project owner's consolidated channel for all server status signals, good
and bad. Bot online status, successful redeploy, and command
responsiveness were all confirmed post-deploy. A live end-to-end trigger
(via a deliberately fresh throwaway account reaching `INCIDENT` band
through its own attributes) was scoped but deferred — the project owner
has several bot accounts joining soon that will naturally exercise the
real pipeline.

## Known limitations

- No block-list management command exists in the Discord command layer
  (only a read command, `/fetch sniff watchlist`, surfaces allow/block
  entries) — noted during smoke-test planning, not part of this branch's
  scope.

## Test evidence

Full suite: 1173/1173 passing at merge. Ruff clean throughout. Pyright
error count unchanged from this file's pre-existing baseline (43 errors,
all pre-existing `discord.py` stub noise, confirmed via before/after
diffing).
