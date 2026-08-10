"""Integration tests for `krubit.discord.watchdog_runtime.WatchdogRuntime`.

Exercises the live wiring against a real on-disk `SQLiteStore` (never mocked),
matching every other Phase 3 service test's convention. Small local `Fake*` classes
stand in for `discord.Member`/`discord.Guild`/`discord.Message`/a Discord text
channel, matching `test_watch_window_service.py`'s established pattern.

Per Task 5's carried-forward review finding, this file gives explicit, separate
coverage to BOTH in-memory caches this task wires live
(`SpamWaveDetector.record_message` via `on_message`,
`WebhookAbuseDetector.record_webhook_event` via `on_webhooks_update`) so a future
change that silently drops one wiring is caught here, not just by inspection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from krubit.discord.watchdog_runtime import WatchdogRuntime
from krubit.domain.watchdog import AllowBlockEntry, Incident, IncidentKind, RiskBand
from krubit.services.raid_detection import SpamWaveDetector
from krubit.services.watch_window import WATCH_WINDOW_DURATION
from krubit.services.webhook_and_permission_risk import WebhookAbuseDetector
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111
MEMBER_ID = 222
STAFF_CHANNEL_ID = 999


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


class FakeChannel:
    def __init__(self, channel_id: int = STAFF_CHANNEL_ID) -> None:
        self.id = channel_id
        self.sent: tuple[dict[str, object], ...] = ()
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.guild: FakeGuild | None = None

    def permissions_for(self, member: object) -> object:
        return self.permissions

    async def send(self, **kwargs: object) -> None:
        self.sent = (*self.sent, kwargs)


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID) -> None:
        self.id = guild_id
        self.members: list[object] = []
        self.channels: dict[int, FakeChannel] = {}
        self.me = object()

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


class FakeMember:
    def __init__(
        self,
        member_id: int,
        guild: FakeGuild,
        *,
        created_at: datetime = NOW - timedelta(days=365),
        has_avatar: bool = True,
        joined_at: datetime | None = NOW,
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.created_at = created_at
        self.joined_at = joined_at
        self.avatar: object | None = object() if has_avatar else None
        self.bot = False
        self.system = False
        self.pending = False
        self.name = f"member{member_id}"


class FakeAuthor:
    def __init__(self, author_id: int, *, bot: bool = False) -> None:
        self.id = author_id
        self.bot = bot


class FakeMessage:
    def __init__(
        self,
        *,
        content: str = "hello there",
        author_id: int = MEMBER_ID,
        author_bot: bool = False,
        guild: FakeGuild | None = None,
        mention_count: int = 0,
        mention_everyone: bool = False,
    ) -> None:
        self.content = content
        self.author = FakeAuthor(author_id, bot=author_bot)
        self.guild: FakeGuild | None = guild if guild is not None else FakeGuild()
        self.mentions = [object() for _ in range(mention_count)]
        self.role_mentions: list[object] = []
        self.mention_everyone = mention_everyone


def as_guild(guild: FakeGuild) -> discord.Guild:
    return cast(discord.Guild, guild)


def incident(*, guild_id: int = GUILD_ID) -> Incident:
    return Incident(
        guild_id=guild_id,
        incident_id="raid:test-incident",
        kind=IncidentKind.RAID,
        band=RiskBand.INCIDENT,
        opened_at=NOW,
        evidence_packet_id="evidence:test",
        recommended_action="Review and decide manually; no automatic action taken.",
        acknowledged_by=None,
    )


def build_runtime(
    store: SQLiteStore,
    *,
    guild: FakeGuild,
    watchdog_enabled: bool = True,
    watchdog_notifications_enabled: bool = False,
    message_content_available: bool = True,
    staff_channel_id: int | None = STAFF_CHANNEL_ID,
    **kwargs: object,
) -> WatchdogRuntime:
    return WatchdogRuntime(
        store,
        watchdog_enabled=watchdog_enabled,
        watchdog_notifications_enabled=watchdog_notifications_enabled,
        staff_channel_id=staff_channel_id,
        guild_lookup=lambda gid: as_guild(guild) if gid == guild.id else None,
        guild_ids=lambda: (guild.id,),
        message_content_available=message_content_available,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def runtime(store: SQLiteStore) -> WatchdogRuntime:
    guild = FakeGuild()
    channel = FakeChannel()
    channel.guild = guild
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=False)
    # Test-only convenience handle onto the fake channel `notify_staff` would resolve
    # to for this guild, so tests can assert on it directly without re-deriving the
    # lookup path themselves.
    rt.channel = channel  # type: ignore[attr-defined]
    return rt


# --- notify_staff: the actual send-boundary gate -----------------------------------


@pytest.mark.asyncio
async def test_notify_staff_sends_nothing_when_notifications_disabled(
    runtime: WatchdogRuntime,
) -> None:
    await runtime.notify_staff(incident())
    assert runtime.channel.sent == ()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_notify_staff_sends_when_notifications_enabled(store: SQLiteStore) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)

    await rt.notify_staff(incident())

    assert len(channel.sent) == 1
    assert "embed" in channel.sent[0]


@pytest.mark.asyncio
async def test_notify_staff_does_nothing_without_a_configured_staff_channel(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    rt = build_runtime(
        store, guild=guild, watchdog_notifications_enabled=True, staff_channel_id=None
    )

    await rt.notify_staff(incident())  # must not raise despite no channel configured


# --- on_member_join: EntrySniffService.assess_join -> WatchWindowService.open_if_warranted


@pytest.mark.asyncio
async def test_on_member_join_opens_a_watch_window_for_an_elevated_band(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)  # default_avatar -> WATCH

    await rt.on_member_join(cast(discord.Member, member), NOW)

    windows = await store.list_open_watch_windows(GUILD_ID)
    assert len(windows) == 1
    assert windows[0].member_id == MEMBER_ID


@pytest.mark.asyncio
async def test_on_member_join_opens_no_window_for_a_clear_band(store: SQLiteStore) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild)
    member = FakeMember(MEMBER_ID, guild, has_avatar=True)  # no signals -> CLEAR

    await rt.on_member_join(cast(discord.Member, member), NOW)

    assert await store.list_open_watch_windows(GUILD_ID) == ()


@pytest.mark.asyncio
async def test_on_member_join_is_a_no_op_when_watchdog_disabled(store: SQLiteStore) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild, watchdog_enabled=False)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)

    await rt.on_member_join(cast(discord.Member, member), NOW)

    assert await store.get_entry_sniff_assessment(GUILD_ID, MEMBER_ID) is None
    assert await store.list_open_watch_windows(GUILD_ID) == ()


@pytest.mark.asyncio
async def test_on_member_join_records_and_notifies_an_incident_for_incident_band(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=MEMBER_ID,
            list_kind="block",
            reason="test block entry",
            set_by=1,
            set_at=NOW,
        )
    )
    member = FakeMember(MEMBER_ID, guild)

    await rt.on_member_join(cast(discord.Member, member), NOW)

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert len(incidents) == 1
    assert incidents[0].kind is IncidentKind.MEMBER
    assert incidents[0].band is RiskBand.INCIDENT
    assert len(channel.sent) == 1
    assert "embed" in channel.sent[0]


@pytest.mark.asyncio
async def test_on_member_join_records_no_incident_for_a_watch_band(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)  # WATCH band, not INCIDENT

    await rt.on_member_join(cast(discord.Member, member), NOW)

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert incidents == ()
    assert channel.sent == ()


@pytest.mark.asyncio
async def test_on_member_join_incident_reflects_the_assessments_own_signals(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=MEMBER_ID,
            list_kind="block",
            reason="test block entry",
            set_by=1,
            set_at=NOW,
        )
    )
    member = FakeMember(MEMBER_ID, guild)

    await rt.on_member_join(cast(discord.Member, member), NOW)

    assessment = await store.get_entry_sniff_assessment(GUILD_ID, MEMBER_ID)
    assert assessment is not None
    receipts = await store.list_sniff_receipts(GUILD_ID)
    incident_receipt = next(r for r in receipts if r.action == "incident_recorded")
    signal_names = incident_receipt.detail["signal_names"]
    assert signal_names == [s.name for s in assessment.signals]


@pytest.mark.asyncio
async def test_two_separate_incident_band_joins_each_get_their_own_incident(
    store: SQLiteStore,
) -> None:
    """Proves the no-dedup Global Constraint: a second INCIDENT-band join
    while a first is still fresh is NOT suppressed, unlike the four
    existing guild-scoped detectors' same-kind cooldown."""
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    rt = build_runtime(store, guild=guild, watchdog_notifications_enabled=True)
    other_member_id = MEMBER_ID + 1
    for mid in (MEMBER_ID, other_member_id):
        await store.save_allow_block_entry(
            AllowBlockEntry(
                guild_id=GUILD_ID,
                discord_user_id=mid,
                list_kind="block",
                reason="test block entry",
                set_by=1,
                set_at=NOW,
            )
        )

    await rt.on_member_join(cast(discord.Member, FakeMember(MEMBER_ID, guild)), NOW)
    await rt.on_member_join(
        cast(discord.Member, FakeMember(other_member_id, guild)), NOW + timedelta(seconds=1)
    )

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert len(incidents) == 2
    assert len(channel.sent) == 2


