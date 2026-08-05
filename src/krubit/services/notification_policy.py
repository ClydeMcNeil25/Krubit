"""Delivery policy: quiet hours, mention budgets, and template validation.

`NotificationPolicy.evaluate` and `decide_mention` are pure, deterministic functions of
a `ContentEvent`, the instant `at`, and the caller-supplied quiet-hours/mention-budget
*state* — they perform no I/O and consume no budget themselves. They exist so the
decision rules can be unit-tested deterministically without a database.

Production delivery code must NOT call `evaluate`/`decide_mention` and then separately
call `SQLiteStore.claim_mention_budget` — reading a budget snapshot and spending it are
two different moments, and two concurrent callers can each read "available" before
either one claims, both walking away believing they own the last `@everyone`/role
mention. Instead, use `NotificationPolicy.evaluate_and_claim`, which performs the
decision AND the atomic claim as one step and downgrades to `MentionKind.NONE` whenever
the claim loses the race — there is no two-step sequence for a caller to get wrong.

Quiet hours are evaluated in the guild's local timezone using `zoneinfo.ZoneInfo`, so
`evaluate` gets the DST-correct wall-clock answer for free: the window is half-open
`[start, end)` and may wrap past midnight (for example `22:00`-`07:00`).

`NotificationTemplate` carries only bounded, non-mention display text. The delivery
POLICY — never template text — supplies every allowed mention; `validate_template`
enforces that boundary by rejecting any Discord mention syntax and any placeholder
outside the small allowed set.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from krubit.domain.creator_signals import ContentEvent, ContentKind

MentionClaim = Callable[[], Awaitable[bool]]
"""An awaitable callback that atomically attempts to spend one mention-budget unit.

