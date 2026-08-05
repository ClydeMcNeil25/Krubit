import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest
from discord import app_commands

import krubit.__main__ as cli
from krubit.__main__ import main
from krubit.config import Settings
from krubit.discord.bot import KrubitBot
from krubit.domain.creator_signals import CreatorAccount, Platform
from krubit.integrations.base import ConnectorAccount, ConnectorHealth, ConnectorPage
from krubit.integrations.catalog import CATALOG
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


def environment(database_path: Path) -> dict[str, str]:
    return {
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "KRUBIT_DATABASE_PATH": str(database_path),
    }


def test_cli_initializes_enables_and_reports_guild_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "krubit.db"
    env = environment(database_path)

    assert main(["init-db"], env) == 0
    assert main(["enable-guild", "111"], env) == 0
    assert main(["status", "111"], env) == 0

    output = capsys.readouterr().out.strip().splitlines()
    status = json.loads(output[-1])
    assert status == {
        "database_healthy": True,
        "enabled": True,
        "event_count": 0,
        "guild_id": "111",
        "receipt_count": 1,
    }


def test_cli_prints_install_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install-url"], environment(tmp_path / "krubit.db")) == 0

    output = capsys.readouterr().out.strip()
    assert output.startswith("https://discord.com/oauth2/authorize?client_id=123456789012345678")


def test_cli_emits_schema_valid_signal_without_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "krubit.db"
    env = environment(database_path)
    main(["init-db"], env)
    main(["enable-guild", "111"], env)

    assert main(["emit-test-signal", "111", "--actor-id", "7"], env) == 0

    output = capsys.readouterr().out.strip().splitlines()
    signal = json.loads(output[-1])
    assert signal["schema_version"] == "krubit.zariya-signal.v1"
    assert signal["guild_id"] == "111"
    assert signal["action_request"] is None


