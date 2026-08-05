"""Guild-scoped raid and spam-wave detection: correlate Entry Sniff output, not
raw Discord events, into `Incident`s.

`RaidDetector` and `SpamWaveDetector` are the third and fourth service-layer
consumers of Task 2's watchdog storage methods (after `EntrySniffService` and
`WatchWindowService`). Per the design doc's "Raid / Spam-Wave / Webhook-Abuse /
Permission-Risk Detection" section:

- **Raid**: "join-velocity spike correlated with join-cluster similarity across
  multiple recent joins."
- **Spam-wave**: "multiple currently-watched (or even `clear`) members posting
  near-duplicate content within a short guild-wide window."

Both detectors are deterministic, evidence-producing only, and never mutate a
member, role, or message — matching every other Phase 3 service. `evaluate` is
side-effect-free on a negative result (returns `None`, writes nothing) and, on a
positive result, calls `record_incident` and `record_sniff_receipt` exactly once,
matching the established pattern from `EntrySniffService`/`WatchWindowService`.

## Why `RaidDetector` reads `entry_sniff_assessments`, not raw join events

Rather than re-deriving join velocity/cluster-similarity from scratch, `RaidDetector`
reads the *already-computed* per-join risk band that `EntrySniffService.assess_join`
produced (via `SQLiteStore.list_recent_entry_sniff_assessments`, the one new query
this task adds — see its docstring in `sqlite.py`). This avoids duplicating
`watchdog_events.extract_join_signals`'s own `join_velocity`/`join_cluster_similarity`
logic and stays consistent with "correlated with join-cluster similarity": a member's
assessment already reflects whatever join-velocity/cluster-similarity signals fired
for their specific join (see `krubit.discord.watchdog_events`'s module docstring), so
counting *how many* recent joins landed at `WATCH` or higher is exactly "join-velocity
spike correlated with join-cluster similarity" restated at the guild level, not a
separate computation of the same thing.

## Threshold design (safety-sensitive — read before changing)

- `_RAID_WINDOW = 10 minutes`: a raid is a burst, not a trend. Ten minutes is long
  enough to catch a coordinated wave that trickles in over a couple of minutes (not
  every hostile actor joins within the same second) but short enough that unrelated
  joins hours apart never accumulate into a false positive.
- `_RAID_ELEVATED_JOIN_THRESHOLD = 8`: eight or more members landing at `WATCH` band
  or higher within the window. `WATCH` is already Entry Sniff's lowest bar for "this
  join looked atypical" (see `krubit.domain.watchdog`'s module docstring — a single
  ordinary signal is enough to leave `CLEAR`), so a genuinely organic new-member wave
  (announcement, viral post, partnership) will land plenty of `CLEAR` joins mixed in
  with a few `WATCH` ones; requiring *eight* elevated-band joins in the same ten
  minutes is a high enough bar that only a real coordinated pattern — not ordinary
  growth variance — clears it. This is deliberately the same order of magnitude as
  `join_velocity`'s own high tier (`>= 10` joins) inside `extract_join_signals`,
  since a raid is, by definition, join-velocity-plus-quality, not velocity alone.

## Spam-wave: in-memory-only correlation cache (mirrors `WatchWindowService`)

Per the design doc, "no standing behavioral log survives a closed, clean watch
window" — `WatchWindowService.inspect_message` already commits to this by keeping its
own single-member message history entirely in process memory, never in
`SQLiteStore` (see that module's docstring). No durable, guild-wide, cross-member
message-content table exists anywhere in this phase's data model, and this task's
own brief says not to add new duplicate tracking. `SpamWaveDetector` therefore keeps
the same discipline one level up: `record_message` appends a bounded, guild-scoped,
in-memory fingerprint (member_id, normalized content, timestamp) that `evaluate`
clusters for near-duplicates across *distinct* members. This cache is fed by whichever
runtime observes messages (Task 7, not yet wired) calling `record_message` per
message — until that wiring exists, `evaluate` correctly and honestly never fires,
matching the design doc's "message-content-dependent signals degrade honestly" stance
for the privileged Message Content intent. `_MESSAGE_CACHE_LIMIT` bounds memory per
guild the same way `WatchWindowService._MESSAGE_HISTORY_LIMIT` bounds it per member.

- `_SPAM_WAVE_WINDOW = 5 minutes`: shorter than the raid window — the design doc calls
  for "a short guild-wide window," and a spam wave (a shared payload blasted by
  several already-present or already-watched accounts) plays out faster than a raid's
  join trickle.
- `_SPAM_WAVE_SIMILARITY = 0.85`: identical to `WatchWindowService`'s own
  `_REPEATED_HISTORY_SIMILARITY`, for the same reason documented there — high enough
  to require near-identical text (a copy-pasted payload with minor per-post noise),
  not merely topically similar messages.
- `_SPAM_WAVE_MEMBER_THRESHOLD = 3`: three or more *distinct* members posting
  near-duplicate content is what makes this a "wave" rather than one member flooding
  (already `WatchWindowService.inspect_message`'s `repeated_content_near_duplicate`
  job, which is explicitly single-member-scoped — see that module's "Cross-member
  isolation" section). Two members echoing the same short phrase happens by
  coincidence in ordinary chat; three independently posting the same near-identical
  payload within five minutes is much harder to explain as coincidence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Final
from uuid import uuid4

from krubit.domain.models import JSONValue
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand, RiskSignal
from krubit.storage.sqlite import SQLiteStore

_RAID_WINDOW: Final[timedelta] = timedelta(minutes=10)
_RAID_ELEVATED_JOIN_THRESHOLD: Final[int] = 8
_RAID_SIGNAL_WEIGHT: Final[int] = 8
_RAID_SIGNAL_CONFIDENCE: Final[float] = 0.85
_RAID_RECOMMENDED_ACTION: Final[str] = (
    "Review the flagged joins in this window and consider a temporary invite pause "
    "or verification-level increase; no automatic action has been taken."
)

_SPAM_WAVE_WINDOW: Final[timedelta] = timedelta(minutes=5)
_SPAM_WAVE_SIMILARITY: Final[float] = 0.85
_SPAM_WAVE_MEMBER_THRESHOLD: Final[int] = 3
_MESSAGE_CACHE_LIMIT: Final[int] = 200
_SPAM_WAVE_SIGNAL_WEIGHT: Final[int] = 7
_SPAM_WAVE_SIGNAL_CONFIDENCE: Final[float] = 0.75
_SPAM_WAVE_RECOMMENDED_ACTION: Final[str] = (
    "Review the flagged messages for this near-duplicate cluster and consider a "
    "temporary slow-mode or channel lock; no automatic action has been taken."
)

# Injected so this task's tests never need Task 6's redaction/storage wiring; the
# default is a placeholder identifier only, not a real evidence packet. A future
# Task 6 caller supplies its own builder (persisting a redacted `EvidencePacket`)
# through the constructor.
EvidencePacketBuilder = Callable[[int, tuple[RiskSignal, ...], datetime], str]


def _default_evidence_builder(
    guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
) -> str:
    del guild_id, signals, now
    return f"evidence:{uuid4().hex}"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class RaidDetector:
    """Fire a `RAID` incident when a guild sees a burst of elevated-band joins."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        window: timedelta = _RAID_WINDOW,
        elevated_join_threshold: int = _RAID_ELEVATED_JOIN_THRESHOLD,
        evidence_builder: EvidencePacketBuilder = _default_evidence_builder,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        if elevated_join_threshold < 1:
            raise ValueError("elevated_join_threshold must be positive")
        self._store = store
        self._window = window
        self._elevated_join_threshold = elevated_join_threshold
        self._evidence_builder = evidence_builder

    async def evaluate(self, guild_id: int, now: datetime) -> Incident | None:
        """Return a `RAID` incident if enough elevated-band joins clustered recently.

        Read-only unless it fires: a negative result touches no storage at all. See
        the module docstring's "Threshold design" section for why `WATCH`-or-higher
        plus an 8-join floor is the organic-growth false-positive guard.
        """
        _require_aware("now", now)
        assessments = await self._store.list_recent_entry_sniff_assessments(
            guild_id, since=now - self._window, until=now
        )
        elevated = tuple(a for a in assessments if a.band is not RiskBand.CLEAR)
        if len(elevated) < self._elevated_join_threshold:
            return None

        signal = RiskSignal(
            name="raid_join_cluster",
            weight=_RAID_SIGNAL_WEIGHT,
            detail=(
                f"{len(elevated)} elevated-risk joins (WATCH band or higher) within "
                f"{int(self._window.total_seconds())} seconds"
            ),
            confidence=_RAID_SIGNAL_CONFIDENCE,
        )
        return await self._record_incident(guild_id, (signal,), now)

    async def _record_incident(
        self, guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
    ) -> Incident:
        evidence_packet_id = self._evidence_builder(guild_id, signals, now)
        incident = Incident(
            guild_id=guild_id,
            incident_id=f"raid:{uuid4().hex}",
            kind=IncidentKind.RAID,
            band=RiskBand.INCIDENT,
            opened_at=now,
            evidence_packet_id=evidence_packet_id,
            recommended_action=_RAID_RECOMMENDED_ACTION,
            acknowledged_by=None,
        )
        saved = await self._store.record_incident(incident)
        detail: dict[str, JSONValue] = {
            "kind": saved.kind.value,
            "signal_names": [signal.name for signal in signals],
        }
        await self._store.record_sniff_receipt(
            guild_id=saved.guild_id,
            receipt_id=f"incident:{saved.incident_id}",
            member_id=None,
            action="incident_recorded",
            detail=detail,
            created_at=now,
        )
        return saved


