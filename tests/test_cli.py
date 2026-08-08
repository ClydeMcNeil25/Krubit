import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest
from discord import app_commands

import krubit.__main__ as cli
from krubit.__main__ import main
from krubit.config import Settings
from krubit.discord.bot import FetchCommands, KrubitBot
from krubit.discord.content_commands import NotificationCommands
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
            "backup",
            "live",
            "creator",
            "notifications",
            "latest",
            "schedule",
            "sniff",
            "activity",
            "milestones",
            "retention",
            "community-pulse",
            "admin",
            "member-export",
        }
        sniff = cast(
            app_commands.Group,
            next(command for command in fetch.commands if command.name == "sniff"),
        )
        assert {command.name for command in sniff.commands} == {
            "member",
            "report",
            "incident",
            "evidence",
            "watchlist",
        }
        admin = cast(
            app_commands.Group,
            next(command for command in fetch.commands if command.name == "admin"),
        )
        assert {command.name for command in admin.commands} == {
            "status",
            "test-card",
            "server-health",
            "changes",
            "permissions",
            "integrations",
            "member",
            "newcomers",
            "inactive",
            "returning",
            "recognition-candidates",
            "member-delete",
            "exclude-channel",
            "exclusions",
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
        creator = cast(
            app_commands.Group,
            next(command for command in fetch.commands if command.name == "creator"),
        )
        # Final-review Important #8: `/fetch creator transfer` must actually be
        # registered — the operator runbook documents it as available.
        assert {command.name for command in creator.commands} == {
            "add",
            "pause",
            "resume",
            "remove",
            "list",
            "show",
            "verify",
            "route",
            "transfer",
            "template",
        }
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_bot_resolves_configured_inactivity_threshold_for_fetch_commands(
    tmp_path: Path,
) -> None:
    """`KrubitBot` must genuinely read `Settings.activity_ledger_inactivity_threshold_days`
    and thread it into the `FetchCommands` group that backs `/fetch inactive`/
    `/fetch activity` -- not silently ignore it, matching Task 7's review finding
    that `activity_ledger_retention_days` was parsed but never wired anywhere."""
    env = environment(tmp_path / "krubit.db")
    env["KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS"] = "21"
    settings = Settings.from_env(env)
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))

    try:
        fetch = cast(
            FetchCommands,
            next(command for command in bot.tree.get_commands() if command.name == "fetch"),
        )
        assert fetch.inactivity_threshold == timedelta(days=21)
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_bot_falls_back_to_the_default_inactivity_threshold_when_unset(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(environment(tmp_path / "krubit.db"))
    assert settings.activity_ledger_inactivity_threshold_days is None
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))

    try:
        fetch = cast(
            FetchCommands,
            next(command for command in bot.tree.get_commands() if command.name == "fetch"),
        )
        assert fetch.inactivity_threshold == timedelta(days=14)
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
async def test_bot_requests_message_content_only_when_watchdog_enabled(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    disabled_bot = KrubitBot(
        Settings(
            application_id=123, database_path=tmp_path / "krubit.db", watchdog_enabled=False
        ),
        FoundationService(store),
    )
    enabled_bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db", watchdog_enabled=True),
        FoundationService(store),
    )

    try:
        assert disabled_bot.intents.message_content is False
        assert enabled_bot.intents.message_content is True
    finally:
        await disabled_bot.close()
        await enabled_bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_bot_request_message_content_intent_override_forces_no_privileged_intent(
    tmp_path: Path,
) -> None:
    """The fallback path `_run_bot` uses after `PrivilegedIntentsRequired`: even with
    `watchdog_enabled=True`, passing `request_message_content_intent=False` must not
    request the privileged intent, and `WatchdogRuntime` must see that honestly (its
    `message_content_available` reflects what was actually requested, not the
    settings flag)."""
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(application_id=123, database_path=tmp_path / "krubit.db", watchdog_enabled=True),
        FoundationService(store),
        request_message_content_intent=False,
    )

    try:
        assert bot.intents.message_content is False
        assert (
            bot._watchdog_runtime._message_content_available  # pyright: ignore[reportPrivateUsage]
            is False
        )
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

    # The dedicated OAuth session/connector are constructed and started before
    # `bot.start()` is ever called (see `_run_bot`'s ordering requirements), so
    # they close first in the `finally` block, ahead of `bot`/`store`/the bot's
    # own connector; `callback_server` itself is a real `CallbackServer` here
    # (not faked) and its `close()` is a no-op since it was never enabled/started.
    assert closed == ["session", "connector", "bot", "store", "connector", "session", "connector"]


