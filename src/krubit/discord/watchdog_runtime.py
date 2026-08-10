"""Live gateway wiring for Phase 3 Watchdog: joins, messages, and periodic sweeps.

`WatchdogRuntime` is the one place every Task 1-6 detection service becomes a live
Discord behavior. It is a thin adapter, matching `ContentRuntime`/`ConnectorScheduler`'s
own division of labor: every actual risk decision still lives in
`krubit.services.entry_sniff`/`watch_window`/`raid_detection`/
`webhook_and_permission_risk`; this module only decides *when* to call them and *what
Discord object* to send a resulting notification to.

## The two flagged in-memory caches (read before changing `on_message`/`on_webhooks_update`)

Task 5's review explicitly flagged that `SpamWaveDetector.record_message` and
`WebhookAbuseDetector.record_webhook_event` are both in-memory caches that only this
task's live wiring can ever feed, and warned that wiring one without the other would
silently resurface a detection gap in production. Both are wired here:
`record_message` from `on_message` (every guild message, not just from watched
members — the design doc's spam-wave detector correlates "currently-watched (or even
`clear`) members", so this is deliberately broader than the watch-window inspection
path below), and `record_webhook_event` from `on_webhooks_update` (the raw gateway
callback, matching `WebhookAbuseDetector.record_webhook_event`'s own docstring
instruction to call it "from wherever the raw gateway callback is handled").

## `watchdog_enabled`/`watchdog_notifications_enabled`: checked at the real boundary

Learned from Phase 2's `ContentRuntime.apply_plan` final-review finding
(`KRUBIT_CREATOR_SIGNALS_ENABLED`/`KRUBIT_SOCIAL_DELIVERY_ENABLED` were parsed but
never enforced in the send path): `watchdog_enabled` is checked at the top of every
public method here (`on_member_join`, `on_message`, `on_webhooks_update`,
`sweep_cycle`) as an early return, and `watchdog_notifications_enabled` is checked at
the top of `notify_staff` itself — the actual Discord-send boundary — not only once at
construction time. A caller that constructs this class with `watchdog_notifications_
enabled=False` gets a `notify_staff` that resolves no channel, sends no message, and
touches no Discord API at all, mirroring `apply_plan`'s own early-return-on-disabled-
flag pattern exactly.

## Message-content availability degrades honestly

`message_content_available` (normally `client.intents.message_content` after a
successful gateway connect — if the privileged intent were requested but not granted
in the Discord Developer Portal, the connection itself would already have failed
before this runtime is ever constructed, matching the existing precedent
`KrubitBot`'s `presence_intent` flag relies on) gates every message-content-dependent
path in `on_message`: when `False`, `on_message` returns immediately rather than
calling `WatchWindowService.inspect_message`/`SpamWaveDetector.record_message` against
a `message.content` that may not actually be populated — matching the design doc's
"until enabled, Krubit degrades to join-signal-only detection ... rather than
failing" stance.

## Avoiding a watch-window query per message

`WatchWindowService.inspect_message` already re-validates against storage as its own
"second, independent line of defense" (see that method's docstring) — but calling it
for every single guild message would mean a `list_open_watch_windows` round trip per
message even for the overwhelming majority that are not from a watched member. This
runtime keeps a small in-memory `{guild_id: {member_id}}` cache, populated when
`open_if_warranted`/`sweep_expired` open or close a window and seeded per guild by
`bootstrap_guild` (called from `on_guild_available`/`on_guild_join`, mirroring
`LiveSignalRuntime.bootstrap_guild`'s own established pattern, so a bot restart does
not silently blind watch-window message inspection until the next sweep). `on_message`
consults this cache first and only calls `inspect_message` for a member the cache
says is currently watched.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

import discord

from krubit.discord.cards import render_card
from krubit.domain.models import Card, CardField, JSONValue
from krubit.domain.watchdog import (
    EntrySniffAssessment,
    Incident,
    IncidentKind,
    RiskBand,
    RiskSignal,
)
from krubit.services.entry_sniff import EntrySniffService
from krubit.services.raid_detection import RaidDetector, SpamWaveDetector
from krubit.services.watch_window import InspectableMessage, WatchWindowService
from krubit.services.webhook_and_permission_risk import (
    PermissionRiskDetector,
    WebhookAbuseDetector,
)
from krubit.storage.sqlite import SQLiteStore

_logger = logging.getLogger(__name__)

GuildLookup = Callable[[int], discord.Guild | None]
GuildIds = Callable[[], Sequence[int]]

_MEMBER_INCIDENT_RECOMMENDED_ACTION = (
    "Review this member's Entry Sniff assessment and signals; consider "
    "manual verification, timeout, or removal if warranted. No automatic "
    "action has been taken."
)


def _default_evidence_builder(
    guild_id: int, signals: tuple[RiskSignal, ...], now: datetime
) -> str:
    del guild_id, signals, now
    return f"evidence:{uuid4().hex}"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class _WebhooksUpdateChannel(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def guild(self) -> discord.Guild: ...


class _StaffChannel(Protocol):
    """The minimal shape `notify_staff` needs from a resolved staff channel."""

    def permissions_for(self, member: object) -> discord.Permissions: ...

    async def send(self, *, embed: discord.Embed) -> object: ...


def _is_sendable_channel(channel: object | None) -> bool:
    return (
        channel is not None
        and callable(getattr(channel, "send", None))
        and callable(getattr(channel, "permissions_for", None))
    )


class WatchdogRuntime:
    """Wire Task 1-6's detection services into live Discord gateway events."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        watchdog_enabled: bool,
        watchdog_notifications_enabled: bool,
        staff_channel_id: int | None,
        guild_lookup: GuildLookup,
        guild_ids: GuildIds,
        message_content_available: bool,
        entry_sniff: EntrySniffService | None = None,
        watch_window: WatchWindowService | None = None,
        raid_detector: RaidDetector | None = None,
        spam_wave_detector: SpamWaveDetector | None = None,
        webhook_abuse_detector: WebhookAbuseDetector | None = None,
        permission_risk_detector: PermissionRiskDetector | None = None,
        now: Callable[[], datetime] | None = None,
        evidence_builder: Callable[[int, tuple[RiskSignal, ...], datetime], str]
        | None = None,
    ) -> None:
        self._store = store
        self._watchdog_enabled = watchdog_enabled
        self._watchdog_notifications_enabled = watchdog_notifications_enabled
        self._staff_channel_id = staff_channel_id
        self._guild_lookup = guild_lookup
        self._guild_ids = guild_ids
        self._message_content_available = message_content_available
        self._entry_sniff = entry_sniff or EntrySniffService(store)
        self._watch_window = watch_window or WatchWindowService(store)
        self._raid_detector = raid_detector or RaidDetector(store)
        self._spam_wave_detector = spam_wave_detector or SpamWaveDetector(store)
        self._webhook_abuse_detector = webhook_abuse_detector or WebhookAbuseDetector(store)
        self._permission_risk_detector = (
            permission_risk_detector or PermissionRiskDetector(store)
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._evidence_builder = evidence_builder or _default_evidence_builder
        # See the module docstring's "Avoiding a watch-window query per message"
        # section. Never the authority on whether a window is open -- only a fast
        # pre-filter; `WatchWindowService.inspect_message` re-validates against
        # storage independently.
        self._watched_members: dict[int, set[int]] = defaultdict(set)

    async def bootstrap_guild(self, guild_id: int, now: datetime) -> None:
        """Seed the watched-member cache for `guild_id` from durable storage.

        Call once per guild on `on_guild_available`/`on_guild_join`, matching
        `LiveSignalRuntime.bootstrap_guild`'s established pattern -- without this, a
        process restart would silently blind `on_message`'s in-memory pre-filter for
        every member whose watch window was already open before the restart, until
        that member's window happened to expire and get swept.
        """
        if not self._watchdog_enabled:
            return
        _require_aware("now", now)
        windows = await self._store.list_open_watch_windows(guild_id)
        self._watched_members[guild_id] = {window.member_id for window in windows}

    async def on_member_join(self, member: discord.Member, now: datetime) -> None:
        """Assess a fresh join, open a bounded watch window when warranted, and --
        new in this task -- record a durable `Incident` and notify staff
        immediately when the assessment reaches `RiskBand.INCIDENT`.

        Exactly Task 4's original two calls, in order, unchanged: `EntrySniffService.
        assess_join` produces the one durable per-join assessment, then
        `WatchWindowService.open_if_warranted` opens a window for anything above
        `RiskBand.CLEAR`. The new third step is strictly additive and never skips or
        reorders either original call. No same-kind dedup is applied here (unlike the
        four sweep-cycle detectors' `_has_open_incident_of_kind` cooldown) -- each
        `INCIDENT`-band join is an independent member and independent evidence, so
        every one gets its own `Incident` and its own notification.
        """
        if not self._watchdog_enabled:
            return
        _require_aware("now", now)
        assessment = await self._entry_sniff.assess_join(member, now)
        window = await self._watch_window.open_if_warranted(assessment, now)
        if window is not None:
            self._watched_members[window.guild_id].add(window.member_id)
        if assessment.band is RiskBand.INCIDENT:
            await self._record_member_incident(assessment, now)

    async def _record_member_incident(
        self, assessment: EntrySniffAssessment, now: datetime
    ) -> None:
        evidence_packet_id = self._evidence_builder(
            assessment.guild_id, assessment.signals, now
        )
        incident = Incident(
            guild_id=assessment.guild_id,
            incident_id=f"member:{uuid4().hex}",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=now,
            evidence_packet_id=evidence_packet_id,
            recommended_action=_MEMBER_INCIDENT_RECOMMENDED_ACTION,
            acknowledged_by=None,
        )
        saved = await self._store.record_incident(incident)
        detail: dict[str, JSONValue] = {
            "kind": saved.kind.value,
            "signal_names": [signal.name for signal in assessment.signals],
        }
        await self._store.record_sniff_receipt(
            guild_id=saved.guild_id,
            receipt_id=f"incident:{saved.incident_id}",
            member_id=assessment.member_id,
            action="incident_recorded",
            detail=detail,
            created_at=now,
        )
        await self.notify_staff(saved)

    async def on_message(self, message: InspectableMessage, now: datetime) -> None:
        """Feed a guild message into watch-window inspection and spam-wave correlation.

        A no-op unless `watchdog_enabled` and `message_content_available` are both
        true, and always a no-op for a DM (`message.guild is None`) or a bot's own
        message -- see the module docstring's "Message-content availability degrades
        honestly" section. `SpamWaveDetector.record_message` is fed for every
        qualifying guild message (its cross-member correlation is deliberately not
        limited to watched members, per the design doc); `WatchWindowService.
        inspect_message` is only attempted for a member this runtime's cache currently
        believes is watched, and any signal it returns is recorded as an evidence
        receipt -- read-only bookkeeping, never an escalation or notification, matching
        this task's narrow on_message scope.
        """
        if not self._watchdog_enabled or not self._message_content_available:
            return
        _require_aware("now", now)
        guild = message.guild
        if guild is None:
            return
        author = message.author
        if getattr(author, "bot", False):
            return

        self._spam_wave_detector.record_message(guild.id, author.id, message.content, now)

        if author.id not in self._watched_members.get(guild.id, ()):
            return
        signal = await self._watch_window.inspect_message(message, now)
        if signal is None:
            return
        await self._store.record_sniff_receipt(
            guild_id=guild.id,
            receipt_id=f"watch-window-message:{uuid4().hex}",
            member_id=author.id,
            action="watch_window_message_signal_observed",
            detail={
                "signal_name": signal.name,
                "detail": signal.detail,
                "weight": signal.weight,
                "confidence": signal.confidence,
            },
            created_at=now,
        )

    async def on_webhooks_update(self, channel: _WebhooksUpdateChannel, now: datetime) -> None:
        """Feed one raw webhook-configuration-change gateway callback into
        `WebhookAbuseDetector`'s in-memory same-channel cache.

        Call this from `KrubitBot.on_webhooks_update`, independently of (and in
        addition to) that handler's existing `_ingest_change` call -- see the module
        docstring's "The two flagged in-memory caches" section for why both this and
        `on_message`'s `record_message` call must exist for detection to work as
        designed.
        """
        if not self._watchdog_enabled:
            return
        _require_aware("now", now)
        self._webhook_abuse_detector.record_webhook_event(channel.guild.id, channel.id, now)

    async def sweep_cycle(self, now: datetime) -> None:
        """Close expired watch windows and run every guild-scoped detector once.

        Each guild is its own isolation boundary and, within a guild, each detector
        call is its own isolation boundary too -- matching `ConnectorScheduler.
        run_cycle`'s discipline that one guild's or one job's failure must never
        cancel or block any other guild's or detector's work in the same cycle. A
        detector that returns an `Incident` triggers exactly one `notify_staff` call.
        """
        if not self._watchdog_enabled:
            return
        _require_aware("now", now)
        for guild_id in self._guild_ids():
            try:
                await self._sweep_guild(guild_id, now)
            except Exception:
                _logger.exception(
                    "WatchdogRuntime.sweep_cycle: unhandled exception sweeping guild %s; "
                    "continuing with the next guild",
                    guild_id,
                )

    async def _sweep_guild(self, guild_id: int, now: datetime) -> None:
        try:
            closed = await self._watch_window.sweep_expired(guild_id, now)
        except Exception:
            _logger.exception(
                "WatchdogRuntime.sweep_cycle: sweep_expired failed for guild %s", guild_id
            )
        else:
            watched = self._watched_members.get(guild_id)
            if watched is not None:
                for window in closed:
                    watched.discard(window.member_id)

        for detector_name, evaluate in self._detector_jobs(guild_id):
            try:
                incident = await evaluate(now)
            except Exception:
                _logger.exception(
                    "WatchdogRuntime.sweep_cycle: %s.evaluate failed for guild %s",
                    detector_name,
                    guild_id,
                )
                continue
            if incident is not None:
                await self.notify_staff(incident)

    def _detector_jobs(
        self, guild_id: int
    ) -> tuple[tuple[str, Callable[[datetime], Awaitable[Incident | None]]], ...]:
        return (
            ("RaidDetector", lambda now: self._raid_detector.evaluate(guild_id, now)),
            ("SpamWaveDetector", lambda now: self._spam_wave_detector.evaluate(guild_id, now)),
            (
                "WebhookAbuseDetector",
                lambda now: self._webhook_abuse_detector.evaluate(guild_id, now),
            ),
            (
                "PermissionRiskDetector",
                lambda now: self._permission_risk_detector.evaluate(guild_id, now),
            ),
        )

    async def notify_staff(self, incident: Incident) -> None:
        """Send one incident notification to the configured staff channel.

        Genuinely a no-op unless `watchdog_notifications_enabled` -- checked first,
        before any channel resolution -- so a disabled deployment never resolves a
        guild, never touches Discord permissions, and never calls `channel.send` at
        all. See the module docstring's "checked at the real boundary" section.
        """
        if not self._watchdog_notifications_enabled:
            return
        if self._staff_channel_id is None:
            return
        guild = self._guild_lookup(incident.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(self._staff_channel_id)
        if not _is_sendable_channel(channel):
            return
        sendable = cast(_StaffChannel, channel)
        permissions = sendable.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
            )
        ):
            return
        try:
            await sendable.send(embed=render_card(_incident_card(incident)))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            _logger.exception(
                "WatchdogRuntime.notify_staff: failed to deliver incident %s to guild %s",
                incident.incident_id,
                incident.guild_id,
            )


def _incident_card(incident: Incident) -> Card:
    return Card(
        kind="watchdog_incident",
        title=f"⚠️ Krubit Watchdog: {incident.kind.value} incident",
        description=(
            f"Band: **{RiskBand.INCIDENT.value}**\n"
            f"Opened: {incident.opened_at.isoformat()}"
        ),
        fields=(
            CardField("Incident ID", incident.incident_id, inline=True),
            CardField("Evidence packet", incident.evidence_packet_id, inline=True),
            CardField("Recommended action", incident.recommended_action, inline=False),
        ),
        color=0xDC2626,
    )
