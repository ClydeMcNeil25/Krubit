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