@pytest.mark.asyncio
async def test_run_bot_degrades_honestly_when_message_content_intent_is_not_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design doc is explicit: "until enabled, Krubit degrades to join-signal-only
    detection ... rather than failing to start." `discord.PrivilegedIntentsRequired`
    raised from `bot.start()` when `KRUBIT_WATCHDOG_ENABLED=true` but Message Content
    is not yet enabled in the Discord Developer Portal must NOT crash the whole
    process -- `_run_bot` must catch it, reconnect once without the privileged intent,
    and continue running. This exercises the real `_run_bot` retry path (not just a
    unit-level flag on `WatchdogRuntime`), proving `request_message_content_intent`
    genuinely reaches the second `KrubitBot` construction.
    """
    closed: list[str] = []
    constructions: list[bool | None] = []

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
        def __init__(
            self,
            *args: object,
            connector: FakeConnector,
            request_message_content_intent: bool | None = None,
            **kwargs: object,
        ) -> None:
            self.connector = connector
            self.request_message_content_intent = request_message_content_intent
            constructions.append(request_message_content_intent)

        async def start(self, token: str) -> None:
            # The first construction (no override -> defaults to
            # `settings.watchdog_enabled`, i.e. True here) simulates the privileged
            # intent not being granted yet. The retry construction explicitly passes
            # `request_message_content_intent=False` and must succeed.
            if self.request_message_content_intent is not False:
                raise discord.PrivilegedIntentsRequired(shard_id=None)

        async def close(self) -> None:
            closed.append("bot")

    async def open_store(path: Path) -> FakeStore:
        return FakeStore()

    def connector_factory(**kwargs: object) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(cli.SQLiteStore, "open", open_store)
    monkeypatch.setattr(cli.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(cli, "KrubitBot", FakeBot)
    settings = Settings(
        application_id=123,
        database_path=tmp_path / "krubit.db",
        bot_token="token",
        watchdog_enabled=True,
    )

    result = await cli._run_bot(settings)  # pyright: ignore[reportPrivateUsage]

    assert result == 0  # must NOT crash the process
    assert constructions == [None, False]
    # Both bot instances (the failed one and the successful retry) were closed.
    assert closed.count("bot") == 2


@pytest.mark.asyncio
async def test_run_bot_reraises_privileged_intents_error_when_watchdog_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`watchdog_enabled=False` never requests the privileged intent in the first
    place (see `KrubitBot.__init__`), so a `PrivilegedIntentsRequired` in that
    configuration reflects some other privileged intent (e.g. `members`/`presences`)
    already required by every phase -- there is nothing safe to retry without, so it
    must propagate rather than be silently swallowed."""
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
            raise discord.PrivilegedIntentsRequired(shard_id=None)

        async def close(self) -> None:
            closed.append("bot")

    async def open_store(path: Path) -> FakeStore:
        return FakeStore()

    def connector_factory(**kwargs: object) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(cli.SQLiteStore, "open", open_store)
    monkeypatch.setattr(cli.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(cli, "KrubitBot", FakeBot)
    settings = Settings(
        application_id=123,
        database_path=tmp_path / "krubit.db",
        bot_token="token",
        watchdog_enabled=False,
    )

    with pytest.raises(discord.PrivilegedIntentsRequired):
        await cli._run_bot(settings)  # pyright: ignore[reportPrivateUsage]

    assert closed.count("bot") == 1


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
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            creator_signals_enabled=True,
        ),
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


