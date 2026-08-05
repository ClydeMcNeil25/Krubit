"""Guild-scoped webhook-abuse and permission-risk detection: correlate Phase 1's
existing guild-event history (never re-tracked here) against Task 2's watch-window
and Entry Sniff state.

`WebhookAbuseDetector` and `PermissionRiskDetector` are the fifth and sixth
service-layer consumers of Task 2's watchdog storage methods. Per this task's brief,
neither detector duplicates any tracking already in `sqlite.py`: both read
`SQLiteStore.list_events` (Phase 1's existing `guild_events` table, populated by
`KrubitBot.on_webhooks_update`/`on_guild_role_create`/`on_guild_role_update`/
`on_guild_role_delete`/`on_member_update` via `_ingest_change`/`guild_event()` — see
`src/krubit/discord/bot.py`), then correlate that against Task 2/4's `watch_windows`
and `entry_sniff_assessments`. Neither detector mutates a member, role, or message;
both are read-only on a negative result and, on a positive result, call
`record_incident` and `record_sniff_receipt` exactly once, matching
`RaidDetector`/`SpamWaveDetector`'s established pattern in
`krubit.services.raid_detection`.

## What Phase 1's `webhooks_updated` event actually contains (and doesn't)

`on_webhooks_update(channel)` is Discord's own gateway event: it fires once per
webhook *configuration change* in a channel (created, edited, or deleted), but the
payload the bot receives from discord.py at that callback is only the channel, not
which specific webhook changed or what changed about it — `KrubitBot._ingest_change`
records `entity_id=channel.id`, `after={"channel": channel.name}` (see
`on_webhooks_update` in `bot.py`). There is no gateway event, and therefore no
`guild_events` row, for an actual webhook *message post* — Discord does not notify a
bot's gateway connection when a webhook posts a message at all, and this task's brief
is explicit that a new per-message webhook-send tracking table must not be added here
("do not duplicate that tracking, only correlate it"). So "an existing webhook
posting at an anomalous rate," read literally as message-send frequency, is not a
question this system can answer without new tracking outside this task's scope.

`WebhookAbuseDetector` therefore treats the one durable signal Phase 1 already
captures — a burst of `webhooks_updated` *configuration-change* events for the same
guild in a short window — as the anomalous-rate proxy. This is a faithful, non-
duplicating reading of "correlated against Krubit's own webhook change history" (the
design doc's own words): legitimate webhook setup is normally a single one-off staff
action (add an integration, done), so several config-change events landing in the
same short window is itself the anomaly worth a staff notification, independent of
message content this system never observes.

## Why this detector counts distinct channels, not raw event rows

`guild_event()`'s `event_id` is a deterministic hash over `{guild_id, event_type,
payload}` — deliberately *excluding* `occurred_at`, so a replayed gateway event is
idempotent (see that function's docstring). `on_webhooks_update(channel)`'s payload is
always exactly `{"channel": channel.name}` — Discord's gateway event carries no
distinguishing detail about *which* webhook changed or *how*. The practical
consequence: if the same channel's webhook configuration changes two or three times
in quick succession — the single most likely real burst pattern — every one of those
events hashes identically and `SQLiteStore.accept_event`'s `INSERT OR IGNORE` keeps
only the first; the repeats are silently dropped before this detector ever runs.
Counting raw stored rows would therefore systematically *undercount* exactly the
pattern this detector exists to catch. Counting **distinct affected channels**
(`entity_id`) instead sidesteps this collision entirely — a genuinely new channel_id
always changes the hash regardless of payload sameness — and is arguably the more
meaningful signal anyway: an attacker touching several channels' webhook
configuration in a short window is broader and more alarming than one channel's
config merely being edited a couple of times.

## The distinct-channel signal alone misses same-channel repeated abuse — fixed below

A code review of this task caught a real gap the first version of this module left
open: the distinct-channel signal above answers "how many different channels were
touched," never "how many times was *this one* channel touched" — and the single most
realistic webhook-abuse shape (an attacker or compromised token repeatedly
create/edit/delete-cycling a webhook in *one* channel to evade takedown) collapses
to exactly one distinguishable `entity_id`, so `affected_channels` never grows past
`{that one channel}` no matter how many times it recurs. Worse, this can never be
fixed by reading `guild_events` more cleverly: the second and third same-payload
`webhooks_updated` events for that channel are never even written to storage — see
"Why this detector counts distinct channels, not raw event rows" above —
`accept_event`'s `INSERT OR IGNORE` silently drops them *before* this detector's next
`evaluate()` call ever runs. No read-side query, however clever, can recover
information that was never persisted.

Two fixes were considered:

1. **Change `guild_event()`'s hash to include `occurred_at`.** Rejected: in
   production, `occurred_at` is `datetime.now(UTC)` read fresh at every
   `_ingest_change` call (see `bot.py`), so a genuine Discord gateway redelivery of
   the *same* logical event almost never carries an identical timestamp. The current
   exclusion of `occurred_at` from the hash is exactly what lets real redeliveries
   dedupe today; including it would silently disable replay-idempotency for every
   event type in the app (member joins, role/channel/automod changes, everything
   `guild_event()` produces), not just this one detector's input — a blast radius far
   larger than this task's fix, and outside this task's own file list
   (`events.py`/`models.py`) besides.
2. **Observe the raw event before it reaches storage, the same way
   `SpamWaveDetector` observes message content before any storage layer exists for
   it** (see `krubit.services.raid_detection`'s module docstring). `record_webhook_
   event(guild_id, channel_id, now)` is a small, bounded, in-memory-only, per-guild
   cache — structurally identical to `SpamWaveDetector._messages` — fed by whichever
   runtime handles the raw `on_webhooks_update` gateway callback, *before* that
   callback's event reaches `accept_event`'s dedup. Because this cache never goes
   through `guild_events`, it is immune to the hash-collision collapse by
   construction, and a burst of same-channel calls is counted exactly. This is
   additive to the existing durable distinct-channel signal, not a replacement: the
   durable signal still independently catches an attacker spreading across several
   channels, using only data already in storage with no wiring required; the
   in-memory signal catches the single-channel case the durable signal structurally
   cannot see. `evaluate` fires if *either* signal clears its threshold. Like
   `SpamWaveDetector`, no runtime calls `record_webhook_event` yet (that wiring is
   Task 7's job, matching the design doc's "message-content-dependent signals degrade
   honestly" precedent extended here to this same-channel-burst signal) — until then
   `evaluate`'s in-memory path is correctly and honestly inert, while its
   durable-storage path (distinct channels) works today.

## Threshold design (safety-sensitive — read before changing)

- `_WEBHOOK_ABUSE_WINDOW = 10 minutes`, `_WEBHOOK_ABUSE_CHANNEL_THRESHOLD = 3`: three
  or more *distinct channels* with a `webhooks_updated` event for one guild within ten
  minutes. One or two channels touched (e.g. a staff member setting up a single new
  integration) is ordinary configuration churn; three-plus distinct channels in a
  tight window is consistent with an attacker probing/creating/reconfiguring webhooks
  across the guild for exfiltration, not routine setup.
- `_WEBHOOK_SAME_CHANNEL_THRESHOLD = 3`: three or more observed `record_webhook_event`
  calls for the *same* channel within the same ten-minute window. Kept equal to the
  distinct-channel threshold for the same reasoning (one or two is ordinary editing;
  three-plus in a tight window is not) — see "The distinct-channel signal alone
  misses same-channel repeated abuse" above for why this second, independent signal
  exists at all.
- `_ROLE_GRANT_LOOKBACK = 30 minutes`: how far back `PermissionRiskDetector` looks for
  a `member_roles_updated` event that added an elevated-permission role. Thirty
  minutes is short enough that the grant is still "recent" relative to `now` (this is
  a live-risk detector, not a historical audit), but long enough to not require the
  grant and the detector's poll to land in the same instant.
- `_RECENT_JOIN_WINDOW = 24 hours`: matches `WatchWindowService.WATCH_WINDOW_DURATION`
  exactly, so "newly-joined" here means the same thing "currently watched due to a
  recent join" already means elsewhere in this phase — a member is never double-
  counted as "newly joined" past the point their own watch window would have expired
  anyway.
- `_ELEVATED_PERMISSION_FLAGS`: `administrator`, `manage_guild`, `manage_roles`,
  `manage_webhooks`, `manage_channels`, `ban_members`, `kick_members` — the
  `discord.Permissions` flags that grant control over the guild's own security
  posture (who else has access, what channels/webhooks exist, who can be removed).
  Deliberately narrower than every "elevated-sounding" permission (for example
  `manage_messages` or `mention_everyone` are excluded) — the concern here is a
  compromised or malicious *currently-watched* member gaining the power to entrench
  or escalate further, not every moderately-privileged role a legitimate helper role
  might carry.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Final
from uuid import uuid4

import discord

from krubit.domain.models import GuildEvent, JSONValue
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand, RiskSignal
from krubit.storage.sqlite import SQLiteStore

_WEBHOOK_ABUSE_WINDOW: Final[timedelta] = timedelta(minutes=10)
_WEBHOOK_ABUSE_CHANNEL_THRESHOLD: Final[int] = 3
_WEBHOOK_SAME_CHANNEL_THRESHOLD: Final[int] = 3
_WEBHOOK_EVENT_CACHE_LIMIT: Final[int] = 200
_WEBHOOK_EVENT_TYPE: Final[str] = "webhooks_updated"
_WEBHOOK_SIGNAL_WEIGHT: Final[int] = 7
_WEBHOOK_SIGNAL_CONFIDENCE: Final[float] = 0.7
_WEBHOOK_RECOMMENDED_ACTION: Final[str] = (
    "Review this guild's current webhooks for the affected channel(s) and consider "
    "revoking any unrecognized webhook; no automatic action has been taken."
)

_ROLE_GRANT_LOOKBACK: Final[timedelta] = timedelta(minutes=30)
_RECENT_JOIN_WINDOW: Final[timedelta] = timedelta(hours=24)
_MEMBER_ROLES_UPDATED_EVENT_TYPE: Final[str] = "member_roles_updated"
_ROLE_EVENT_TYPES: Final[frozenset[str]] = frozenset({"role_created", "role_updated"})
_ELEVATED_PERMISSION_FLAGS: Final[tuple[str, ...]] = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_webhooks",
    "manage_channels",
    "ban_members",
    "kick_members",
)
_PERMISSION_SIGNAL_WEIGHT: Final[int] = 8
_PERMISSION_SIGNAL_CONFIDENCE: Final[float] = 0.8
_PERMISSION_RECOMMENDED_ACTION: Final[str] = (
    "Review the role grant for this member and consider reverting it until staff "
    "confirms it was intentional; no automatic action has been taken."
)

_EVENTS_SCAN_LIMIT: Final[int] = 500

# Injected so this task's tests never need Task 6's redaction/storage wiring; the
# default is a placeholder identifier only. See `krubit.services.raid_detection`'s
# matching `EvidencePacketBuilder` for the same convention.
EvidencePacketBuilder = Callable[[int, tuple[RiskSignal, ...], datetime], str]


def _default_evidence_builder(
    guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
) -> str:
    del guild_id, signals, now
    return f"evidence:{uuid4().hex}"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _role_ids_from_payload(payload: Mapping[str, JSONValue], key: str) -> frozenset[int]:
    side = payload.get(key)
    if not isinstance(side, dict):
        return frozenset()
    raw = side.get("role_ids")
    if not isinstance(raw, str) or not raw:
        return frozenset()
    return frozenset(int(part) for part in raw.split(",") if part)


class WebhookAbuseDetector:
    """Fire a `WEBHOOK_ABUSE` incident on a burst of webhook config-change events.

    Two independent signals, either of which is sufficient to fire:

    1. A durable, storage-backed signal: >= `channel_threshold` distinct channels
       with a `webhooks_updated` event in `SQLiteStore` within `window`. Works today
       with no extra wiring, but structurally cannot see repeated changes to a single
       channel — see the module docstring's "The distinct-channel signal alone misses
       same-channel repeated abuse" section for why.
    2. An in-memory signal fed by `record_webhook_event`: >= `same_channel_threshold`
       observed calls for the *same* channel within `window`. Mirrors
       `SpamWaveDetector`'s own in-memory correlation cache and closes exactly the
       gap signal 1 cannot see, at the cost of needing a runtime to call
       `record_webhook_event` per raw gateway callback (not yet wired — see the same
       module docstring section).
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        window: timedelta = _WEBHOOK_ABUSE_WINDOW,
        channel_threshold: int = _WEBHOOK_ABUSE_CHANNEL_THRESHOLD,
        same_channel_threshold: int = _WEBHOOK_SAME_CHANNEL_THRESHOLD,
        evidence_builder: EvidencePacketBuilder = _default_evidence_builder,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        if channel_threshold < 1:
            raise ValueError("channel_threshold must be positive")
        if same_channel_threshold < 1:
            raise ValueError("same_channel_threshold must be positive")
        self._store = store
        self._window = window
        self._channel_threshold = channel_threshold
        self._same_channel_threshold = same_channel_threshold
        self._evidence_builder = evidence_builder
        self._channel_events: dict[int, deque[tuple[int, datetime]]] = defaultdict(
            lambda: deque(maxlen=_WEBHOOK_EVENT_CACHE_LIMIT)
        )

    def record_webhook_event(self, guild_id: int, channel_id: int, now: datetime) -> None:
        """Remember one raw `on_webhooks_update` observation for `channel_id`.

        In-memory only, bounded, and never written to `SQLiteStore` -- see the module
        docstring's "The distinct-channel signal alone misses same-channel repeated
        abuse" section. Call this from wherever the raw gateway callback is handled,
        *before* (or independent of) that callback also calling `accept_event`, so a
        burst of same-channel, same-payload calls is counted here even though only
        the first of them survives storage's dedup.
        """
        _require_aware("now", now)
        self._channel_events[guild_id].append((channel_id, now))

    async def evaluate(self, guild_id: int, now: datetime) -> Incident | None:
        _require_aware("now", now)
        cutoff = now - self._window
        signals: list[RiskSignal] = []

        events = await self._store.list_events(guild_id, limit=_EVENTS_SCAN_LIMIT)
        recent_webhook_events = [
            event
            for event in events
            if event.event_type == _WEBHOOK_EVENT_TYPE and cutoff <= event.occurred_at <= now
        ]
        # Count distinct channels, not raw rows -- see the module docstring's "Why
        # this detector counts distinct channels, not raw event rows" section.
        affected_channels = {str(event.payload.get("entity_id")) for event in recent_webhook_events}
        if len(affected_channels) >= self._channel_threshold:
            signals.append(
                RiskSignal(
                    name="webhook_config_change_burst",
                    weight=_WEBHOOK_SIGNAL_WEIGHT,
                    detail=(
                        f"{len(affected_channels)} distinct channels had webhook "
                        f"configuration-change events in this guild within "
                        f"{int(self._window.total_seconds())} seconds"
                    ),
                    confidence=_WEBHOOK_SIGNAL_CONFIDENCE,
                )
            )

        hot_channel = self._hottest_same_channel(guild_id, cutoff, now)
        if hot_channel is not None:
            channel_id, occurrences = hot_channel
            signals.append(
                RiskSignal(
                    name="webhook_same_channel_reconfig_burst",
                    weight=_WEBHOOK_SIGNAL_WEIGHT,
                    detail=(
                        f"channel {channel_id} had {occurrences} webhook "
                        f"configuration-change observations within "
                        f"{int(self._window.total_seconds())} seconds (observed "
                        "directly via record_webhook_event, independent of "
                        "guild_events storage deduplication)"
                    ),
                    confidence=_WEBHOOK_SIGNAL_CONFIDENCE,
                )
            )

        if not signals:
            return None

        return await _record_incident(
            self._store,
            guild_id=guild_id,
            kind=IncidentKind.WEBHOOK_ABUSE,
            signals=tuple(signals),
            recommended_action=_WEBHOOK_RECOMMENDED_ACTION,
            evidence_builder=self._evidence_builder,
            now=now,
        )

    def _hottest_same_channel(
        self, guild_id: int, cutoff: datetime, now: datetime
    ) -> tuple[int, int] | None:
        """Return `(channel_id, count)` for the channel with the most in-window
        `record_webhook_event` observations, if any clears `same_channel_threshold`.
        """
        del now
        cache = self._channel_events.get(guild_id)
        if not cache:
            return None
        counts: dict[int, int] = {}
        for channel_id, observed_at in cache:
            if observed_at < cutoff:
                continue
            counts[channel_id] = counts.get(channel_id, 0) + 1
        if not counts:
            return None
        hottest_channel_id = max(counts, key=lambda channel_id: counts[channel_id])
        hottest_count = counts[hottest_channel_id]
        if hottest_count < self._same_channel_threshold:
            return None
        return hottest_channel_id, hottest_count


