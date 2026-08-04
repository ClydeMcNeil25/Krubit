from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from krubit.config import Settings
from krubit.discord.bot import KrubitBot
from krubit.discord.events import guild_event
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


def test_guild_event_id_is_deterministic_for_gateway_replays() -> None:
    occurred = datetime(2026, 8, 4, 1, 2, tzinfo=UTC)
    event = guild_event(
        "role_updated", 111, 222, occurred, {"name": "A"}, {"name": "B"}
    )
    replay = guild_event(
        "role_updated", 111, 222, occurred, {"name": "A"}, {"name": "B"}
    )

    assert event.event_id == replay.event_id
    assert event.payload == {
        "entity_id": "222",
        "before": {"name": "A"},
        "after": {"name": "B"},
    }


@pytest.mark.asyncio
async def test_bot_collects_guild_and_automod_rule_changes(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))
    before_guild = cast(discord.Guild, SimpleNamespace(id=111, name="Before"))
    after_guild = cast(discord.Guild, SimpleNamespace(id=111, name="After"))
    rule = cast(
        discord.AutoModRule,
        SimpleNamespace(id=222, guild=SimpleNamespace(id=111), name="Links", enabled=True),
    )
    changed_rule = cast(
        discord.AutoModRule,
        SimpleNamespace(id=222, guild=SimpleNamespace(id=111), name="Links 2", enabled=False),
    )
    execution = cast(
        discord.AutoModAction,
        SimpleNamespace(guild_id=111, rule_id=222, user_id=333, channel_id=444),
    )
    try:
        await bot.on_guild_update(before_guild, after_guild)
        await bot.on_automod_rule_create(rule)
        await bot.on_automod_rule_update(rule, changed_rule)
        await bot.on_automod_action(execution)
        await bot.on_automod_rule_delete(changed_rule)

        events = await store.list_events(111)
        assert {event.event_type for event in events} == {
            "guild_updated",
            "automod_rule_created",
            "automod_rule_updated",
            "automod_action_executed",
            "automod_rule_deleted",
        }
    finally:
        await bot.close()
        await store.close()