# --- on_message: both flagged in-memory caches must both be fed --------------------


@pytest.mark.asyncio
async def test_on_message_feeds_spam_wave_detector_for_every_guild_message(
    store: SQLiteStore,
) -> None:
    """`SpamWaveDetector.record_message` must be called for every qualifying guild
    message, not only from watched members (the design doc's spam-wave detector
    correlates "currently-watched (or even clear) members")."""
    guild = FakeGuild()
    spam_wave = SpamWaveDetector(store)
    rt = build_runtime(store, guild=guild, spam_wave_detector=spam_wave)

    payload = "buy cheap tokens now at totally-legit-site.example click fast"
    for member_id in (301, 302, 303):
        await rt.on_message(
            cast(
                "discord.Message",
                FakeMessage(content=payload, author_id=member_id, guild=guild),
            ),
            NOW,
        )

    fired = await spam_wave.evaluate(GUILD_ID, NOW)
    assert fired is not None
    assert fired.kind is IncidentKind.SPAM_WAVE


@pytest.mark.asyncio
async def test_on_message_ignores_bot_authored_messages(store: SQLiteStore) -> None:
    guild = FakeGuild()
    spam_wave = SpamWaveDetector(store)
    rt = build_runtime(store, guild=guild, spam_wave_detector=spam_wave)

    await rt.on_message(
        cast(
            "discord.Message",
            FakeMessage(content="hello", author_id=401, author_bot=True, guild=guild),
        ),
        NOW,
    )

    assert spam_wave._messages.get(GUILD_ID) in (None, [])  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_message_inspects_only_watched_members(store: SQLiteStore) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild)
    watched_member = FakeMember(MEMBER_ID, guild, has_avatar=False)  # -> WATCH band
    await rt.on_member_join(cast(discord.Member, watched_member), NOW)

    # An unwatched member's risky message must not produce a watch-window receipt.
    unwatched_message = FakeMessage(
        content="hi", author_id=555, guild=guild, mention_count=25
    )
    await rt.on_message(cast("discord.Message", unwatched_message), NOW)
    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=555)
    assert not any(r.action == "watch_window_message_signal_observed" for r in receipts)

    # The watched member's risky message must be inspected and receipted.
    watched_message = FakeMessage(
        content="hi", author_id=MEMBER_ID, guild=guild, mention_count=25
    )
    await rt.on_message(cast("discord.Message", watched_message), NOW)
    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=MEMBER_ID)
    assert any(r.action == "watch_window_message_signal_observed" for r in receipts)


