"""Entry Sniff join-signal extraction: pure Discord-object-to-`RiskSignal` mapping.

Phase 3's join-time detector, `extract_join_signals`, mirrors the framework-
independent, no-I/O convention already established by
`krubit.discord.live_signals.extract_twitch_observation`: it takes already-fetched
Discord-shaped objects (never fetches anything itself) and a caller-supplied clock
reading, and returns a tuple of `krubit.domain.watchdog.RiskSignal`. It never touches
the network, never reads a guild's allow/block list (that is staff-configured storage,
not a Discord-object field, and is applied by `krubit.services.entry_sniff.
EntrySniffService` after this function returns), and never infers anything about a
member's identity, mental state, or protected traits — only the bounded, named signals
below, each read from a field Discord's Gateway/API already exposes to an installed
bot.

## Signal weight design (safety-sensitive — read before changing)

Every weight is a fixed integer in `RiskSignal`'s `[1, 10]` range; every confidence
reflects how certain the *detector* is that the observation is real, not how alarming
it is (see `krubit.domain.watchdog`'s module docstring for how weight and confidence
combine into `evaluate_risk_band`'s effective weight). One signal alone should almost
never cross `_SUSPICIOUS_THRESHOLD` (3.0) on its own — new accounts and default
avatars happen to genuine new members constantly — so every per-signal effective
weight below is deliberately kept under ~3.0, requiring at least two independent
signals to corroborate before a join looks more than "quietly worth watching":

- `account_age` (weight 5 @ 0.9 = 4.5 when < 1h old; weight 3 @ 0.7 = 2.1 when < 24h;
  weight 1 @ 0.3 = 0.3 when < 7d; no signal beyond 7 days old). An account created
  minutes before joining is the single strongest, most commonly-seen raid indicator,
  so it is the only individual join signal allowed to clear the SUSPICIOUS threshold
  by itself — but only at the < 1h tier, and only just past it (4.5), never alone
  reaching INCIDENT (6.0). The 24h and 7-day tiers are progressively weaker and more
  uncertain, since "created a week ago" describes a large fraction of harmless casual
  Discord users too.
- `default_avatar` (weight 2 @ 0.6 = 1.2): common among genuine new members who
  haven't customized their profile yet; kept deliberately weak on its own, useful
  mainly as corroboration alongside `account_age`.
- `garbage_username` (weight 2 @ 0.5 = 1.0): fires only on the narrow, high-precision
  pattern of an all-digit username (Discord's unique-username system never assigns
  this to a human-chosen handle). Deliberately narrow rather than a broad heuristic —
  a false accusation from an overzealous pattern is a worse failure mode than missing
  a few genuinely garbage names.
- `bot_or_system_account` (weight 2 @ 0.4 = 0.8): `on_member_join` also fires when a
  bot application is added to a guild via OAuth; this is usually a deliberate staff
  action, so the weight and confidence both stay low — it exists only so a review of a
  join burst can see that some entries were bot adds, not to accuse the bot itself.
- `rules_screening_pending` (weight 1 @ 0.3 = 0.3): `Member.pending` means the member
  has not yet completed Discord's own Membership Screening — an expected, temporary
  state for legitimate joiners, not itself risky, so this is the lowest-weight signal
  in the set and exists purely as corroborating context, never a driver on its own.
- `join_velocity` (weight 3 @ 0.5 = 1.5 at >= 5 joins in the caller's window; weight 6
  @ 0.7 = 4.2 at >= 10): a short-window join burst is a classic raid precursor, so the
  high tier alone can clear SUSPICIOUS, matching `account_age`'s < 1h tier, but the
  caller (`EntrySniffService`) is responsible for windowing `recent_joins` to a short,
  bounded interval before calling this function — this function trusts that the tuple
  it receives is already scoped to "recent."
- `join_cluster_similarity` (weight 4 @ 0.6 = 2.4 at >= 5 similar recent joins; weight
  6 @ 0.75 = 4.5 at >= 8): correlates `recent_joins` against *this* member's own
  account-age and avatar-presence pattern — coordinated joins sharing both traits are
  much harder to explain as coincidence than a raw join count alone, so this signal is
  weighted above plain `join_velocity` at the same count.

No signal here reaches `_INCIDENT_THRESHOLD` (6.0) alone; corroboration across two or
more independent signals is required to reach `SUSPICIOUS`, and reaching `INCIDENT`
from join signals alone requires several of them stacking (for example a < 1h account
plus a high-tier join-cluster match). The one exception in the whole Entry Sniff
pipeline is the guild's own block list, which `EntrySniffService` — not this module —
attaches as a maximum-weight, maximum-confidence signal, since an explicit staff block
is a certain fact, not an inferred pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from krubit.domain.watchdog import RiskSignal

_ACCOUNT_AGE_VERY_NEW = timedelta(hours=1)
_ACCOUNT_AGE_NEW = timedelta(hours=24)
_ACCOUNT_AGE_YOUNG = timedelta(days=7)

_JOIN_VELOCITY_ELEVATED = 5
_JOIN_VELOCITY_HIGH = 10

_CLUSTER_AGE_TOLERANCE = timedelta(hours=2)
_CLUSTER_SIMILARITY_ELEVATED = 5
_CLUSTER_SIMILARITY_HIGH = 8

_GARBAGE_USERNAME_PATTERN = re.compile(r"^\d+$")


class JoinSubject(Protocol):
    """Structural shape `extract_join_signals` needs from a joining member.

    Deliberately narrow: only fields already present on `discord.Member` (and,
    equally, on any lightweight recent-join snapshot a caller constructs) are named
    here. No network-fetchable or derived field belongs in this protocol. Every
    attribute is a read-only property (rather than a plain annotation) so this
    protocol stays covariant — `discord.Member.avatar` is typed `Asset | None`, a
    subtype of this protocol's `object | None`, and an invariant (mutable-attribute)
    protocol would reject that assignment.
    """

    @property
    def id(self) -> int: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def avatar(self) -> object | None: ...
    @property
    def bot(self) -> bool: ...
    @property
    def system(self) -> bool: ...
    @property
    def pending(self) -> bool: ...
    @property
    def name(self) -> str: ...


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def extract_join_signals(
    member: JoinSubject, recent_joins: tuple[JoinSubject, ...], now: datetime
) -> tuple[RiskSignal, ...]:
    """Deterministically extract bounded, named `RiskSignal`s from one member join.

    Pure: no I/O, no clock reads beyond the supplied `now`, no allow/block-list
    consultation (that is `EntrySniffService`'s job, after this function returns). Two
    calls with equal inputs always return an equal result.
    """
    _require_aware("now", now)
    _require_aware("member.created_at", member.created_at)

    signals: list[RiskSignal] = []

    account_age_signal = _account_age_signal(member, now)
    if account_age_signal is not None:
        signals.append(account_age_signal)

    if member.avatar is None:
        signals.append(
            RiskSignal(
                name="default_avatar",
                weight=2,
                detail="member has never set a custom avatar",
                confidence=0.6,
            )
        )

    if _GARBAGE_USERNAME_PATTERN.match(member.name):
        signals.append(
            RiskSignal(
                name="garbage_username",
                weight=2,
                detail=f"username matches an all-digit auto-generated pattern: {member.name}",
                confidence=0.5,
            )
        )

    if member.bot or member.system:
        signals.append(
            RiskSignal(
                name="bot_or_system_account",
                weight=2,
                detail="member is a bot or system account",
                confidence=0.4,
            )
        )

    if member.pending:
        signals.append(
            RiskSignal(
                name="rules_screening_pending",
                weight=1,
                detail="member has not yet completed Rules Screening",
                confidence=0.3,
            )
        )

    velocity_signal = _join_velocity_signal(recent_joins)
    if velocity_signal is not None:
        signals.append(velocity_signal)

    cluster_signal = _join_cluster_similarity_signal(member, recent_joins, now)
    if cluster_signal is not None:
        signals.append(cluster_signal)

    return tuple(signals)


def _account_age_signal(member: JoinSubject, now: datetime) -> RiskSignal | None:
    age = now - member.created_at
    if age < timedelta(0):
        age = timedelta(0)
    if age <= _ACCOUNT_AGE_VERY_NEW:
        return RiskSignal(
            name="account_age",
            weight=5,
            detail=f"account created {age} before joining (< 1 hour)",
            confidence=0.9,
        )
    if age <= _ACCOUNT_AGE_NEW:
        return RiskSignal(
            name="account_age",
            weight=3,
            detail=f"account created {age} before joining (< 24 hours)",
            confidence=0.7,
        )
    if age <= _ACCOUNT_AGE_YOUNG:
        return RiskSignal(
            name="account_age",
            weight=1,
            detail=f"account created {age} before joining (< 7 days)",
            confidence=0.3,
        )
    return None


def _join_velocity_signal(recent_joins: tuple[JoinSubject, ...]) -> RiskSignal | None:
    count = len(recent_joins)
    if count >= _JOIN_VELOCITY_HIGH:
        return RiskSignal(
            name="join_velocity",
            weight=6,
            detail=f"{count} other members joined in the same recent window",
            confidence=0.7,
        )
    if count >= _JOIN_VELOCITY_ELEVATED:
        return RiskSignal(
            name="join_velocity",
            weight=3,
            detail=f"{count} other members joined in the same recent window",
            confidence=0.5,
        )
    return None


def _join_cluster_similarity_signal(
    member: JoinSubject, recent_joins: tuple[JoinSubject, ...], now: datetime
) -> RiskSignal | None:
    member_age = now - member.created_at
    member_has_avatar = member.avatar is not None
    similar = 0
    for other in recent_joins:
        other_age = now - other.created_at
        ages_match = abs(other_age - member_age) <= _CLUSTER_AGE_TOLERANCE
        avatars_match = (other.avatar is not None) == member_has_avatar
        if ages_match and avatars_match:
            similar += 1

    if similar >= _CLUSTER_SIMILARITY_HIGH:
        return RiskSignal(
            name="join_cluster_similarity",
            weight=6,
            detail=(
                f"{similar} recently-joined members share this member's account-age "
                "and avatar-presence pattern"
            ),
            confidence=0.75,
        )
    if similar >= _CLUSTER_SIMILARITY_ELEVATED:
        return RiskSignal(
            name="join_cluster_similarity",
            weight=4,
            detail=(
                f"{similar} recently-joined members share this member's account-age "
                "and avatar-presence pattern"
            ),
            confidence=0.6,
        )
    return None


@dataclass(frozen=True, slots=True)
class JoinFingerprint:
    """A lightweight, durable-free snapshot of one recent join's `JoinSubject` fields.

    `EntrySniffService` builds these from `discord.Member` objects still present in a
    guild's member cache (never from storage — Task 3 introduces no new persistence
    for join history) so that `recent_joins` can outlive the moment a member's own
    `discord.Member` reference might change shape, without holding a live Discord
    object longer than necessary.
    """

    id: int
    created_at: datetime
    avatar: object | None
    bot: bool
    system: bool
    pending: bool
    name: str

    @classmethod
    def from_member(cls, member: JoinSubject) -> JoinFingerprint:
        return cls(
            id=member.id,
            created_at=member.created_at,
            avatar=member.avatar,
            bot=member.bot,
            system=member.system,
            pending=member.pending,
            name=member.name,
        )
