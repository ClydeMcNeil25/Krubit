"""Activity ledger domain vocabulary: pure value objects and deterministic calculation.

Phase 4 ("Member Activity Ledger") records **factual participation events, never
content**: a message event records that a member posted in a channel at a time, never
the message text; a reaction event records which emoji-shaped reaction was added,
never any inferred sentiment; a voice event records join/leave timestamps and computed
duration, never audio or a transcript. Every type here is a frozen,
`__post_init__`-validated dataclass, matching the sibling `krubit.domain.watchdog` and
`krubit.domain.creator_signals` modules' "framework-independent" convention: no I/O, no
clock reads, no Discord objects, no persistence.

## "Meaningful action" event kinds (measurement-relevant — read before changing)

`time_to_activation`, `cohort_membership`, and `participation_trend` all need to
answer "did this member do something that counts as genuine participation," and the
design doc is explicit that this must be "a named, explainable rule (not a black-box
heuristic)": the first non-join event of a configured set of "meaningful" event kinds
(message, reaction, voice join, event RSVP) after the member's join event.

`_MEANINGFUL_EVENT_KINDS` is that fixed, named set:

- `MESSAGE` and `REACTION` are the two lowest-friction, most common forms of genuine
  participation (posting and reacting) — exactly the two kinds the design doc's
  activation-rule sentence names first.
- `VOICE_SESSION` ("voice join") is included because joining a voice channel is a
  deliberate, synchronous act of showing up, not passive presence.
- `EVENT_ATTENDANCE` ("event RSVP") is included for the same reason: RSVPing to a
  Scheduled Event is a deliberate signal of intent to participate.

Deliberately **excluded**: `JOIN` (the reference point activation is measured from,
never itself a meaningful action), `ONBOARDING` (Rules Screening completion is often
compulsory gate-passing, not chosen participation), `ROLE_CHANGE` (staff- or
integration-driven, not a member choosing to participate), `MILESTONE` (a *derived*
fact materialized from other meaningful events — counting it here would let a
milestone count itself), and `MODERATION_RECEIPT` (a pointer to a Watchdog incident
this member was involved in, which is never "participation" in the sense this module
measures). This set is a fixed named constant, not per-guild configuration, so every
calculation in this module stays pure, reproducible, and auditable: the same events
always produce the same activation/retention answer, with no opaque score.

## Cohort-window boundary discipline

`cohort_membership` needs "no floating-point drift, no off-by-one on the window
boundary (inclusive join day, inclusive window-end day)" per the design doc, matching
Phase 3's quiet-hours half-open-interval discipline for date boundaries. Concretely:
a meaningful event counts toward retention if its **calendar date** (not exact
timestamp) falls anywhere in `[join_date, join_date + window_days]` inclusive on both
ends — so an event on the join member's local join day always counts (even if its
exact timestamp technically precedes the join timestamp, since the granularity the
design doc specifies is days, not seconds), and an event exactly `window_days` days
after the join day still counts, while an event on `window_days + 1` days after does
not. See `cohort_membership`'s docstring for the exact comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

_MAX_EMOJI_LENGTH = 64
_MAX_DETAIL_LENGTH = 300
_MAX_REASON_LENGTH = 300
_MAX_RECEIPT_ID_LENGTH = 200


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


class LedgerEventKind(StrEnum):
    """Every event kind the append-only per-member ledger records.

    See the module docstring's "Meaningful action" section for which of these count
    toward activation/retention/trend calculations and why.
    """

    JOIN = "join"
    ONBOARDING = "onboarding"
    MESSAGE = "message"
    REACTION = "reaction"
    VOICE_SESSION = "voice_session"
    EVENT_ATTENDANCE = "event_attendance"
    ROLE_CHANGE = "role_change"
    MILESTONE = "milestone"
    MODERATION_RECEIPT = "moderation_receipt"


MEANINGFUL_EVENT_KINDS: frozenset[LedgerEventKind] = frozenset(
    {
        LedgerEventKind.MESSAGE,
        LedgerEventKind.REACTION,
        LedgerEventKind.VOICE_SESSION,
        LedgerEventKind.EVENT_ATTENDANCE,
    }
)


class AttendanceAction(StrEnum):
    """Whether a Scheduled Event RSVP was added or removed."""

    ADD = "add"
    REMOVE = "remove"


class RoleChangeAction(StrEnum):
    """Whether a role was granted or removed for a member."""

    GRANTED = "granted"
    REMOVED = "removed"


class MilestoneKind(StrEnum):
    """A named, explainable milestone rule (never a black-box "loyalty score")."""

    MESSAGE_COUNT = "message_count"
    JOIN_ANNIVERSARY = "join_anniversary"


class CohortWindow(StrEnum):
    """The two retention windows the design doc requires: 7-day and 30-day."""

    SEVEN_DAY = "seven_day"
    THIRTY_DAY = "thirty_day"


_COHORT_WINDOW_DAYS: dict[CohortWindow, int] = {
    CohortWindow.SEVEN_DAY: 7,
    CohortWindow.THIRTY_DAY: 30,
}


def cohort_window_days(window: CohortWindow) -> int:
    """Return the fixed day count for a `CohortWindow` (7 or 30)."""
    if type(window) is not CohortWindow:
        raise ValueError("window must be a CohortWindow")
    return _COHORT_WINDOW_DAYS[window]


@dataclass(frozen=True, slots=True)
class JoinEvent:
    """A member's join event: the reference point activation is measured from.

    Never itself a "meaningful action" — see the module docstring.
    """

    guild_id: int
    member_id: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.JOIN


@dataclass(frozen=True, slots=True)
class OnboardingEvent:
    """Rules Screening completion, where observable."""

    guild_id: int
    member_id: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.ONBOARDING


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """A message post: channel + timestamp only, never message text."""

    guild_id: int
    member_id: int
    occurred_at: datetime
    channel_id: int
    thread_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_positive_id("channel_id", self.channel_id)
        if self.thread_id is not None:
            _require_positive_id("thread_id", self.thread_id)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.MESSAGE


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    """A reaction add: channel + emoji shape + timestamp, never inferred sentiment."""

    guild_id: int
    member_id: int
    occurred_at: datetime
    channel_id: int
    emoji: str

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_positive_id("channel_id", self.channel_id)
        _require_text("emoji", self.emoji, limit=_MAX_EMOJI_LENGTH)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.REACTION


@dataclass(frozen=True, slots=True)
class VoiceSessionEvent:
    """A voice channel session: join/leave timestamps and computed duration only.

    Never audio, never a transcript, never speaking-time-per-word inference.
    `occurred_at` is the voice-join timestamp (used as this event's ordering point
    for activation/retention/trend calculations); `duration` is derived from
    `occurred_at` and `left_at`, never stored redundantly.
    """

    guild_id: int
    member_id: int
    occurred_at: datetime
    left_at: datetime
    channel_id: int

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_aware("left_at", self.left_at)
        if self.left_at < self.occurred_at:
            raise ValueError("left_at must not precede occurred_at")
        _require_positive_id("channel_id", self.channel_id)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.VOICE_SESSION

    @property
    def duration(self) -> timedelta:
        return self.left_at - self.occurred_at


@dataclass(frozen=True, slots=True)
class EventAttendanceEvent:
    """A Scheduled Event RSVP add/remove."""

    guild_id: int
    member_id: int
    occurred_at: datetime
    scheduled_event_id: int
    action: AttendanceAction

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_positive_id("scheduled_event_id", self.scheduled_event_id)
        if type(self.action) is not AttendanceAction:
            raise ValueError("action must be an AttendanceAction")

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.EVENT_ATTENDANCE


@dataclass(frozen=True, slots=True)
class RoleChangeEvent:
    """A role granted/removed, reusing Phase 1's existing role-event tracking."""

    guild_id: int
    member_id: int
    occurred_at: datetime
    role_id: int
    action: RoleChangeAction

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_positive_id("role_id", self.role_id)
        if type(self.action) is not RoleChangeAction:
            raise ValueError("action must be a RoleChangeAction")

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.ROLE_CHANGE


