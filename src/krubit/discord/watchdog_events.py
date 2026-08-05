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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlsplit

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

# -- Message-signal thresholds (safety-sensitive — read before changing) ----------
#
# `extract_message_signals` is only ever called for a member with a currently open
# watch window (see `krubit.services.watch_window.WatchWindowService.inspect_message`),
# so, unlike the join signals above, every signal here already carries an implicit
# prior: this member already looked worth a second look. That does NOT mean these
# thresholds should be loose — a false accusation against an already-anxious watched
# member is still a worse failure mode than a slow catch — so every weight/confidence
# pair below is chosen the same way the join signals were: with ONE deliberate,
# explicitly-named exception (below), no single message-signal should clear
# `_SUSPICIOUS_THRESHOLD` (3.0) alone.
#
# - `mass_mentions` (weight 3 @ 0.5 = 1.5 at >= 8 combined user/role mentions; weight 6
#   @ 0.7 = 4.2 at >= 15, and unconditionally at the same weight/confidence for an
#   `@everyone`/`@here` ping): an ordinary message rarely pings more than a handful of
#   people; a burst of mentions is the classic "ping the whole server" spam/raid
#   pattern. **This is the one exception to the "no single signal alone" rule above,
#   deliberately, not by oversight**: the HIGH tier's effective weight (6 * 0.7 = 4.2)
#   exceeds `_SUSPICIOUS_THRESHOLD` (3.0) by itself, mirroring the precedent already
#   set by `extract_join_signals`'s own `account_age` < 1h tier (4.5) and
#   `join_velocity` HIGH tier (4.2) — see that function's module docstring. A message
#   mentioning 15+ users/roles at once, or a bare `@everyone`/`@here` ping, is already
#   maximally disruptive to the guild on its own; requiring a second corroborating
#   signal before treating it as more than "quietly worth watching" would under-react
#   to the single clearest, most self-evident message-level attack pattern this
#   function can observe. The `@everyone`/`@here` case intentionally matches the high
#   tier's weight/confidence regardless of the accompanying explicit-mention count,
#   since a guild-wide ping is already maximally disruptive on its own — counting
#   explicit mentions on top of it would not make it more or less alarming. The
#   ELEVATED tier (1.5) stays well under threshold, so an ordinary handful-of-mentions
#   message never escalates alone; only the HIGH tier and the `@everyone`/`@here` case
#   carry this exception.
# - `malicious_link_shape` (weight 4 @ 0.6 = 2.4): fires on structural URL red flags
#   only — a bare IP-address host, `userinfo@host` credential/redirect tricks (the
#   classic `https://real-site.com@evil.tld/` phishing shape), or a known link-
#   shortener domain (redirect-shortener detection, matching the design doc's
#   wording) — never a fetched blocklist and never content classification of *what*
#   the link claims to be. Kept at a single weight regardless of which structural
#   pattern matched: distinguishing "this is a phishing link" from "this happens to be
#   a shortened link" is exactly the kind of intent judgment this module must not make.
# - `repeated_content` (weight 3 @ 0.5 = 1.5): fires when a single message's own word
#   sequence is dominated by repetition (a "buy now buy now buy now" pattern) — a
#   proxy for keyboard-spam/copy-paste-flood content. Requires both a minimum word
#   count and a repetition ratio so a short casual repeat ("lol lol") never fires; see
#   `_REPEATED_CONTENT_MIN_WORDS`/`_REPEATED_CONTENT_RATIO`. This is intentionally
#   distinct from `WatchWindowService`'s own `repeated_content_near_duplicate` signal,
#   which compares a message against the *same* member's own prior messages within the
#   watch window (a stateful, service-level check this pure function cannot make).
_MASS_MENTIONS_ELEVATED = 8
_MASS_MENTIONS_HIGH = 15
_MASS_MENTIONS_ELEVATED_WEIGHT = 3
_MASS_MENTIONS_ELEVATED_CONFIDENCE = 0.5
_MASS_MENTIONS_HIGH_WEIGHT = 6
_MASS_MENTIONS_HIGH_CONFIDENCE = 0.7

_LINK_SHAPE_WEIGHT = 4
_LINK_SHAPE_CONFIDENCE = 0.6
_URL_PATTERN = re.compile(r"https?://\S+")
# A small, fixed, offline set of well-known redirect-shortener domain *shapes* — never
# fetched, never updated at runtime, matching the design doc's "URL structure and
# redirect-shortener detection, not ... a fetched blocklist" requirement.
_KNOWN_SHORTENER_DOMAINS = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "is.gd",
        "ow.ly",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
        "tiny.cc",
        "rb.gy",
    }
)

_REPEATED_CONTENT_MIN_WORDS = 4
_REPEATED_CONTENT_RATIO = 2.0
_REPEATED_CONTENT_WEIGHT = 3
_REPEATED_CONTENT_CONFIDENCE = 0.5


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