@pytest.mark.asyncio
async def test_bot_registers_phase_one_fetch_commands(tmp_path: Path) -> None:
    settings = Settings.from_env(environment(tmp_path / "krubit.db"))
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))

    try:
        fetch = cast(
            app_commands.Group,
            next(command for command in bot.tree.get_commands() if command.name == "fetch"),
        )

        assert {command.name for command in fetch.commands} == {
            "status",
            "test-card",
            "server-health",
            "changes",
            "permissions",
            "integrations",
            "backup",
            "live",
            "creator",
            "notifications",
            "latest",
            "schedule",
        }
        backup = cast(
            app_commands.Group,
            next(command for command in fetch.commands if command.name == "backup"),
        )
        assert {command.name for command in backup.commands} == {
            "status",
            "create",
            "preview",
        }
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_bot_records_guild_installed_while_runtime_is_connected(tmp_path: Path) -> None:
    settings = Settings.from_env(environment(tmp_path / "krubit.db"))
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    bot = KrubitBot(settings, FoundationService(store))
    guild = cast(discord.Guild, SimpleNamespace(id=111, name="Krucial Town"))

    try:
        await bot.on_guild_join(guild)

        event_count, _ = await store.counts(111)
        assert event_count == 1
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_bot_uses_presence_intent_while_twitch_remains_optional(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db"),
        FoundationService(store),
        twitch=None,
    )

    try:
        assert bot.intents.presences is True
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_run_bot_closes_every_resource_when_twitch_constructor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class FakeConnector:
        async def close(self) -> None:
            closed.append("connector")

    class FakeSession:
        def __init__(self, *, connector: FakeConnector) -> None:
            self.connector = connector

        async def close(self) -> None:
            closed.append("session")

    def fail_twitch(session: FakeSession, client_id: str, client_secret: str) -> object:
        raise RuntimeError("constructor failure")

    def connector_factory(**kwargs: object) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(cli.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(cli, "TwitchHelixClient", fail_twitch)
    settings = Settings(
        application_id=123,
        database_path=tmp_path / "krubit.db",
        bot_token="token",
        twitch_client_id="client",
        twitch_client_secret="secret",
        live_signals_enabled=True,
    )

    with pytest.raises(RuntimeError, match="constructor failure"):
        await cli._run_bot(settings)  # pyright: ignore[reportPrivateUsage]

    assert closed == ["session", "connector"]


@pytest.mark.asyncio
async def test_run_bot_preserves_start_error_and_closes_every_owned_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class FakeStore:
        async def initialize(self) -> None:
            return None

        async def list_live_signal_guild_ids(self) -> list[int]:
            return []

        async def close(self) -> None:
            closed.append("store")

    class FakeConnector:
        async def close(self) -> None:
            closed.append("connector")

    class FakeSession:
        def __init__(self, *, connector: FakeConnector) -> None:
            self.connector = connector

        async def close(self) -> None:
            closed.append("session")

    class FakeBot:
        def __init__(self, *args: object, connector: FakeConnector, **kwargs: object) -> None:
            self.connector = connector

        async def start(self, token: str) -> None:
            raise RuntimeError("start failure")

        async def close(self) -> None:
            closed.append("bot")
            raise RuntimeError("close failure")

    async def open_store(path: Path) -> FakeStore:
        return FakeStore()

    def connector_factory(**kwargs: object) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(cli.SQLiteStore, "open", open_store)
    monkeypatch.setattr(cli.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(cli, "KrubitBot", FakeBot)
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db", bot_token="token")

    with pytest.raises(RuntimeError, match="start failure"):
        await cli._run_bot(settings)  # pyright: ignore[reportPrivateUsage]

    assert closed == ["bot", "store", "connector", "session", "connector"]


@pytest.mark.asyncio
async def test_enabled_live_runtime_loop_is_cancelled_by_bot_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OfflineTwitch:
        async def get_stream(self, login: str) -> object:
            raise AssertionError("reconciliation should not contact Twitch")

    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            live_signals_enabled=True,
        ),
        FoundationService(store),
        twitch=OfflineTwitch(),  # type: ignore[arg-type]
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sync() -> None:
        return None

    async def ready() -> None:
        return None

    async def reconcile(guilds: object) -> int:
        entered.set()
        await release.wait()
        return 0

    monkeypatch.setattr(bot.tree, "sync", sync)
    monkeypatch.setattr(bot, "wait_until_ready", ready)
    monkeypatch.setattr(
        bot._live_runtime,  # pyright: ignore[reportPrivateUsage]
        "reconcile_all",
        reconcile,
    )
    try:
        await bot.setup_hook()
        await entered.wait()
        task = bot.live_signal_reconciliation.get_task()
        assert task is not None and not task.done()

        await bot.close()
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert bot.daily_health_summary.get_task() is not None
    finally:
        release.set()
        await store.close()


class _InertConnector:
    """Enough of `Connector` to enable `content_scheduler_cycle`; never actually called."""

    descriptor = CATALOG[Platform.BLUESKY]

    async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
        raise NotImplementedError

    async def fetch_page(
        self, account: CreatorAccount, *, cursor: str | None
    ) -> ConnectorPage:  # pragma: no cover
        raise NotImplementedError

    async def health(
        self, account: CreatorAccount | None = None
    ) -> ConnectorHealth:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_content_scheduler_cycle_survives_an_unhandled_exception_and_runs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception escaping `run_cycle` must never stop the loop permanently.

    `discord.ext.tasks.Loop` only auto-retries a narrow set of reconnect-worthy
    exceptions; anything else stops the loop until the process restarts unless the
    loop body itself catches it. This proves `KrubitBot.content_scheduler_cycle`
    survives an arbitrary unhandled exception from `ConnectorScheduler.run_cycle` and
    still fires again on its next tick, rather than silently going dark.
    """
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db"),
        FoundationService(store),
        content_connectors={Platform.BLUESKY: _InertConnector()},
    )
    calls = 0
    ran_twice = asyncio.Event()

    async def flaky_run_cycle() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated cycle failure")
        ran_twice.set()

    async def sync() -> None:
        return None

    async def ready() -> None:
        return None

    monkeypatch.setattr(bot.tree, "sync", sync)
    monkeypatch.setattr(bot, "wait_until_ready", ready)
    monkeypatch.setattr(
        bot._content_scheduler,  # pyright: ignore[reportPrivateUsage]
        "run_cycle",
        flaky_run_cycle,
    )
    bot.content_scheduler_cycle.change_interval(seconds=0.01)

    try:
        await bot.setup_hook()
        await asyncio.wait_for(ran_twice.wait(), timeout=5)

        assert calls >= 2
        task = bot.content_scheduler_cycle.get_task()
        assert task is not None and not task.done()
    finally:
        await bot.close()
        await store.close()
