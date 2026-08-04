from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.live_runtime import (
    LIVE_CHANNEL_NAME,
    STREAMING_ROLE_NAME,
    LiveSignalRuntime,
)
from krubit.domain.live_signals import (
    LiveSignalAction,
    LiveSignalPlan,
    StreamingObservation,
    TwitchLookup,
    TwitchLookupKind,
    TwitchStream,
)
from krubit.services.live_signals import LiveSignalService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)


class FakeRole:
    def __init__(self, role_id: int, name: str, position: int) -> None:
        self.id = role_id
        self.name = name
        self.position = position


class FakeMember:
    def __init__(self, member_id: int, roles: list[FakeRole], *, bot: bool = False) -> None:
        self.id = member_id
        self.roles = roles
        self.bot = bot
        self.guild: object | None = None
        self.activities: tuple[object, ...] = ()
        self.display_name = "Krucial Studios"
        self.added_roles: list[FakeRole] = []
        self.removed_roles: list[FakeRole] = []
        self.add_failure = False

    async def add_roles(self, role: FakeRole, *, reason: str) -> None:
        if self.add_failure:
            raise PermissionError("role denied")
        self.added_roles.append(role)
        self.roles.append(role)

    async def remove_roles(self, role: FakeRole, *, reason: str) -> None:
        self.removed_roles.append(role)
        self.roles.remove(role)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class FakeTextChannel:
    def __init__(
        self, channel_id: int, name: str, *, mention_everyone: bool = True
    ) -> None:
        self.id = channel_id
        self.name = name
        self.permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            mention_everyone=mention_everyone,
        )
        self.sent: list[dict[str, object]] = []
        self.messages: dict[int, FakeMessage] = {}
        self.history_failure: BaseException | None = None

    def permissions_for(self, member: object) -> object:
        return self.permissions

    async def send(self, **kwargs: object) -> FakeMessage:
        self.sent.append(kwargs)
        message = FakeMessage(1000 + len(self.sent))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.messages[message_id]

    async def history(self, *, limit: int) -> AsyncIterator[FakeMessage]:
        if self.history_failure is not None:
            raise self.history_failure
        for message in list(self.messages.values())[-limit:]:
            yield message


class FakeGuild:
    def __init__(
        self,
        channel: FakeTextChannel,
        role: FakeRole,
        member: FakeMember,
        *,
        can_manage_roles: bool = True,
        top_role_position: int = 10,
    ) -> None:
        self.id = 111
        self.name = "Krucial Town"
        self.channels = [channel]
        self.roles = [role]
        self.channel = channel
        self.role = role
        self.member = member
        member.guild = self
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=can_manage_roles),
            top_role=FakeRole(1, "Krubit", top_role_position),
        )

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        return self.channel if channel_id == self.channel.id else None

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.role if role_id == self.role.id else None

    def get_member(self, member_id: int) -> FakeMember | None:
        return self.member if member_id == self.member.id else None


class ScriptedTwitch:
    def __init__(self, results: list[TwitchLookup]) -> None:
        self.results = results

    async def get_stream(self, login: str) -> TwitchLookup:
        return self.results.pop(0)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[
    LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService
]]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    channel = FakeTextChannel(444, LIVE_CHANNEL_NAME)
    role = FakeRole(333, STREAMING_ROLE_NAME, 5)
    member_role = FakeRole(7, "Member", 1)
    member = FakeMember(222, [member_role])
    guild = FakeGuild(channel, role, member)
    service = LiveSignalService(store, ScriptedTwitch([live_lookup(), offline_lookup()]))
    runtime = LiveSignalRuntime(store, service, now=lambda: NOW)
    try:
        yield runtime, store, guild, service
    finally:
        await store.close()


def live_lookup() -> TwitchLookup:
    return TwitchLookup(
        TwitchLookupKind.LIVE,
        TwitchStream(
            stream_id="stream-1",
            user_login="krucialstudios",
            user_name="Krucial Studios",
            title="Building Krucial Town",
            game_name="Just Chatting",
            started_at=NOW,
            thumbnail_url="https://cdn.twitch.tv/preview.jpg",
        ),
    )


def offline_lookup() -> TwitchLookup:
    return TwitchLookup(TwitchLookupKind.OFFLINE)


def observation() -> StreamingObservation:
    return StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=NOW,
        observed_at=NOW,
    )


def as_guild(guild: FakeGuild) -> discord.Guild:
    return cast(discord.Guild, guild)