class MessageSubject(Protocol):
    """Structural shape `extract_message_signals` needs from a guild text message.

    Deliberately narrow, matching `JoinSubject`'s convention: only fields already
    present on `discord.Message` are named here, and never full-history/network-
    fetchable fields. `content` is public-guild-channel message text the bot already
    receives via the gateway (never a DM — see the design doc's Non-Negotiable
    Boundaries; the caller, `WatchWindowService.inspect_message`, is responsible for
    only ever being invoked with a guild message in the first place).
    """

    @property
    def content(self) -> str: ...
    @property
    def mentions(self) -> Sequence[object]: ...
    @property
    def role_mentions(self) -> Sequence[object]: ...
    @property
    def mention_everyone(self) -> bool: ...


def extract_message_signals(message: MessageSubject, now: datetime) -> tuple[RiskSignal, ...]:
    """Deterministically extract bounded, named `RiskSignal`s from one guild message.

    Pure: no I/O, no clock reads beyond the supplied `now`, no storage/history lookup
    (that is `WatchWindowService`'s job, after this function returns — it is the only
    caller responsible for ensuring this function only ever runs for a member with a
    currently open watch window). Two calls with equal inputs always return an equal
    result. See the module docstring's "Message-signal thresholds" section for the
    weight/confidence rationale behind each signal below. `now` is currently only used
    for timezone-awareness validation (no signal here is time-based yet); it stays a
    required parameter so a future time-sensitive message signal (e.g. posting-rate
    within a rolling window) can be added without changing this function's signature.
    """
    _require_aware("now", now)

    signals: list[RiskSignal] = []

    mention_signal = _mass_mentions_signal(message)
    if mention_signal is not None:
        signals.append(mention_signal)

    link_signal = _malicious_link_shape_signal(message)
    if link_signal is not None:
        signals.append(link_signal)

    repeated_signal = _repeated_content_signal(message)
    if repeated_signal is not None:
        signals.append(repeated_signal)

    return tuple(signals)


def _mass_mentions_signal(message: MessageSubject) -> RiskSignal | None:
    mention_count = len(message.mentions) + len(message.role_mentions)
    if message.mention_everyone:
        return RiskSignal(
            name="mass_mentions",
            weight=_MASS_MENTIONS_HIGH_WEIGHT,
            detail=(
                f"message pings @everyone/@here (plus {mention_count} explicit "
                "user/role mention(s))"
            ),
            confidence=_MASS_MENTIONS_HIGH_CONFIDENCE,
        )
    if mention_count >= _MASS_MENTIONS_HIGH:
        return RiskSignal(
            name="mass_mentions",
            weight=_MASS_MENTIONS_HIGH_WEIGHT,
            detail=f"message mentions {mention_count} users/roles at once",
            confidence=_MASS_MENTIONS_HIGH_CONFIDENCE,
        )
    if mention_count >= _MASS_MENTIONS_ELEVATED:
        return RiskSignal(
            name="mass_mentions",
            weight=_MASS_MENTIONS_ELEVATED_WEIGHT,
            detail=f"message mentions {mention_count} users/roles at once",
            confidence=_MASS_MENTIONS_ELEVATED_CONFIDENCE,
        )
    return None


def _malicious_link_shape_signal(message: MessageSubject) -> RiskSignal | None:
    for url in _URL_PATTERN.findall(message.content):
        reason = _suspicious_url_reason(url)
        if reason is not None:
            return RiskSignal(
                name="malicious_link_shape",
                weight=_LINK_SHAPE_WEIGHT,
                detail=f"message contains a URL with a {reason}: {url[:200]}",
                confidence=_LINK_SHAPE_CONFIDENCE,
            )
    return None


def _suspicious_url_reason(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname is None:
        return None
    if parsed.username is not None:
        return "credential/redirect trick (userinfo before hostname)"
    try:
        ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        return "bare IP-address host"
    if parsed.hostname.lower() in _KNOWN_SHORTENER_DOMAINS:
        return "known link-shortener domain shape"
    return None


def _repeated_content_signal(message: MessageSubject) -> RiskSignal | None:
    words = message.content.lower().split()
    if len(words) < _REPEATED_CONTENT_MIN_WORDS:
        return None
    distinct = len(set(words))
    if distinct == 0:
        return None
    ratio = len(words) / distinct
    if ratio >= _REPEATED_CONTENT_RATIO:
        return RiskSignal(
            name="repeated_content",
            weight=_REPEATED_CONTENT_WEIGHT,
            detail=(
                f"message repeats the same word(s) within itself "
                f"({len(words)} words, only {distinct} distinct)"
            ),
            confidence=_REPEATED_CONTENT_CONFIDENCE,
        )
    return None