@pytest.mark.asyncio
async def test_on_message_is_a_no_op_without_message_content_availability(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    spam_wave = SpamWaveDetector(store)
    rt = build_runtime(
        store,
        guild=guild,
        message_content_available=False,
        spam_wave_detector=spam_wave,
    )
    watched_member = FakeMember(MEMBER_ID, guild, has_avatar=False)
    await rt.on_member_join(cast(discord.Member, watched_member), NOW)

    await rt.on_message(
        cast(
            "discord.Message",
            FakeMessage(content="hi", author_id=MEMBER_ID, guild=guild, mention_count=25),
        ),
        NOW,
    )

    assert spam_wave._messages.get(GUILD_ID) in (None, [])  # type: ignore[attr-defined]
    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=MEMBER_ID)
    assert not any(r.action == "watch_window_message_signal_observed" for r in receipts)


# --- on_webhooks_update: the second flagged in-memory cache ------------------------


@pytest.mark.asyncio
async def test_on_webhooks_update_feeds_webhook_abuse_detector_same_channel_cache(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    webhook_abuse = WebhookAbuseDetector(store)
    rt = build_runtime(store, guild=guild, webhook_abuse_detector=webhook_abuse)
    channel = SimpleNamespace(id=777, guild=guild)

    for _ in range(3):
        await rt.on_webhooks_update(channel, NOW)  # type: ignore[arg-type]

    fired = await webhook_abuse.evaluate(GUILD_ID, NOW)
    assert fired is not None
    assert fired.kind is IncidentKind.WEBHOOK_ABUSE


# --- sweep_cycle: expiry + all four detectors + isolation --------------------------


@pytest.mark.asyncio
async def test_sweep_cycle_closes_expired_windows(store: SQLiteStore) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)
    await rt.on_member_join(cast(discord.Member, member), NOW)
    assert len(await store.list_open_watch_windows(GUILD_ID)) == 1

    await rt.sweep_cycle(NOW + WATCH_WINDOW_DURATION + timedelta(seconds=1))

    assert await store.list_open_watch_windows(GUILD_ID) == ()