@pytest.mark.asyncio
async def test_configure_bootstraps_exact_names_then_keeps_ids_after_rename(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, _ = runtime

    configured = await executor.configure_guild(as_guild(guild))
    guild.channel.name = "renamed-channel"
    guild.role.name = "renamed-role"

    assert configured is not None
    assert (configured.channel_id, configured.role_id) == (444, 333)
    assert await executor.configure_guild(as_guild(guild)) == configured
    assert await store.get_live_signal_config(111) == configured


@pytest.mark.asyncio
async def test_configure_requires_manage_roles_and_usable_role_before_persisting(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, _ = runtime
    guild.me.guild_permissions.manage_roles = False

    assert await executor.configure_guild(as_guild(guild)) is None
    assert await store.get_live_signal_config(111) is None


@pytest.mark.asyncio
async def test_apply_plan_adds_only_the_dedicated_role_and_announces_once(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)

    await executor.apply_plan(as_guild(guild), plan)
    await executor.apply_plan(as_guild(guild), plan)

    assert [role.id for role in guild.member.added_roles] == [333]
    assert [role.id for role in guild.member.roles] == [7, 333]
    assert len(guild.channel.sent) == 1
    sent = guild.channel.sent[0]
    assert sent["allowed_mentions"].everyone is True  # type: ignore[union-attr]
    saved = await store.get_live_session(111, plan.session_key)
    assert saved is not None and saved.role_assigned_by_krubit is True


@pytest.mark.asyncio
async def test_apply_plan_records_preexisting_role_as_not_owned_and_never_removes_it(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, service = runtime
    guild.member.roles.append(guild.role)
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)

    await executor.apply_plan(as_guild(guild), plan)
    ended = await service.reconcile(111, now=NOW)
    for removal in ended:
        await executor.apply_plan(as_guild(guild), removal)

    saved = await store.get_live_session(111, plan.session_key)
    assert saved is not None and saved.role_assigned_by_krubit is False
    assert guild.member.added_roles == []
    assert guild.member.removed_roles == []


@pytest.mark.asyncio
async def test_apply_plan_removes_only_the_exact_owned_configured_role(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, _, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)
    await executor.apply_plan(as_guild(guild), plan)
    ended = await service.reconcile(111, now=NOW)

    assert len(ended) == 1
    await executor.apply_plan(as_guild(guild), ended[0])
    assert [role.id for role in guild.member.removed_roles] == [333]
    assert [role.id for role in guild.member.roles] == [7]


@pytest.mark.asyncio
async def test_announcement_without_mention_permission_is_degraded_without_implicit_mentions(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, _, guild, service = runtime
    guild.channel.permissions.mention_everyone = False
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)

    await executor.apply_plan(as_guild(guild), plan)

    allowed = guild.channel.sent[0]["allowed_mentions"]
    assert allowed.everyone is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_edit_plan_changes_only_embed_and_view(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, _, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)
    await executor.apply_plan(as_guild(guild), plan)
    message = guild.channel.messages[1001]
    edit = LiveSignalPlan(
        guild_id=111,
        session_key=plan.session_key,
        actions=(LiveSignalAction.EDIT_ANNOUNCEMENT,),
        stream=plan.stream,
        member_id=222,
        announcement_channel_id=444,
        announcement_message_id=1001,
    )

    await executor.apply_plan(as_guild(guild), edit)

    assert len(message.edits) == 1
    assert set(message.edits[0]) == {"embed", "view"}


@pytest.mark.asyncio
async def test_disabled_guild_or_runtime_never_mutates_or_sends(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, service = runtime
    await store.set_guild_enabled(111, False)
    plan = await service.observe(observation(), now=NOW)

    await executor.apply_plan(as_guild(guild), plan)

    assert guild.member.added_roles == []
    assert guild.channel.sent == []


@pytest.mark.asyncio
async def test_reconcile_all_applies_plans_without_retaining_background_tasks(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, _, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    plan = await service.observe(observation(), now=NOW)
    await executor.apply_plan(as_guild(guild), plan)

    assert await executor.reconcile_all([as_guild(guild)]) == 1


@pytest.mark.asyncio
async def test_presence_role_failure_blocks_enrichment_then_recovery_announces_once(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, _ = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    guild.member.activities = (
        SimpleNamespace(
            type=discord.ActivityType.streaming,
            url="https://twitch.tv/krucialstudios",
            start=NOW,
        ),
    )
    guild.member.add_failure = True

    await executor.handle_presence(as_member(guild.member), as_member(guild.member))

    saved = (await store.list_active_live_sessions(111))[0]
    assert guild.channel.sent == []
    assert saved.stream is None and saved.role_id is None
    guild.member.add_failure = False

    await executor.reconcile_guild(as_guild(guild))

    assert [role.id for role in guild.member.added_roles] == [333]
    assert len(guild.channel.sent) == 1


@pytest.mark.asyncio
async def test_recovered_history_forbidden_keeps_claim_pending_without_sending(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    begun = await service.begin_presence(observation(), now=NOW)
    await service.record_role_result(
        111, begun.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )
    announced = await service.enrich_presence(observation(), now=NOW)
    guild.channel.history_failure = discord.Forbidden(
        cast(Any, SimpleNamespace(status=403, reason="forbidden", headers={})), "forbidden"
    )
    recovery = (await service.recover_pending(111))[0]

    await executor.apply_plan(as_guild(guild), recovery)

    assert announced.stream is not None
    delivery = await store.get_live_delivery(111, f"stream:{announced.stream.stream_id}")
    assert guild.channel.sent == []
    assert delivery is not None and delivery.status == "claimed"
    guild.channel.history_failure = None
    await executor.apply_plan(as_guild(guild), recovery)
    assert len(guild.channel.sent) == 1


@pytest.mark.asyncio
async def test_reconcile_defers_same_session_announcement_after_recovered_role_failure(
    runtime: tuple[LiveSignalRuntime, SQLiteStore, FakeGuild, LiveSignalService],
) -> None:
    executor, store, guild, service = runtime
    assert await executor.configure_guild(as_guild(guild)) is not None
    begun = await service.begin_presence(observation(), now=NOW)
    guild.member.add_failure = True

    await executor.reconcile_guild(as_guild(guild))
    await executor.reconcile_guild(as_guild(guild))

    saved = await store.get_live_session(111, begun.session_key)
    assert saved is not None and saved.role_id is None
    assert guild.channel.sent == []
    guild.member.add_failure = False

    await executor.reconcile_guild(as_guild(guild))
    await executor.reconcile_guild(as_guild(guild))

    assert [role.id for role in guild.member.added_roles] == [333]
    assert len(guild.channel.sent) == 1


def as_member(member: FakeMember) -> discord.Member:
    return cast(discord.Member, member)
