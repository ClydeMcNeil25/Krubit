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


@pytest.mark.asyncio
async def test_on_automod_action_is_a_no_op_for_watchdog_when_watchdog_disabled(
    tmp_path: Path,
) -> None:
    """Critical #3: `on_automod_action`'s Watchdog block (the `list_open_watch_windows`
    read plus the `automod_action_correlated` sniff-receipt write) must be gated on
    `watchdog_enabled` like every other Watchdog data-producing path in
    `WatchdogRuntime`. Regression test for the bug where this handler wrote a
    member-linked `sniff_receipts` row unconditionally, contradicting the ops doc's
    claim that disabling the flag means no Watchdog activity.

    Uses a real `discord.AutoModRuleTriggerType` so `correlate_automod_action` would
    return a non-`None` signal if it ran at all -- proving the block was skipped, not
    merely that it produced no signal.
    """
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    settings = Settings(
        application_id=123, database_path=tmp_path / "krubit.db", watchdog_enabled=False
    )
    bot = KrubitBot(settings, FoundationService(store))
    execution = cast(
        discord.AutoModAction,
        SimpleNamespace(
            guild_id=111,
            rule_id=222,
            user_id=333,
            channel_id=444,
            rule_trigger_type=discord.AutoModRuleTriggerType.spam,
            matched_keyword=None,
            matched_content=None,
        ),
    )
    try:
        await bot.on_automod_action(execution)

        # The Phase-1-style change ingestion (predates Watchdog) still happens.
        events = await store.list_events(111)
        assert {event.event_type for event in events} == {"automod_action_executed"}

        # But no Watchdog-specific sniff receipt was written.
        receipts = await store.list_sniff_receipts(111)
        assert all(receipt.action != "automod_action_correlated" for receipt in receipts)
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_on_automod_action_correlates_when_watchdog_enabled(tmp_path: Path) -> None:
    """Contrast case for the test above: with `watchdog_enabled=True`, the same
    payload does produce an `automod_action_correlated` sniff receipt, proving the
    gate added for Critical #3 disables the block precisely (not the whole handler).
    """
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    settings = Settings(
        application_id=123, database_path=tmp_path / "krubit.db", watchdog_enabled=True
    )
    bot = KrubitBot(settings, FoundationService(store))
    execution = cast(
        discord.AutoModAction,
        SimpleNamespace(
            guild_id=111,
            rule_id=222,
            user_id=333,
            channel_id=444,
            rule_trigger_type=discord.AutoModRuleTriggerType.spam,
            matched_keyword=None,
            matched_content=None,
        ),
    )
    try:
        await bot.on_automod_action(execution)

        receipts = await store.list_sniff_receipts(111)
        assert any(receipt.action == "automod_action_correlated" for receipt in receipts)
    finally:
        await bot.close()
        await store.close()
