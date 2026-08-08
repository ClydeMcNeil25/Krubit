from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from krubit.discord.bot import FetchCommands
from krubit.services.foundation import FoundationService
from krubit.storage.sqlite import SQLiteStore


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred: dict[str, bool] | None = None
        self.sent: dict[str, Any] | None = None

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.sent = {"content": content, "ephemeral": ephemeral}


class _FakeInteraction:
    def __init__(self, member: object) -> None:
        self.guild_id = 111
        self.guild = SimpleNamespace(id=111)
        self.user = member
        self.response = _FakeResponse()
        self.edited_embed: object | None = None

    async def edit_original_response(self, *, embed: object) -> None:
        self.edited_embed = embed


class _FakeMember:
    def __init__(self, member_id: int, *, can_manage_guild: bool) -> None:
        self.id = member_id
        self.guild_permissions = SimpleNamespace(manage_guild=can_manage_guild)


@pytest.mark.asyncio
async def test_fetch_status_is_staff_only_and_receipts_the_requesting_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    commands = FetchCommands(FoundationService(store))
    admin = next(command for command in commands.commands if command.name == "admin")
    status = next(command for command in admin.commands if command.name == "status")  # type: ignore[attr-defined]
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        assert status.default_permissions is not None
        assert status.default_permissions.manage_guild is True

        denied = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await status.callback(admin, denied)  # type: ignore[arg-type]

        assert denied.response.sent is not None
        assert denied.response.sent["ephemeral"] is True
        assert denied.response.deferred is None
        assert denied.edited_embed is None

        allowed = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await status.callback(admin, allowed)  # type: ignore[arg-type]

        assert allowed.response.deferred == {"ephemeral": True, "thinking": True}
        assert allowed.response.sent is None
        assert allowed.edited_embed is not None
        assert "Phase 1" in (allowed.edited_embed.description or "")  # type: ignore[union-attr]

        receipts = await store.list_receipts(111)
        assert [(item.action, item.status, item.actor_id) for item in receipts] == [
            ("fetch_status", "succeeded", 7),
            ("fetch_status", "denied", 42),
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_latest_is_open_to_any_guild_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    commands = FetchCommands(FoundationService(store))
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        assert commands.latest.default_permissions is None

        staff = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await commands.latest.callback(commands, staff)  # type: ignore[arg-type]

        assert staff.response.deferred == {"ephemeral": True, "thinking": True}
        assert staff.response.sent is None
        assert staff.edited_embed is not None

        member = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await commands.latest.callback(commands, member)  # type: ignore[arg-type]

        assert member.response.deferred == {"ephemeral": True, "thinking": True}
        assert member.response.sent is None
        assert member.edited_embed is not None

        receipts = await store.list_receipts(111)
        assert [(item.action, item.status, item.actor_id) for item in receipts] == [
            ("fetch_latest", "succeeded", 42),
            ("fetch_latest", "succeeded", 7),
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_latest_still_rejects_a_disabled_guild_regardless_of_staff_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    commands = FetchCommands(FoundationService(store))
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        staff = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await commands.latest.callback(commands, staff)  # type: ignore[arg-type]

        assert staff.response.sent is not None
        assert staff.response.sent["ephemeral"] is True
        assert staff.response.deferred is None
        assert staff.edited_embed is None

        member = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await commands.latest.callback(commands, member)  # type: ignore[arg-type]

        assert member.response.sent is not None
        assert member.response.sent["ephemeral"] is True
        assert member.response.deferred is None
        assert member.edited_embed is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_schedule_still_rejects_a_disabled_guild_regardless_of_staff_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    commands = FetchCommands(FoundationService(store))
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        staff = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await commands.schedule.callback(commands, staff)  # type: ignore[arg-type]

        assert staff.response.sent is not None
        assert staff.response.sent["ephemeral"] is True
        assert staff.response.deferred is None
        assert staff.edited_embed is None

        member = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await commands.schedule.callback(commands, member)  # type: ignore[arg-type]

        assert member.response.sent is not None
        assert member.response.sent["ephemeral"] is True
        assert member.response.deferred is None
        assert member.edited_embed is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fetch_schedule_is_open_to_any_guild_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    commands = FetchCommands(FoundationService(store))
    monkeypatch.setattr("krubit.discord.bot.discord.Member", _FakeMember)

    try:
        assert commands.schedule.default_permissions is None

        staff = _FakeInteraction(_FakeMember(7, can_manage_guild=True))
        await commands.schedule.callback(commands, staff)  # type: ignore[arg-type]

        assert staff.response.deferred == {"ephemeral": True, "thinking": True}
        assert staff.edited_embed is not None

        member = _FakeInteraction(_FakeMember(42, can_manage_guild=False))
        await commands.schedule.callback(commands, member)  # type: ignore[arg-type]

        assert member.response.deferred == {"ephemeral": True, "thinking": True}
        assert member.edited_embed is not None

        receipts = await store.list_receipts(111)
        assert [(item.action, item.status, item.actor_id) for item in receipts] == [
            ("fetch_schedule", "succeeded", 42),
            ("fetch_schedule", "succeeded", 7),
        ]
    finally:
        await store.close()