Expected to wrap a single, already-bound call to `SQLiteStore.claim_mention_budget`
(guild, budget kind, and period key fixed by the caller) and return whether this call
won the claim. See `NotificationPolicy.evaluate_and_claim`.
"""

_MAX_HEADLINE_LENGTH = 256
_MAX_FOOTER_LENGTH = 256
_MAX_ACCENT_COLOR = 0xFFFFFF

_ALLOWED_PLACEHOLDERS = frozenset({"creator", "platform", "title", "content_type", "url"})
_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")
_MENTION_PATTERN = re.compile(r"@everyone|@here|<@[!&]?\d+>", re.IGNORECASE)


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


class DeliveryDisposition(StrEnum):
    """Whether a decided delivery should go out now or wait."""

    DELIVER = "deliver"
    QUEUE = "queue"


class MentionKind(StrEnum):
    """Which mention, if any, accompanies a delivery."""

    NONE = "none"
    EVERYONE = "everyone"
    ROLE = "role"


@dataclass(frozen=True, slots=True)
class MentionDecision:
    """The mention outcome for one delivery, independent of disposition.

    `consumed` records whether this decision spent one unit of the relevant mention
    budget — `False` for `NONE` (nothing to spend) and for a budget-exhausted
    suppression (nothing was left to spend).
    """

    kind: MentionKind
    role_id: int | None
    consumed: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.kind) is not MentionKind:
            raise ValueError("kind must be a MentionKind")
        if self.kind is MentionKind.ROLE and self.role_id is None:
            raise ValueError("a role mention requires role_id")
        if self.kind is not MentionKind.ROLE and self.role_id is not None:
            raise ValueError("only a role mention carries role_id")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """The outcome of evaluating one `ContentEvent` against a `NotificationPolicy`."""

    disposition: DeliveryDisposition
    mention: MentionKind
    mention_role_id: int | None = None
    release_at: datetime | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.disposition) is not DeliveryDisposition:
            raise ValueError("disposition must be a DeliveryDisposition")
        if type(self.mention) is not MentionKind:
            raise ValueError("mention must be a MentionKind")
        if self.mention is MentionKind.ROLE and self.mention_role_id is None:
            raise ValueError("a role mention requires mention_role_id")
        if self.mention is not MentionKind.ROLE and self.mention_role_id is not None:
            raise ValueError("only a role mention carries mention_role_id")
        if self.disposition is DeliveryDisposition.QUEUE and self.release_at is None:
            raise ValueError("a queued decision must carry release_at")
        if self.disposition is DeliveryDisposition.DELIVER and self.release_at is not None:
            raise ValueError("a deliver decision must not carry release_at")
        if self.release_at is not None and (
            self.release_at.tzinfo is None or self.release_at.utcoffset() is None
        ):
            raise ValueError("release_at must include a timezone")


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    """Whether two content candidates should be merged into one delivery card."""

    merge: bool
    correlation_group: str | None
    reason: str

    def __post_init__(self) -> None:
        if type(self.merge) is not bool:
            raise ValueError("merge must be a bool")
        if self.merge and self.correlation_group is None:
            raise ValueError("a merged decision requires a correlation_group")
        if not self.merge and self.correlation_group is not None:
            raise ValueError("a non-merged decision must not carry a correlation_group")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    """Bounded, mention-free display text. Structural bounds only — see `validate_template`
    for placeholder-allowlist and mention-syntax enforcement."""

    headline: str
    footer: str
    accent_color: int

    def __post_init__(self) -> None:
        _require_text("headline", self.headline, limit=_MAX_HEADLINE_LENGTH)
        _require_text("footer", self.footer, limit=_MAX_FOOTER_LENGTH)
        if type(self.accent_color) is not int:
            raise ValueError("accent_color must be an int")
        if not 0 <= self.accent_color <= _MAX_ACCENT_COLOR:
            raise ValueError(f"accent_color must fit in 24 bits (0..{_MAX_ACCENT_COLOR:#08x})")


def validate_template(template: NotificationTemplate) -> NotificationTemplate:
    """Reject any Discord mention syntax and any placeholder outside the allowed set.

    The delivery policy — never template text — supplies every allowed mention, so any
    of `@everyone`, `@here`, or a raw `<@id>`/`<@!id>`/`<@&id>` mention token anywhere in
    `headline` or `footer` is rejected, in any case. Only `{creator}`, `{platform}`,
    `{title}`, `{content_type}`, and `{url}` placeholders are permitted.
    """
    for field_name, text in (("headline", template.headline), ("footer", template.footer)):
        if _MENTION_PATTERN.search(text):
            raise ValueError(
                f"{field_name} must not contain mention syntax: mentions are controlled "
                "by notification policy, not template text"
            )
        for match in _PLACEHOLDER_PATTERN.finditer(text):
            placeholder = match.group(1)
            if placeholder not in _ALLOWED_PLACEHOLDERS:
                raise ValueError(
                    f"{field_name} contains unsupported placeholder '{{{placeholder}}}'; "
                    f"allowed placeholders are {sorted(_ALLOWED_PLACEHOLDERS)}"
                )
    return template


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A guild's quiet window in its own local timezone, half-open `[start, end)`.

    `start == end` is rejected as ambiguous (it could mean "never quiet" or "always
    quiet" and neither should be inferred silently) rather than guessed at.
    """

    start: time
    end: time
    zone: ZoneInfo

    def __post_init__(self) -> None:
        if type(self.zone) is not ZoneInfo:
            raise ValueError("zone must be a ZoneInfo")
        if self.start == self.end:
            raise ValueError("start and end must differ")

    def contains(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must include a timezone")
        local_time = at.astimezone(self.zone).time()
        if self.start < self.end:
            return self.start <= local_time < self.end
        # Wraps past midnight, e.g. 22:00-07:00.
        return local_time >= self.start or local_time < self.end

    def release_at(self, at: datetime) -> datetime:
        """The next instant `end` occurs in local wall-clock time at or after `at`.

        Computed by re-stamping the local wall clock and letting `ZoneInfo` resolve the
        correct UTC offset for that wall time, so this stays correct across a DST
        transition that falls between `at` and the release time.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must include a timezone")
        local = at.astimezone(self.zone)
        candidate = local.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0, fold=0
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate


@dataclass(frozen=True, slots=True)
class MentionBudgetState:
    """A caller-supplied snapshot of how much of a mention budget is already spent.

    `limit=None` means unlimited (never suppress on budget grounds).
    """

    limit: int | None
    consumed: int = 0

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must not be negative")
        if self.consumed < 0:
            raise ValueError("consumed must not be negative")

    @property
    def available(self) -> bool:
        return self.limit is None or self.consumed < self.limit


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """A guild/route's resolved delivery policy for one evaluation.

    `social_mention_role_id` is the approved role mention for social routes (at most
    one), resolved by the caller from the account's `CreatorRoute` before evaluation —
    `evaluate` itself performs no route lookup.
    """

    quiet_hours: QuietHours | None
    live_everyone_budget: MentionBudgetState
    social_role_budget: MentionBudgetState
    social_mention_role_id: int | None = None
    live_bypass_quiet_hours: bool = True

    def evaluate(self, event: ContentEvent, at: datetime) -> DeliveryDecision:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must include a timezone")
        is_live = event.content_kind is ContentKind.LIVE
        in_quiet_hours = self.quiet_hours is not None and self.quiet_hours.contains(at)

        if in_quiet_hours and not (is_live and self.live_bypass_quiet_hours):
            assert self.quiet_hours is not None  # narrows for type-checking
            return DeliveryDecision(
                disposition=DeliveryDisposition.QUEUE,
                mention=MentionKind.NONE,
                release_at=self.quiet_hours.release_at(at),
                reason="queued during quiet hours",
            )

        mention_decision = self.decide_mention(event)
        return DeliveryDecision(
            disposition=DeliveryDisposition.DELIVER,
            mention=mention_decision.kind,
            mention_role_id=mention_decision.role_id,
            reason=mention_decision.reason,
        )

    async def evaluate_and_claim(
        self, event: ContentEvent, at: datetime, *, claim_mention: MentionClaim
    ) -> DeliveryDecision:
        """The safe, atomicity-enforcing entry point for real delivery decisions.

        Identical to `evaluate`, except that whenever the tentative decision would
        consume a mention budget (an `EVERYONE` or `ROLE` mention), it calls
        `claim_mention` — expected to perform the matching
        `SQLiteStore.claim_mention_budget` call — and only returns that mention if the
        claim actually succeeds. If the claim loses the race (a concurrent caller
        already spent the last unit), the decision is downgraded to `MentionKind.NONE`
        with `mention_role_id` cleared; disposition and delivery are unaffected — a
        suppressed mention never blocks or delays the underlying content delivery.

        This is the method production delivery code should call. `evaluate` and
        `decide_mention` remain available for deterministic, I/O-free unit testing of
        the decision rules themselves.
        """
        tentative = self.evaluate(event, at)
        if tentative.mention is MentionKind.NONE:
            return tentative
        won = await claim_mention()
        if won:
            return tentative
        return replace(
            tentative,
            mention=MentionKind.NONE,
            mention_role_id=None,
            reason=f"{tentative.reason} (budget claim lost to a concurrent delivery)",
        )

    def decide_mention(self, event: ContentEvent) -> MentionDecision:
        """Decide the mention for a delivery that is going out now, from a budget snapshot.

        Separated from `evaluate` so the live/social mention rules can be reasoned
        about and tested independently of quiet-hours timing. This does NOT perform
        the atomic budget claim — `consumed=True` only marks that this decision
        *would* spend one unit, not that it has. Do not use this method's output
        directly to decide what mention actually ships; use `evaluate_and_claim`.
        """
        if event.content_kind is ContentKind.LIVE:
            budget = self.live_everyone_budget
            if budget.available:
                return MentionDecision(
                    kind=MentionKind.EVERYONE,
                    role_id=None,
                    consumed=True,
                    reason="live default everyone mention",
                )
            return MentionDecision(
                kind=MentionKind.NONE,
                role_id=None,
                consumed=False,
                reason="live everyone mention budget exhausted",
            )

        if self.social_mention_role_id is None:
            return MentionDecision(
                kind=MentionKind.NONE,
                role_id=None,
                consumed=False,
                reason="no social mention role configured",
            )
        budget = self.social_role_budget
        if budget.available:
            return MentionDecision(
                kind=MentionKind.ROLE,
                role_id=self.social_mention_role_id,
                consumed=True,
                reason="social approved role mention",
            )
        return MentionDecision(
            kind=MentionKind.NONE,
            role_id=None,
            consumed=False,
            reason="social role mention budget exhausted",
        )