class PermissionRiskDetector:
    """Fire a `PERMISSION_RISK` incident when a currently-watched or newly-joined
    member is granted an elevated-permission role.

    See the module docstring's "Threshold design" section for the elevated-permission
    flag list and the "currently-watched or newly-joined" window definitions.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        role_grant_lookback: timedelta = _ROLE_GRANT_LOOKBACK,
        recent_join_window: timedelta = _RECENT_JOIN_WINDOW,
        evidence_builder: EvidencePacketBuilder = _default_evidence_builder,
    ) -> None:
        if role_grant_lookback <= timedelta(0):
            raise ValueError("role_grant_lookback must be positive")
        if recent_join_window <= timedelta(0):
            raise ValueError("recent_join_window must be positive")
        self._store = store
        self._role_grant_lookback = role_grant_lookback
        self._recent_join_window = recent_join_window
        self._evidence_builder = evidence_builder

    async def evaluate(self, guild_id: int, now: datetime) -> Incident | None:
        _require_aware("now", now)
        events = await self._store.list_events(guild_id, limit=_EVENTS_SCAN_LIMIT)
        cutoff = now - self._role_grant_lookback
        role_grant_events = [
            event
            for event in events
            if event.event_type == _MEMBER_ROLES_UPDATED_EVENT_TYPE
            and cutoff <= event.occurred_at <= now
        ]
        role_events = [event for event in events if event.event_type in _ROLE_EVENT_TYPES]

        for grant_event in role_grant_events:
            added_role_ids = _role_ids_from_payload(
                grant_event.payload, "after"
            ) - _role_ids_from_payload(grant_event.payload, "before")
            if not added_role_ids:
                continue
            elevated_role_id = self._first_elevated_role(added_role_ids, role_events)
            if elevated_role_id is None:
                continue

            entity_id = grant_event.payload.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id.isdigit():
                continue
            member_id = int(entity_id)

            if not await self._is_watched_or_newly_joined(guild_id, member_id, now):
                continue

            signal = RiskSignal(
                name="permission_grant_to_watched_member",
                weight=_PERMISSION_SIGNAL_WEIGHT,
                detail=(
                    f"member {member_id} was granted role {elevated_role_id}, which "
                    "carries an elevated permission, while currently watched or "
                    "newly joined"
                ),
                confidence=_PERMISSION_SIGNAL_CONFIDENCE,
            )
            return await _record_incident(
                self._store,
                guild_id=guild_id,
                kind=IncidentKind.PERMISSION_RISK,
                signals=(signal,),
                recommended_action=_PERMISSION_RECOMMENDED_ACTION,
                evidence_builder=self._evidence_builder,
                now=now,
            )
        return None

    @staticmethod
    def _first_elevated_role(
        role_ids: frozenset[int], role_events: list[GuildEvent]
    ) -> int | None:
        """Return the first of `role_ids` whose most-recent known permission state
        carries any `_ELEVATED_PERMISSION_FLAGS` flag, or `None` if none do.

        `role_events` is already ordered most-recent-first (per `list_events`), so the
        first matching event found for a role id is that role's current known state.
        """
        for role_id in role_ids:
            target = str(role_id)
            for event in role_events:
                if event.payload.get("entity_id") != target:
                    continue
                after = event.payload.get("after")
                if not isinstance(after, dict):
                    continue
                raw_permissions = after.get("permissions")
                if not isinstance(raw_permissions, str) or not raw_permissions.isdigit():
                    continue
                permissions = discord.Permissions(int(raw_permissions))
                if any(getattr(permissions, flag) for flag in _ELEVATED_PERMISSION_FLAGS):
                    return role_id
                break
        return None

    async def _is_watched_or_newly_joined(
        self, guild_id: int, member_id: int, now: datetime
    ) -> bool:
        open_windows = await self._store.list_open_watch_windows(guild_id)
        if any(window.member_id == member_id for window in open_windows):
            return True

        assessment = await self._store.get_entry_sniff_assessment(guild_id, member_id)
        if assessment is None:
            return False
        return now - assessment.joined_at <= self._recent_join_window


async def _record_incident(
    store: SQLiteStore,
    *,
    guild_id: int,
    kind: IncidentKind,
    signals: tuple[RiskSignal, ...],
    recommended_action: str,
    evidence_builder: EvidencePacketBuilder,
    now: datetime,
) -> Incident:
    evidence_packet_id = evidence_builder(guild_id, signals, now)
    incident = Incident(
        guild_id=guild_id,
        incident_id=f"{kind.value}:{uuid4().hex}",
        kind=kind,
        band=RiskBand.INCIDENT,
        opened_at=now,
        evidence_packet_id=evidence_packet_id,
        recommended_action=recommended_action,
        acknowledged_by=None,
    )
    saved = await store.record_incident(incident)
    detail: dict[str, JSONValue] = {
        "kind": saved.kind.value,
        "signal_names": [signal.name for signal in signals],
    }
    await store.record_sniff_receipt(
        guild_id=saved.guild_id,
        receipt_id=f"incident:{saved.incident_id}",
        member_id=None,
        action="incident_recorded",
        detail=detail,
        created_at=now,
    )
    return saved
