"""Watchdog domain vocabulary: pure value objects and deterministic risk-band evaluation.

Phase 3 ("Entry Sniffing, bounded watch windows, raid/spam-wave detection, and
incident evidence") carries zero autonomous moderation authority — this module only
models *detection and evidence*, never an action that mutates a member, role, or
message. Every type here is a frozen, `__post_init__`-validated dataclass; the only
behavior is `evaluate_risk_band`, a pure function with no I/O, no clock reads, and no
Discord objects, matching the sibling `krubit.domain.creator_signals` module's
"framework-independent" convention.

## Threshold design (safety-sensitive — read before changing)

`evaluate_risk_band` maps a tuple of `RiskSignal` to a `RiskBand` by computing each
signal's *effective weight* as `weight * confidence` and summing them. Multiplying by
confidence means a signal the detector is unsure about (e.g. confidence 0.5) pulls a
member toward a higher band only half as hard as the same signal observed with full
confidence — an uncertain observation should never carry as much force as a certain
one, since a false accusation is a worse failure mode than a slow one.

Two fixed constants (`_SUSPICIOUS_THRESHOLD` and `_INCIDENT_THRESHOLD`) then bucket
the summed effective weight into a band:

- `CLEAR` is reserved *exclusively* for the empty-signals case. The design doc's Risk
  Bands section is explicit that `clear` means "no watch window, no residual record
  beyond the one assessment" — the moment even one weak, low-confidence signal fires,
  Entry Sniff observed something atypical about the join and the member graduates to
  at least bounded elevated monitoring (`WATCH`), never back to a no-signal state.
- `WATCH` (0 <= effective weight < 3.0): a single ordinary signal (e.g. weight 3 at
  ~80-90% confidence, effective ~2.4-2.7) lands here. One signal alone is common and
  not inherently alarming — new accounts and default avatars happen to genuine new
  members constantly — so it earns quiet, staff-invisible monitoring rather than a
  notification.
- `SUSPICIOUS` (3.0 <= effective weight < 6.0): reached either by one strong signal
  or, more commonly, by *two or more* ordinary signals corroborating each other (e.g.
  the two-signal test fixture: weight 3 @ 0.9 + weight 4 @ 0.8 = 2.7 + 3.2 = 5.9).
  Correlation across independent signals is what should elevate risk — it is much
  harder to explain away two unrelated red flags than one — so this band triggers a
  staff notification (not yet Zariya) per the design doc.
- `INCIDENT` (effective weight >= 6.0): requires either one very strong, high-
  confidence signal (e.g. weight 8 @ 0.9 = 7.2 — a near-certain match against a known
  attack signature) or several corroborating moderate signals stacking well past the
  SUSPICIOUS threshold. This is deliberately a high bar: entering `INCIDENT` triggers
  staff *and* Zariya notification plus a durable evidence packet, and — per the
  Non-Negotiable Boundaries — always ends in "recommend a reversible action," never an
  automatic one. A single ambiguous signal must never be enough to cross this line.

These thresholds are fixed named constants, not per-guild configuration, so that
`evaluate_risk_band` stays pure, reproducible, and auditable: the same signals always
produce the same band and the same plain-language explanation naming every signal
that contributed, never an opaque score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from krubit.domain.models import JSONValue
from krubit.security.redaction import redact

_MAX_NAME_LENGTH = 64
_MAX_DETAIL_LENGTH = 300
_MAX_EXPLANATION_LENGTH = 4_000
_MAX_INCIDENT_ID_LENGTH = 64
_MAX_ACTION_LENGTH = 500
_MAX_REASON_LENGTH = 300
_MAX_URL_LENGTH = 2_048
_MAX_EVENT_ID_LENGTH = 200

_MIN_SIGNAL_WEIGHT = 1
_MAX_SIGNAL_WEIGHT = 10

# See the module docstring "Threshold design" section for the rationale behind these
# two fixed cut points.
_SUSPICIOUS_THRESHOLD = 3.0
_INCIDENT_THRESHOLD = 6.0

_LIST_KINDS = frozenset({"allow", "block"})


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class RiskBand(StrEnum):
    """Ascending-severity outcome of `evaluate_risk_band`.

    A member never sees their own or another member's band; only authorized staff
    and Zariya do (per the design doc's Product Decisions).
    """

    CLEAR = "clear"
    WATCH = "watch"
    SUSPICIOUS = "suspicious"
    INCIDENT = "incident"


class WatchWindowCloseReason(StrEnum):
    """Why a `WatchWindow` stopped being open."""

    EXPIRED = "expired"
    ESCALATED = "escalated"
    STAFF_OVERRIDE = "staff_override"


class IncidentKind(StrEnum):
    """The detection category that produced an `Incident`.

    All five route into the same evidence-packet and notification path — there is no
    separate enforcement path for any of them.
    """

    MEMBER = "member"
    RAID = "raid"
    SPAM_WAVE = "spam_wave"
    WEBHOOK_ABUSE = "webhook_abuse"
    PERMISSION_RISK = "permission_risk"


@dataclass(frozen=True, slots=True)
class RiskSignal:
    """One bounded, named, explainable contribution to a risk-band evaluation.

    `weight` is a fixed, bounded integer (never a free-form score) naming how much
    this *kind* of signal matters in the abstract; `confidence` is how sure the
    detector is that this particular observation is real. See the module docstring
    for how the two combine.

    `detail` is redacted (via `redact()`) unconditionally in `__post_init__`, before
    this frozen instance ever becomes visible to a caller. This is deliberately a
    structural guarantee, not a caller convention: there is no way to construct a
    `RiskSignal` — anywhere in the codebase, including a test, a future detector, or
    a hand-built instance that skips every other helper in this phase — whose
    `.detail` holds unredacted content. That in turn makes `repr()`/`str()` of a
    `RiskSignal` (and of any `EvidencePacket` holding one, since its default `repr`
    recurses into `.signals`) safe by construction, closing the same class of "repr
    leaks a secret" bug `krubit.config.Settings` and
    `krubit.integrations.meta.MetaOAuthGrant` were fixed for in Phase 2 — here via
    redacting the value itself rather than hiding the field from `repr` entirely,
    since (unlike a token) `detail` is meant to stay human-readable evidence.
    """

    name: str
    weight: int
    detail: str
    confidence: float

    def __post_init__(self) -> None:
        _require_text("name", self.name, limit=_MAX_NAME_LENGTH)
        _require_text("detail", self.detail, limit=_MAX_DETAIL_LENGTH)
        if type(self.weight) is not int:
            raise ValueError("weight must be an int")
        if not (_MIN_SIGNAL_WEIGHT <= self.weight <= _MAX_SIGNAL_WEIGHT):
            raise ValueError(
                f"weight must be between {_MIN_SIGNAL_WEIGHT} and {_MAX_SIGNAL_WEIGHT}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        redacted_detail = redact(self.detail)
        if not isinstance(redacted_detail, str):
            raise TypeError("redact() must return a str for a str input")
        object.__setattr__(self, "detail", redacted_detail)


def evaluate_risk_band(signals: tuple[RiskSignal, ...]) -> tuple[RiskBand, str]:
    """Deterministically map observed signals to a `RiskBand` and a full explanation.

    Pure: no I/O, no clock reads, no randomness. Calling this twice with the same
    `signals` tuple always returns an equal result. The explanation names every
    contributing signal (not a summary) so a human reviewer can audit exactly why the
    band was assigned. See the module docstring for the threshold rationale.
    """
    if type(signals) is not tuple:
        raise ValueError("signals must be a tuple")
    if not signals:
        return RiskBand.CLEAR, "no signals observed"

    effective_total = sum(signal.weight * signal.confidence for signal in signals)

    if effective_total < _SUSPICIOUS_THRESHOLD:
        band = RiskBand.WATCH
    elif effective_total < _INCIDENT_THRESHOLD:
        band = RiskBand.SUSPICIOUS
    else:
        band = RiskBand.INCIDENT

    contributions = "; ".join(
        f"{signal.name} (weight={signal.weight}, confidence={signal.confidence:.2f}, "
        f"effective={signal.weight * signal.confidence:.2f}): {signal.detail}"
        for signal in signals
    )
    explanation = (
        f"{band.value} band: effective weight {effective_total:.2f} from "
        f"{len(signals)} signal(s) - {contributions}"
    )
    return band, explanation


@dataclass(frozen=True, slots=True)
class EntrySniffAssessment:
    """The one durable, versioned assessment produced for a single member join.

    Identity is `(guild_id, member_id, joined_at)`, matching the `entry_sniff_
    assessments` table's primary key. A rejoin after leave produces a new assessment
    with a new `joined_at`; it never resumes or averages a prior one.
    """

    guild_id: int
    member_id: int
    joined_at: datetime
    band: RiskBand
    signals: tuple[RiskSignal, ...]
    explanation: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("joined_at", self.joined_at)
        if type(self.band) is not RiskBand:
            raise ValueError("band must be a RiskBand")
        if type(self.signals) is not tuple:
            raise ValueError("signals must be a tuple")
        _require_text("explanation", self.explanation, limit=_MAX_EXPLANATION_LENGTH)
        _require_aware("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class WatchWindow:
    """A bounded, automatically-expiring elevated-monitoring state for one member.

    Identity is `(guild_id, member_id)`, matching the `watch_windows` table's primary
    key. Never opened for `RiskBand.CLEAR` — per the design doc, a watch window is
    "opened automatically only for `watch` band or higher (never for `clear`)."
    `closed_at` and `close_reason` are either both set or both unset: a window is
    either still open or fully closed with a reason, never half-closed.
    """

    guild_id: int
    member_id: int
    opened_at: datetime
    expires_at: datetime
    band: RiskBand
    closed_at: datetime | None
    close_reason: WatchWindowCloseReason | None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("opened_at", self.opened_at)
        _require_aware("expires_at", self.expires_at)
        if self.expires_at <= self.opened_at:
            raise ValueError("expires_at must be after opened_at")
        if type(self.band) is not RiskBand:
            raise ValueError("band must be a RiskBand")
        if self.band is RiskBand.CLEAR:
            raise ValueError("a watch window must never be opened for RiskBand.CLEAR")
        if (self.closed_at is None) != (self.close_reason is None):
            raise ValueError("closed_at and close_reason must both be set or both be unset")
        if self.closed_at is not None:
            _require_aware("closed_at", self.closed_at)
            if self.closed_at < self.opened_at:
                raise ValueError("closed_at must not precede opened_at")
        if self.close_reason is not None and type(self.close_reason) is not WatchWindowCloseReason:
            raise ValueError("close_reason must be a WatchWindowCloseReason")


@dataclass(frozen=True, slots=True)
class Incident:
    """A durable evidence-backed record for one incident-band detection.

    Identity is `(guild_id, incident_id)`, matching the `incidents` table's primary
    key. `band` is always `RiskBand.INCIDENT` — incidents are only ever created for
    incident-band detections (member assessments or guild-scoped raid/spam-wave/
    webhook-abuse/permission-risk detections); this is enforced here structurally
    rather than left to callers, since "all four route into the same evidence-packet
    and notification path as an incident-band member assessment" per the design doc.
    `recommended_action` is always human-reviewed free text; nothing in this phase
    executes it automatically.
    """

    guild_id: int
    incident_id: str
    kind: IncidentKind
    band: RiskBand
    opened_at: datetime
    evidence_packet_id: str
    recommended_action: str
    acknowledged_by: int | None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("incident_id", self.incident_id, limit=_MAX_INCIDENT_ID_LENGTH)
        if type(self.kind) is not IncidentKind:
            raise ValueError("kind must be an IncidentKind")
        if type(self.band) is not RiskBand:
            raise ValueError("band must be a RiskBand")
        if self.band is not RiskBand.INCIDENT:
            raise ValueError("an Incident record requires RiskBand.INCIDENT")
        _require_aware("opened_at", self.opened_at)
        _require_text("evidence_packet_id", self.evidence_packet_id, limit=_MAX_INCIDENT_ID_LENGTH)
        _require_text("recommended_action", self.recommended_action, limit=_MAX_ACTION_LENGTH)
        if self.acknowledged_by is not None:
            _require_positive_id("acknowledged_by", self.acknowledged_by)


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Authorized facts backing one incident: signals, message links, and event IDs.

    Never a single opaque "risk score" — `signals` must be non-empty so every
    packet is explainable from the named signals that fired, per the design doc's
    Evidence Packets section. `message_links` are jump-URLs (not full message
    content, unless the specific message triggered the signal — in which case only
    that message's redacted content plus the trigger reason belongs in the matching
    `RiskSignal.detail`, not in this field).

    `to_storage_dict()` is the only supported way to obtain this packet's persisted
    representation, and it always calls `redact()` on every free-text field itself
    (see its docstring) — this is a deliberate strengthening of Task 1's original
    "redaction happens at the storage layer" note: rather than trusting every future
    caller to remember to redact before writing an `EvidencePacket` to storage, the
    guarantee is now structural, on the type itself. Redaction is intentionally
    still safe to apply twice: `krubit.services.incident_evidence.build_evidence_packet`
    (Task 6) also redacts each signal's `detail` before constructing this dataclass,
    so a packet built through the normal path is redacted once at assembly and once
    again here, as defense in depth. `to_storage_dict()` covers a packet built any
    other way too — directly, in a test, or by future code that never goes through
    `build_evidence_packet`.
    """

    guild_id: int
    incident_id: str
    signals: tuple[RiskSignal, ...]
    message_links: tuple[str, ...]
    event_ids: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("incident_id", self.incident_id, limit=_MAX_INCIDENT_ID_LENGTH)
        if type(self.signals) is not tuple:
            raise ValueError("signals must be a tuple")
        if not self.signals:
            raise ValueError("an evidence packet must name at least one signal")
        if type(self.message_links) is not tuple:
            raise ValueError("message_links must be a tuple")
        for link in self.message_links:
            _require_text("message_links entry", link, limit=_MAX_URL_LENGTH)
            if not link.startswith("https://"):
                raise ValueError("message_links entries must use https")
        if type(self.event_ids) is not tuple:
            raise ValueError("event_ids must be a tuple")
        for event_id in self.event_ids:
            _require_text("event_ids entry", event_id, limit=_MAX_EVENT_ID_LENGTH)
        _require_aware("created_at", self.created_at)

    def to_storage_dict(self) -> dict[str, JSONValue]:
        """Return this packet's redacted, JSON-safe representation for storage.

        Always passes the full payload through `redact()` before returning it — see
        the class docstring for why this method, not a caller convention, is the
        redaction boundary. There is no other method on this type that exposes a
        dict/JSON representation, so this is structurally the only path to a
        storage-ready value.
        """
        payload: JSONValue = {
            "guild_id": self.guild_id,
            "incident_id": self.incident_id,
            "signals": [
                {
                    "name": signal.name,
                    "weight": signal.weight,
                    "detail": signal.detail,
                    "confidence": signal.confidence,
                }
                for signal in self.signals
            ],
            "message_links": list(self.message_links),
            "event_ids": list(self.event_ids),
            "created_at": self.created_at.isoformat(),
        }
        redacted = redact(payload)
        if not isinstance(redacted, dict):
            raise TypeError("redact() must return a dict for a dict payload")
        return redacted


@dataclass(frozen=True, slots=True)
class AllowBlockEntry:
    """One guild-configured allow/block entry for a Discord user ID.

    Identity is `(guild_id, discord_user_id)`, matching the `guild_allow_block_lists`
    table's primary key. `list_kind` is either `"allow"` or `"block"` — staff-set
    facts consumed by Entry Sniff signal extraction, not inferred from behavior.
    """

    guild_id: int
    discord_user_id: int
    list_kind: str
    reason: str
    set_by: int
    set_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("discord_user_id", self.discord_user_id)
        if self.list_kind not in _LIST_KINDS:
            raise ValueError(f"list_kind must be one of {sorted(_LIST_KINDS)}")
        _require_text("reason", self.reason, limit=_MAX_REASON_LENGTH)
        _require_positive_id("set_by", self.set_by)
        _require_aware("set_at", self.set_at)