@pytest.mark.asyncio
async def test_sweep_cycle_notifies_staff_when_a_detector_fires(store: SQLiteStore) -> None:
    guild = FakeGuild()
    channel = FakeChannel()
    guild.channels[channel.id] = channel
    webhook_abuse = WebhookAbuseDetector(store)
    rt = build_runtime(
        store,
        guild=guild,
        watchdog_notifications_enabled=True,
        webhook_abuse_detector=webhook_abuse,
    )
    fake_channel_events = SimpleNamespace(id=888, guild=guild)
    for _ in range(3):
        await rt.on_webhooks_update(fake_channel_events, NOW)  # type: ignore[arg-type]

    await rt.sweep_cycle(NOW)

    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_sweep_cycle_isolates_one_guilds_detector_failure_from_another_guild(
    store: SQLiteStore,
) -> None:
    """A real concurrent-failure scenario: guild A's `RaidDetector.evaluate` raises,
    guild B's watch-window sweep must still run to completion in the same cycle."""
    guild_a = FakeGuild(guild_id=111)
    guild_b = FakeGuild(guild_id=222)

    class ExplodingRaidDetector:
        async def evaluate(self, guild_id: int, now: datetime) -> Incident | None:
            if guild_id == guild_a.id:
                raise RuntimeError("synthetic raid-detector failure for guild A")
            return None

    rt = WatchdogRuntime(
        store,
        watchdog_enabled=True,
        watchdog_notifications_enabled=False,
        staff_channel_id=None,
        guild_lookup=lambda gid: as_guild({guild_a.id: guild_a, guild_b.id: guild_b}[gid])
        if gid in (guild_a.id, guild_b.id)
        else None,
        guild_ids=lambda: (guild_a.id, guild_b.id),
        message_content_available=True,
        raid_detector=cast("object", ExplodingRaidDetector()),  # type: ignore[arg-type]
    )
    member_b = FakeMember(444, guild_b, has_avatar=False)
    await rt.on_member_join(cast(discord.Member, member_b), NOW)
    assert len(await store.list_open_watch_windows(guild_b.id)) == 1

    await rt.sweep_cycle(NOW + WATCH_WINDOW_DURATION + timedelta(seconds=1))

    # Guild B's sweep must have completed despite guild A's detector raising.
    assert await store.list_open_watch_windows(guild_b.id) == ()


@pytest.mark.asyncio
async def test_sweep_cycle_is_a_no_op_when_watchdog_disabled(store: SQLiteStore) -> None:
    guild = FakeGuild()
    rt = build_runtime(store, guild=guild, watchdog_enabled=False)
    member = FakeMember(MEMBER_ID, guild, has_avatar=False)
    # Seed a window directly through storage (bypassing on_member_join, which is
    # itself gated) so this test only exercises sweep_cycle's own gate.
    from krubit.domain.watchdog import WatchWindow

    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=member.id,
            opened_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )

    await rt.sweep_cycle(NOW + timedelta(hours=2))

    assert len(await store.list_open_watch_windows(GUILD_ID)) == 1


# --- bootstrap_guild: restart-safety for the watched-member cache ------------------


@pytest.mark.asyncio
async def test_bootstrap_guild_seeds_the_watched_member_cache_from_storage(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild()
    from krubit.domain.watchdog import WatchWindow

    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=MEMBER_ID,
            opened_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )
    rt = build_runtime(store, guild=guild)  # a fresh instance, as after a restart

    await rt.bootstrap_guild(GUILD_ID, NOW)

    message = FakeMessage(content="hi", author_id=MEMBER_ID, guild=guild, mention_count=25)
    await rt.on_message(cast("discord.Message", message), NOW)
    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=MEMBER_ID)
    assert any(r.action == "watch_window_message_signal_observed" for r in receipts)