@dataclass(frozen=True, slots=True)
class MilestoneEvent:
    """A materialized ledger event recording that a `Milestone` was reached."""

    guild_id: int
    member_id: int
    occurred_at: datetime
    milestone_kind: MilestoneKind
    detail: str

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        if type(self.milestone_kind) is not MilestoneKind:
            raise ValueError("milestone_kind must be a MilestoneKind")
        _require_text("detail", self.detail, limit=_MAX_DETAIL_LENGTH)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.MILESTONE


@dataclass(frozen=True, slots=True)
class ModerationReceiptEvent:
    """A redacted pointer to a Watchdog incident/receipt this member was involved in.

    Never the incident's raw content — `receipt_id` points at Phase 3's existing
    receipt records rather than re-deriving or duplicating them.
    """

    guild_id: int
    member_id: int
    occurred_at: datetime
    receipt_id: str

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("occurred_at", self.occurred_at)
        _require_text("receipt_id", self.receipt_id, limit=_MAX_RECEIPT_ID_LENGTH)

    @property
    def kind(self) -> LedgerEventKind:
        return LedgerEventKind.MODERATION_RECEIPT


LedgerEvent = (
    JoinEvent
    | OnboardingEvent
    | MessageEvent
    | ReactionEvent
    | VoiceSessionEvent
    | EventAttendanceEvent
    | RoleChangeEvent
    | MilestoneEvent
    | ModerationReceiptEvent
)
"""Union of every per-kind ledger event value object.

The domain model exposes distinct value objects per kind (never a single
polymorphic dataclass with optional fields) per the design doc; this alias is the
type calculation functions accept a tuple of.
"""