@pytest.mark.asyncio
async def test_content_scheduler_never_runs_when_creator_signals_disabled(
    tmp_path: Path,
) -> None:
    """Final-review Critical #1: `KRUBIT_CREATOR_SIGNALS_ENABLED=false` (the default)
    must mean the connector polling scheduler never starts, even if a caller supplies
    ready-to-poll connectors — the flag, not just the presence of connectors, is the
    gate."""
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            creator_signals_enabled=False,
        ),
        FoundationService(store),
        content_connectors={Platform.BLUESKY: _InertConnector()},
    )

    try:
        assert bot._content_connectors == {}  # pyright: ignore[reportPrivateUsage]
        assert bot._content_scheduler_enabled is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await bot.close()
        await store.close()


@pytest.mark.asyncio
async def test_run_bot_never_builds_content_connectors_when_creator_signals_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same gate applies at `_run_bot`'s call site, not just inside `KrubitBot`."""
    build_called = False
    received_connectors: dict[object, object] | None = None

    class FakeStore:
        async def initialize(self) -> None:
            return None

        async def list_live_signal_guild_ids(self) -> list[int]:
            return []

        async def close(self) -> None:
            return None

    class FakeConnector:
        async def close(self) -> None:
            return None

    class FakeSession:
        def __init__(self, *, connector: FakeConnector) -> None:
            self.connector = connector

        async def close(self) -> None:
            return None

    class FakeBot:
        def __init__(self, *args: object, content_connectors: object, **kwargs: object) -> None:
            nonlocal received_connectors
            received_connectors = cast(dict[object, object], content_connectors)

        async def start(self, token: str) -> None:
            raise RuntimeError("stop before real Discord connect")

        async def close(self) -> None:
            return None

    def spy_build(*args: object, **kwargs: object) -> dict[object, object]:
        nonlocal build_called
        build_called = True
        return {}

    async def open_store(path: Path) -> FakeStore:
        return FakeStore()

    def connector_factory(**kwargs: object) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(cli.SQLiteStore, "open", open_store)
    monkeypatch.setattr(cli.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(cli, "KrubitBot", FakeBot)
    monkeypatch.setattr(cli, "_build_content_connectors", spy_build)
    settings = Settings(
        application_id=123,
        database_path=tmp_path / "krubit.db",
        bot_token="token",
        creator_signals_enabled=False,
    )

    with pytest.raises(RuntimeError, match="stop before real Discord connect"):
        await cli._run_bot(settings)  # pyright: ignore[reportPrivateUsage]

    assert build_called is False
    assert received_connectors == {}


@pytest.mark.asyncio
async def test_notification_commands_share_the_bots_content_runtime(tmp_path: Path) -> None:
    """Final-review Critical #1 composed consequence: `ContentCommandService`'s own
    `ContentRuntime` (used by `/fetch notifications retry|retract`) must be the exact
    same instance `KrubitBot` polls into, so it honors `social_delivery_enabled` too —
    not a second, independently-defaulted `ContentRuntime` that could bypass shadow
    mode."""
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    bot = KrubitBot(
        Settings(
            application_id=123,
            database_path=tmp_path / "krubit.db",
            social_delivery_enabled=False,
        ),
        FoundationService(store),
    )
    try:
        fetch = cast(
            app_commands.Group,
            next(command for command in bot.tree.get_commands() if command.name == "fetch"),
        )
        notifications = cast(
            NotificationCommands,
            next(command for command in fetch.commands if command.name == "notifications"),
        )
        service = notifications._service  # pyright: ignore[reportPrivateUsage]
        assert service._runtime is bot._content_runtime  # pyright: ignore[reportPrivateUsage]
        assert (
            service._runtime._social_delivery_enabled  # pyright: ignore[reportPrivateUsage]
            is False
        )
    finally:
        await bot.close()
        await store.close()
