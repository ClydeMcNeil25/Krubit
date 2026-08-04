from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.install import phase_zero_permissions
from krubit.discord.inventory import capture_inventory


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeGuild:
    id = 111
    name = "Test Guild"
    roles: list[SimpleNamespace]
    channels: list[SimpleNamespace]
    scheduled_events: list[SimpleNamespace] = []
    me: SimpleNamespace

    def __init__(
        self,
        role_order: tuple[int, ...],
        *,
        forbidden: bool = False,
        granted: discord.Permissions | None = None,
    ) -> None:
        self.roles = [
            SimpleNamespace(
                id=role_id,
                name=f"Role {role_id}",
                position=role_id,
                color=SimpleNamespace(value=0),
                managed=False,
                permissions=discord.Permissions(view_channel=True),
            )
            for role_id in role_order
        ]
        self.channels = []
        self.me = SimpleNamespace(guild_permissions=granted or phase_zero_permissions())
        self._forbidden = forbidden

    def get_channel(self, channel_id: int) -> None:
        del channel_id
        return None

    async def webhooks(self) -> list[SimpleNamespace]:
        if self._forbidden:
            raise discord.Forbidden(cast(Any, FakeResponse()), "denied")
        return []

    async def fetch_automod_rules(self) -> list[SimpleNamespace]:
        if self._forbidden:
            raise discord.Forbidden(cast(Any, FakeResponse()), "denied")
        return []


@pytest.mark.asyncio
async def test_capture_inventory_sorts_resources_and_marks_forbidden_sections() -> None:
    capture = await capture_inventory(
        cast(discord.Guild, FakeGuild((20, 10), forbidden=True)),
        required_permissions=discord.Permissions(view_channel=True, view_audit_log=True),
        configured_channel_id=900,
    )

    roles = cast(list[dict[str, object]], capture.content["roles"])
    assert [role["id"] for role in roles] == ["10", "20"]
    assert capture.content["configured_channel"] == {"id": "900", "present": False}
    assert [(issue.section, issue.status) for issue in capture.coverage] == [
        ("automod_rules", "limited"),
        ("webhooks", "limited"),
    ]


@pytest.mark.asyncio
async def test_capture_inventory_is_stable_across_discord_resource_order() -> None:
    required = discord.Permissions(view_channel=True)
    first = await capture_inventory(
        cast(discord.Guild, FakeGuild((20, 10))),
        required_permissions=required,
        configured_channel_id=None,
    )
    second = await capture_inventory(
        cast(discord.Guild, FakeGuild((10, 20))),
        required_permissions=required,
        configured_channel_id=None,
    )

    assert first.content == second.content
    assert first.coverage == second.coverage == ()


@pytest.mark.asyncio
async def test_capture_inventory_identifies_unexpected_mutation_permissions() -> None:
    granted = phase_zero_permissions()
    granted.view_audit_log = True
    granted.manage_guild = True
    granted.kick_members = True

    capture = await capture_inventory(
        cast(discord.Guild, FakeGuild((10,), granted=granted)),
        required_permissions=discord.Permissions(view_channel=True, view_audit_log=True),
        configured_channel_id=None,
    )

    permissions = cast(dict[str, object], capture.content["bot_permissions"])
    assert permissions["unexpected_mutation"] == ["kick_members", "manage_guild"]