class SpamWaveDetector:
    """Fire a `SPAM_WAVE` incident when >= 3 distinct members post near-duplicate
    content within a short guild-wide window.

    See the module docstring's "Spam-wave: in-memory-only correlation cache" section
    for why the message cache lives in this instance rather than `SQLiteStore`, and
    why `evaluate` correctly never fires until some runtime calls `record_message`.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        window: timedelta = _SPAM_WAVE_WINDOW,
        similarity_threshold: float = _SPAM_WAVE_SIMILARITY,
        member_threshold: int = _SPAM_WAVE_MEMBER_THRESHOLD,
        evidence_builder: EvidencePacketBuilder = _default_evidence_builder,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        if not (0.0 <= similarity_threshold <= 1.0):
            raise ValueError("similarity_threshold must be between 0.0 and 1.0")
        if member_threshold < 2:
            raise ValueError("member_threshold must be at least 2")
        self._store = store
        self._window = window
        self._similarity_threshold = similarity_threshold
        self._member_threshold = member_threshold
        self._evidence_builder = evidence_builder
        self._messages: dict[int, deque[tuple[int, str, datetime]]] = defaultdict(
            lambda: deque(maxlen=_MESSAGE_CACHE_LIMIT)
        )

    def record_message(
        self, guild_id: int, member_id: int, content: str, now: datetime
    ) -> None:
        """Remember one member's message fingerprint for later cross-member correlation.

        In-memory only, bounded, and never written to `SQLiteStore` — see the module
        docstring. A blank/whitespace-only message is not remembered (nothing to
        compare).
        """
        _require_aware("now", now)
        normalized = content.strip().lower()
        if not normalized:
            return
        self._messages[guild_id].append((member_id, normalized, now))

    async def evaluate(self, guild_id: int, now: datetime) -> Incident | None:
        """Return a `SPAM_WAVE` incident if a near-duplicate cluster spans enough
        distinct members within the trailing window, or `None` (writing nothing) if
        the cache is empty (never fed) or no cluster clears the member threshold.
        """
        _require_aware("now", now)
        cutoff = now - self._window
        cache = self._messages.get(guild_id)
        if not cache:
            return None

        recent = [entry for entry in cache if entry[2] >= cutoff]
        cluster = self._largest_near_duplicate_cluster(recent)
        if cluster is None:
            return None
        member_ids, representative = cluster

        signal = RiskSignal(
            name="spam_wave_near_duplicate",
            weight=_SPAM_WAVE_SIGNAL_WEIGHT,
            detail=(
                f"{len(member_ids)} distinct members posted near-duplicate content "
                f"(>= {self._similarity_threshold:.0%} similarity) within "
                f"{int(self._window.total_seconds())} seconds: {representative[:80]!r}"
            ),
            confidence=_SPAM_WAVE_SIGNAL_CONFIDENCE,
        )
        return await self._record_incident(guild_id, (signal,), now)

    def _largest_near_duplicate_cluster(
        self, entries: list[tuple[int, str, datetime]]
    ) -> tuple[frozenset[int], str] | None:
        """Greedily group `entries` by near-duplicate content and return the first
        cluster (in chronological order of its representative) whose distinct member
        count clears `_member_threshold`, or `None` if none does.

        Deterministic for a fixed input: clusters are built in the order `entries`
        were appended (chronological, since `record_message` only appends), and each
        message joins the first existing cluster it is similar enough to rather than
        the "best" one — this keeps the algorithm O(n * clusters) and reproducible.
        """
        clusters: list[tuple[str, dict[int, str]]] = []
        for member_id, normalized, _occurred_at in entries:
            joined = False
            for representative, members in clusters:
                if SequenceMatcher(None, normalized, representative).ratio() >= (
                    self._similarity_threshold
                ):
                    members[member_id] = normalized
                    joined = True
                    break
            if not joined:
                clusters.append((normalized, {member_id: normalized}))

        for representative, members in clusters:
            if len(members) >= self._member_threshold:
                return frozenset(members), representative
        return None

    async def _record_incident(
        self, guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
    ) -> Incident:
        evidence_packet_id = self._evidence_builder(guild_id, signals, now)
        incident = Incident(
            guild_id=guild_id,
            incident_id=f"spam-wave:{uuid4().hex}",
            kind=IncidentKind.SPAM_WAVE,
            band=RiskBand.INCIDENT,
            opened_at=now,
            evidence_packet_id=evidence_packet_id,
            recommended_action=_SPAM_WAVE_RECOMMENDED_ACTION,
            acknowledged_by=None,
        )
        saved = await self._store.record_incident(incident)
        detail: dict[str, JSONValue] = {
            "kind": saved.kind.value,
            "signal_names": [signal.name for signal in signals],
        }
        await self._store.record_sniff_receipt(
            guild_id=saved.guild_id,
            receipt_id=f"incident:{saved.incident_id}",
            member_id=None,
            action="incident_recorded",
            detail=detail,
            created_at=now,
        )
        return saved