@dataclass(frozen=True, slots=True)
class Milestone:
    """A durable, materialized record that a member reached a named milestone rule."""

    guild_id: int
    member_id: int
    kind: MilestoneKind
    reached_at: datetime
    detail: str

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        if type(self.kind) is not MilestoneKind:
            raise ValueError("kind must be a MilestoneKind")
        _require_aware("reached_at", self.reached_at)
        _require_text("detail", self.detail, limit=_MAX_DETAIL_LENGTH)


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """The outcome of `time_to_activation` for one member.

    `activated=False` means "not yet activated," never an error — absence of any
    meaningful action within the retention window is an ordinary, expected outcome
    per the design doc.
    """

    activated: bool
    time_to_activation: timedelta | None
    activating_kind: LedgerEventKind | None

    def __post_init__(self) -> None:
        if type(self.activated) is not bool:
            raise ValueError("activated must be a bool")
        if self.activated:
            if self.time_to_activation is None:
                raise ValueError("an activated result must set time_to_activation")
            if self.time_to_activation < timedelta(0):
                raise ValueError("time_to_activation must not be negative")
            if self.activating_kind is None:
                raise ValueError("an activated result must set activating_kind")
            if self.activating_kind not in MEANINGFUL_EVENT_KINDS:
                raise ValueError("activating_kind must be a meaningful event kind")
        else:
            if self.time_to_activation is not None:
                raise ValueError("a non-activated result must not set time_to_activation")
            if self.activating_kind is not None:
                raise ValueError("a non-activated result must not set activating_kind")


