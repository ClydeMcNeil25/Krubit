from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from krubit.config import Settings
from krubit.discord.bot import KrubitBot
from krubit.services.daily_summary import DailySummaryService
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


class _FakeTextChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        can_view: bool = True,
        can_send: bool = True,
        can_embed: bool = True,
        fail_send: bool = False,
    ) -> None:
        self.id = channel_id
        self._permissions = SimpleNamespace(
            view_channel=can_view,
            send_messages=can_send,
            embed_links=can_embed,
        )
        self._fail_send = fail_send
        self.sent: list[object] = []

    def permissions_for(self, member: object) -> SimpleNamespace:
        return self._permissions

    async def send(self, *, embed: object) -> None:
        if self._fail_send:
            raise RuntimeError("simulated Discord delivery failure")
        self.sent.append(embed)


class _FakeGuild:
    def __init__(self, channel: object | None) -> None:
        self.id = 111
        self.name = "Krucial Town"
        self.me = SimpleNamespace(guild_permissions=discord.Permissions.none())
        self.roles: list[object] = []
        self.channels: list[object] = []
        self.scheduled_events: list[object] = []
        self._channel = channel

    def get_channel(self, channel_id: int) -> object | None:
        return self._channel if getattr(self._channel, "id", None) == channel_id else None

    async def webhooks(self) -> list[object]:
        return []

    async def fetch_automod_rules(self) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_daily_summary_claim_is_once_per_guild_and_date(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    service = DailySummaryService(store)
    try:
        assert await service.claim(111, date(2026, 8, 4)) is True
        assert await service.claim(111, date(2026, 8, 4)) is False
        assert await service.claim(222, date(2026, 8, 4)) is True
        assert await service.claim(111, date(2026, 8, 5)) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_bot_daily_summary_sends_nothing_when_delivery_is_disabled(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db"),
        FoundationService(store),
    )
    guild = cast(discord.Guild, SimpleNamespace(id=111))
    try:
        first = await bot.run_daily_summary_for_guild(guild, date(2026, 8, 4))
        second = await bot.run_daily_summary_for_guild(guild, date(2026, 8, 4))

        assert first.status == "delivery_disabled"
        assert second.status == "already_claimed"
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_disabled_outcome_is_durable(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    service = DailySummaryService(store)
    try:
        result = await service.record_outcome(
            111, date(2026, 8, 4), status="delivery_disabled", channel_id=None
        )

        assert result.status == "delivery_disabled"
        assert await store.daily_summary_status(111, date(2026, 8, 4)) == "delivery_disabled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_missing_channel_is_durable_and_receipted(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            staff_channel_id=999,
        ),
        FoundationService(store),
    )
    try:
        result = await bot.run_daily_summary_for_guild(
            cast(discord.Guild, _FakeGuild(None)), date(2026, 8, 4)
        )

        assert result.status == "channel_missing"
        assert await store.daily_summary_status(111, date(2026, 8, 4)) == "channel_missing"
        receipt = (await store.list_receipts(111))[0]
        assert (receipt.action, receipt.status, receipt.detail) == (
            "daily_health_summary",
            "failed",
            {"channel_id": 999, "reason": "staff_channel_missing"},
        )
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_skips_disabled_guild_without_persisting_activity(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db"),
        FoundationService(store),
    )
    guild = cast(discord.Guild, SimpleNamespace(id=111))
    try:
        result = await bot.run_daily_summary_for_guild(guild, date(2026, 8, 4))

        assert result.status == "guild_disabled"
        assert await store.daily_summary_status(111, date(2026, 8, 4)) is None
        assert await store.list_receipts(111) == []
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_missing_send_permission_is_durable_and_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    channel = _FakeTextChannel(999, can_send=False)
    monkeypatch.setattr("krubit.discord.bot.discord.TextChannel", _FakeTextChannel)
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            staff_channel_id=999,
        ),
        FoundationService(store),
    )
    try:
        result = await bot.run_daily_summary_for_guild(
            cast(discord.Guild, _FakeGuild(channel)), date(2026, 8, 4)
        )

        assert result.status == "permission_missing"
        assert channel.sent == []
        receipt = (await store.list_receipts(111))[0]
        assert (receipt.action, receipt.status, receipt.detail) == (
            "daily_health_summary",
            "failed",
            {"channel_id": 999, "reason": "staff_channel_not_writable"},
        )
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_delivery_failure_is_durable_and_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    channel = _FakeTextChannel(999, fail_send=True)
    monkeypatch.setattr("krubit.discord.bot.discord.TextChannel", _FakeTextChannel)
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            staff_channel_id=999,
        ),
        FoundationService(store),
    )
    try:
        result = await bot.run_daily_summary_for_guild(
            cast(discord.Guild, _FakeGuild(channel)), date(2026, 8, 4)
        )

        assert result.status == "failed"
        receipt = (await store.list_receipts(111))[0]
        assert (receipt.action, receipt.status, receipt.detail) == (
            "daily_health_summary",
            "failed",
            {
                "channel_id": 999,
                "error_type": "RuntimeError",
                "reason": "discord_delivery_failed",
            },
        )
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_daily_summary_success_sends_once_and_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    channel = _FakeTextChannel(999)
    monkeypatch.setattr("krubit.discord.bot.discord.TextChannel", _FakeTextChannel)
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            staff_channel_id=999,
        ),
        FoundationService(store),
    )
    guild = cast(discord.Guild, _FakeGuild(channel))
    try:
        first = await bot.run_daily_summary_for_guild(guild, date(2026, 8, 4))
        second = await bot.run_daily_summary_for_guild(guild, date(2026, 8, 4))

        assert first.status == "sent"
        assert second.status == "already_claimed"
        assert len(channel.sent) == 1
        assert await store.daily_summary_status(111, date(2026, 8, 4)) == "sent"
        receipts = await store.list_receipts(111)
        assert len(receipts) == 1
        assert (receipts[0].action, receipts[0].status, receipts[0].detail) == (
            "daily_health_summary",
            "succeeded",
            {"channel_id": 999, "snapshot_version": 1},
        )
    finally:
        await bot.close()
        await store.close()
