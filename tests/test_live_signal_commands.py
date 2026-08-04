from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.bot import FetchCommands
from krubit.discord.live_commands import LiveCommands
from krubit.domain.live_signals import LiveSignalSession, LiveSignalStatus
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


class FakeResponse:
    def __init__(self) -> None:
        self.deferred: dict[str, bool] | None = None
        self.sent: dict[str, Any] | None = None

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.sent = {"content": content, "ephemeral": ephemeral}


class FakeMember:
    def __init__(self, *, manager: bool) -> None:
        self.id = 7
        self.guild_permissions = SimpleNamespace(manage_guild=manager)


class FakeInteraction:
    def __init__(self, *, manager: bool, guild_id: int | None = 111) -> None:
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.user = FakeMember(manager=manager)
        self.response = FakeResponse()
        self.edited_embed: discord.Embed | None = None
        self.edited_view: discord.ui.View | None = None
        self.role_calls: list[object] = []
        self.public_send_calls: list[object] = []

    async def edit_original_response(
        self, *, embed: discord.Embed, view: discord.ui.View | None = None
    ) -> None:
        self.edited_embed = embed
        self.edited_view = view


class FakeLiveService:
    def __init__(self) -> None:
        self.status_calls: list[int] = []
        self.health_calls: list[int] = []

    async def status(self, guild_id: int) -> tuple[LiveSignalSession, ...]:
        self.status_calls.append(guild_id)
        if guild_id != 111:
            return ()
        return (
            LiveSignalSession(
                guild_id=111,
                session_key="session-1",
                member_id=222,
                twitch_login="krucialstudios",
                twitch_url="https://twitch.tv/krucialstudios",
                status=LiveSignalStatus.LIVE,
                detected_at=datetime(2026, 8, 4, tzinfo=UTC),
                last_discord_at=datetime(2026, 8, 4, 1, tzinfo=UTC),
                last_twitch_at=datetime(2026, 8, 4, 2, tzinfo=UTC),
            ),
        )

    async def integration_health(self, guild_id: int) -> str:
        self.health_calls.append(guild_id)
        return "healthy" if guild_id == 111 else "limited"


class FakeReconcileCallback:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, guild: discord.Guild) -> int:
        self.calls.append(guild.id)
        return 2


def command(commands: LiveCommands, name: str) -> discord.app_commands.Command[Any, Any, Any]:
    return cast(
        discord.app_commands.Command[Any, Any, Any],
        next(item for item in commands.commands if item.name == name),
    )


async def invoke(
    registered: discord.app_commands.Command[Any, Any, Any],
    live: LiveCommands,
    interaction: FakeInteraction,
) -> None:
    await cast(Any, registered.callback)(cast(Any, live), cast(Any, interaction))


@pytest.fixture
async def commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LiveCommands, FakeLiveService, FakeReconcileCallback, SQLiteStore]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    monkeypatch.setattr("krubit.discord.bot.discord.Member", FakeMember)
    parent = FetchCommands(FoundationService(store))
    service = FakeLiveService()
    callback = FakeReconcileCallback()
    return LiveCommands(parent, service, callback), service, callback, store


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("status", "test", "reconcile"))
async def test_fetch_live_commands_require_a_guild_and_manage_guild(
    commands: tuple[LiveCommands, FakeLiveService, FakeReconcileCallback, SQLiteStore], name: str
) -> None:
    live, _, _, store = commands
    try:
        registered = command(live, name)
        assert registered.default_permissions is not None
        assert registered.default_permissions.manage_guild is True

        direct = FakeInteraction(manager=True, guild_id=None)
        await invoke(registered, live, direct)
        assert direct.response.sent == {
            "content": "This command is server-only.",
            "ephemeral": True,
        }

        denied = FakeInteraction(manager=False)
        await invoke(registered, live, denied)
        assert denied.response.sent is not None and denied.response.sent["ephemeral"] is True
        assert denied.response.deferred is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_live_status_is_guild_scoped_and_redacted(
    commands: tuple[LiveCommands, FakeLiveService, FakeReconcileCallback, SQLiteStore]
) -> None:
    live, service, _, store = commands
    try:
        interaction = FakeInteraction(manager=True)
        await invoke(command(live, "status"), live, interaction)

        assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
        assert service.status_calls == [111]
        assert service.health_calls == [111]
        assert interaction.edited_embed is not None
        values = [str(field.value) for field in interaction.edited_embed.fields]
        assert "2026-08-04T01:00:00+00:00" in values
        assert "2026-08-04T02:00:00+00:00" in values
        assert all("session-1" not in value and "krucialstudios" not in value for value in values)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_live_test_is_ephemeral_and_cannot_ping_or_mutate(
    commands: tuple[LiveCommands, FakeLiveService, FakeReconcileCallback, SQLiteStore]
) -> None:
    live, _, _, store = commands
    try:
        interaction = FakeInteraction(manager=True)
        await invoke(command(live, "test"), live, interaction)

        assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
        assert interaction.edited_embed is not None
        assert interaction.edited_view is not None
        assert interaction.role_calls == []
        assert interaction.public_send_calls == []
        assert "@everyone" not in (interaction.edited_embed.description or "")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_live_reconcile_calls_only_the_idempotent_callback_and_receipts_result(
    commands: tuple[LiveCommands, FakeLiveService, FakeReconcileCallback, SQLiteStore]
) -> None:
    live, _, callback, store = commands
    try:
        interaction = FakeInteraction(manager=True)
        await invoke(command(live, "reconcile"), live, interaction)

        assert callback.calls == [111]
        assert interaction.edited_embed is not None
        receipts = await store.list_receipts(111)
        assert [(item.action, item.detail) for item in receipts] == [
            ("fetch_live_reconcile", {"plans_applied": 2})
        ]
    finally:
        await store.close()