@dataclass(frozen=True, slots=True)
class CohortResult:
    """The outcome of `cohort_membership` for one join cohort and window."""

    window: CohortWindow
    cohort_size: int
    retained_count: int
    retention_rate: float

    def __post_init__(self) -> None:
        if type(self.window) is not CohortWindow:
            raise ValueError("window must be a CohortWindow")
        if self.cohort_size < 0:
            raise ValueError("cohort_size must not be negative")
        if self.retained_count < 0:
            raise ValueError("retained_count must not be negative")
        if self.retained_count > self.cohort_size:
            raise ValueError("retained_count must not exceed cohort_size")
        expected_rate = 0.0 if self.cohort_size == 0 else self.retained_count / self.cohort_size
        if abs(self.retention_rate - expected_rate) > 1e-9:
            raise ValueError("retention_rate must equal retained_count / cohort_size")


@dataclass(frozen=True, slots=True)
class ParticipationTrend:
    """The outcome of `participation_trend` for one member and window.

    See `krubit.services.activation_retention.participation_trend`'s docstring for
    how `active_day_count`, `returning`, `channel_diversity`, and `event_diversity`
    are each derived from stored events, never a single opaque trend score.
    """

    window: CohortWindow
    active_day_count: int
    returning: bool
    channel_diversity: int
    event_diversity: int

    def __post_init__(self) -> None:
        if type(self.window) is not CohortWindow:
            raise ValueError("window must be a CohortWindow")
        if self.active_day_count < 0:
            raise ValueError("active_day_count must not be negative")
        if type(self.returning) is not bool:
            raise ValueError("returning must be a bool")
        if self.channel_diversity < 0:
            raise ValueError("channel_diversity must not be negative")
        if self.event_diversity < 0:
            raise ValueError("event_diversity must not be negative")


@dataclass(frozen=True, slots=True)
class RecognitionCandidate:
    """A factual shortlist entry Krubit surfaces to staff/Zariya for recognition.

    Per the design doc's "Recognition-candidate view" and the rollout doc's
    Non-Negotiable Boundaries: Krubit never assigns a numeric "worthiness" score,
    never ranks members against each other, and never drafts recognition wording —
    deciding *who deserves recognition* and writing the words is explicitly
    Zariya's role. `reasons` is a non-empty tuple of factual, independently
    verifiable statements grounded in stored facts (milestones reached, trend
    figures crossing a documented threshold) — enforced structurally here, not
    just by convention, so a candidate with no cited reasons cannot be constructed.
    """

    guild_id: int
    member_id: int
    window: CohortWindow
    reasons: tuple[str, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        if type(self.window) is not CohortWindow:
            raise ValueError("window must be a CohortWindow")
        if type(self.reasons) is not tuple:
            raise ValueError("reasons must be a tuple")
        if not self.reasons:
            raise ValueError("reasons must not be empty: every candidate must cite facts")
        for reason in self.reasons:
            _require_text("reason", reason, limit=_MAX_REASON_LENGTH)
        _require_aware("evaluated_at", self.evaluated_at)


@dataclass(frozen=True, slots=True)
class ExclusionEntry:
    """A guild-configured channel excluded from ledger ingestion.

    Enforced at the ingestion boundary, before any storage call — see the design
    doc's Privacy Controls section. This value object only models the configuration
    fact; enforcement is a later task's responsibility.
    """

    guild_id: int
    channel_id: int
    excluded_by: int
    reason: str
    excluded_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("channel_id", self.channel_id)
        _require_positive_id("excluded_by", self.excluded_by)
        _require_text("reason", self.reason, limit=_MAX_REASON_LENGTH)
        _require_aware("excluded_at", self.excluded_at)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """A guild-configurable maximum age for raw ledger rows.

    `max_age_days` bounds how long raw event rows survive before a scheduled sweep
    prunes them; already-computed cohort/milestone aggregates are retained per the
    design doc unless a guild's policy explicitly says otherwise (a later task's
    concern — this value object only models the configured age bound).
    """

    guild_id: int
    max_age_days: int
    updated_by: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        _require_positive_id("updated_by", self.updated_by)
        _require_aware("updated_at", self.updated_at)
