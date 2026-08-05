"""Assemble redacted `EvidencePacket`s and correlate Discord AutoMod actions.

Task 6 of the Phase 3 Watchdog plan. Two independent, pure, I/O-free functions:

- `build_evidence_packet(incident, raw_signals, raw_messages) -> EvidencePacket`:
  the real evidence-packet builder every Task 3-5 detector (`entry_sniff.py`,
  `watch_window.py`, `raid_detection.py`, `webhook_and_permission_risk.py`) was
  built against an *injected* placeholder (`EvidencePacketBuilder`) for, since this
  builder did not exist yet when they were written. This task does not change those
  detectors — wiring this real builder into them is later work — it only builds the
  function itself.
- `correlate_automod_action(action, now) -> RiskSignal | None`: reads the payload of
  Discord's own `on_automod_action` event (already wired in
  `src/krubit/discord/bot.py`, `KrubitBot.on_automod_action`) and turns it into a
  `RiskSignal` that can feed the same `evaluate_risk_band` path every other detector
  in this phase feeds. It adds no new AutoMod rule creation and no enforcement call
  of any kind (no `.create_rule(`, `.timeout(`, `.kick(`, `.ban(`, `.delete(`) — it
  is a read-only correlation of an event Discord already acted on.

## Redaction boundary (safety-sensitive — read before changing)

`build_evidence_packet` never copies a message's raw `.content` into the packet in
any form — only `.jump_url` (a link back to the message, not its payload) and `.id`
(an opaque identifier) ever reach `EvidencePacket.message_links` / `.event_ids`. This
is the strongest possible redaction available: content that is never stored cannot
later leak through a redaction regex that fails to catch some new secret shape. It
also matches `EvidencePacket`'s own docstring in `krubit/domain/watchdog.py`, which
reserves message *content* for the one case where a specific message directly
triggered a signal — and in that case, the caller is expected to have already folded
that (still-unredacted) content into the corresponding `RiskSignal.detail` before
calling this function, exactly like `krubit.discord.watchdog_events.
extract_message_signals` already does (e.g. its `message contains a URL with a
{reason}: {url[:200]}` signal detail).

`raw_signals` entries, unlike raw message content, legitimately need to reach the
packet — that is the entire point of an evidence packet: named, explainable signals,
never an opaque score. So every signal's `detail` is passed through `redact()` here,
before an `EvidencePacket` is ever constructed. This is defense-in-depth layered on
top of `EvidencePacket.to_storage_dict()`'s own independent, structural redaction
guarantee (see that method's docstring) — a caller who bypasses this builder entirely
and constructs an `EvidencePacket` by hand still cannot produce an unredacted storage
dict, because `to_storage_dict()` redacts unconditionally, not by convention.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast

import discord

from krubit.domain.watchdog import EvidencePacket, Incident, RiskSignal
from krubit.security.redaction import redact

# Discord's own AutoMod already confirmed the match with certainty (it took an
# action); Krubit did not independently verify it. This weight/confidence pair is
# deliberately in the same range as `RaidDetector`'s own single-strong-signal case
# (see `raid_detection.py`'s "Threshold design" docs) — one automod-correlated
# signal alone should meaningfully move the risk band, but should not, by itself,
# automatically reach `RiskBand.INCIDENT` (weight 5 * confidence 0.9 = 4.5, which
# lands in `SUSPICIOUS`, not `INCIDENT` — see `evaluate_risk_band`'s thresholds).
_AUTOMOD_SIGNAL_WEIGHT = 5
_AUTOMOD_SIGNAL_CONFIDENCE = 0.9
_MAX_DETAIL_LENGTH = 300


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class EvidenceMessage(Protocol):
    """The minimal shape `build_evidence_packet` reads from a message.

    Matches the subset of `discord.Message` this module actually touches. Note that
    `.content` is deliberately *not* part of this protocol: see the module
    docstring's "Redaction boundary" section for why raw message content never
    reaches an `EvidencePacket`.
    """

    @property
    def id(self) -> int: ...
    @property
    def jump_url(self) -> str: ...


def _redact_signal(signal: RiskSignal) -> RiskSignal:
    """Return `signal` with its `detail` passed through `redact()`.

    `redact()` returns `JSONValue`; `RiskSignal.detail` is always a `str`, and
    `redact()` returns a `str` unchanged in shape for a `str` input (see
    `krubit.security.redaction.redact`), so the cast here is safe.
    """
    return replace(signal, detail=cast(str, redact(signal.detail)))


def build_evidence_packet(
    incident: Incident,
    raw_signals: tuple[RiskSignal, ...],
    raw_messages: tuple[EvidenceMessage, ...],
) -> EvidencePacket:
    """Assemble a redacted `EvidencePacket` backing `incident`.

    `raw_signals` becomes `EvidencePacket.signals` (required non-empty, matching
    `EvidencePacket`'s own constraint), each entry's `detail` redacted first.
    `raw_messages` contributes `message_links` (each message's `jump_url`) and
    `event_ids` (each message's `id`, stringified) — never message content; see the
    module docstring. Order-preserving de-duplication, since the same message can
    legitimately be cited by more than one signal.
    """
    if type(raw_signals) is not tuple:
        raise TypeError("raw_signals must be a tuple")
    if type(raw_messages) is not tuple:
        raise TypeError("raw_messages must be a tuple")
    if not raw_signals:
        raise ValueError("build_evidence_packet requires at least one signal")

    redacted_signals = tuple(_redact_signal(signal) for signal in raw_signals)
    message_links = tuple(dict.fromkeys(message.jump_url for message in raw_messages))
    event_ids = tuple(dict.fromkeys(str(message.id) for message in raw_messages))

    return EvidencePacket(
        guild_id=incident.guild_id,
        incident_id=incident.incident_id,
        signals=redacted_signals,
        message_links=message_links,
        event_ids=event_ids,
        created_at=incident.opened_at,
    )


def correlate_automod_action(action: discord.AutoModAction, now: datetime) -> RiskSignal | None:
    """Turn an already-fired Discord AutoMod action into a `RiskSignal`, or `None`.

    Reads only fields already present on the event `KrubitBot.on_automod_action`
    (`src/krubit/discord/bot.py:840`) receives from discord.py: `rule_id`,
    `rule_trigger_type`, and (when present) `matched_keyword`/`matched_content`.
    Makes no Discord API call and takes no action — no rule creation, no timeout,
    kick, ban, or message delete. This is a pure read-then-name mapping from
    Discord's own enforcement decision into Krubit's independent, evidence-only risk
    vocabulary, so it can feed `evaluate_risk_band` alongside every other detector's
    signals per the design doc's "route into the same evidence-packet and
    notification path" model.

    Returns `None` when `action` carries no recognizable `rule_trigger_type` (e.g. a
    minimal/legacy payload) rather than guessing — matching the "degrade honestly"
    stance the rest of this phase uses for degraded inputs (see
    `raid_detection.py`'s `SpamWaveDetector`, `watch_window.py`'s module docstring).
    """
    _require_aware("now", now)

    trigger = getattr(action, "rule_trigger_type", None)
    trigger_name = getattr(trigger, "name", None)
    if not trigger_name:
        return None

    matched = getattr(action, "matched_keyword", None) or getattr(action, "matched_content", None)
    detail = f"Discord AutoMod rule {action.rule_id} triggered ({trigger_name})"
    if matched:
        detail = f"{detail}: matched {matched!r}"
    if len(detail) > _MAX_DETAIL_LENGTH:
        detail = detail[: _MAX_DETAIL_LENGTH - 1] + "…"

    return RiskSignal(
        name=f"automod_correlated_{trigger_name}",
        weight=_AUTOMOD_SIGNAL_WEIGHT,
        detail=detail,
        confidence=_AUTOMOD_SIGNAL_CONFIDENCE,
    )
