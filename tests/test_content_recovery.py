"""End-to-end scheduler -> ledger -> Discord recovery scenarios for Task 13.

Covers what `tests/test_content_runtime.py` (Task 6) does not: content that arrives
through `ConnectorScheduler.run_cycle` rather than a hand-built `ConnectorPage`, a
delivery that is deliberately queued by policy and only released by a later
`recover_pending` sweep, and a scheduler cycle spanning a simulated restart that still
delivers exactly once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import discord
import pytest

from krubit.discord.content_runtime import ConnectorScheduler, ContentRuntime
from krubit.domain.creator_signals import (
    ContentKind,
    ContentPlan,
    ContentState,
    CreatorAccount,
    CreatorRoute,
    Platform,
    creator_account_id,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import ConnectorAccount, ConnectorHealth, ConnectorPage
from krubit.integrations.catalog import CATALOG
from krubit.services.notification_policy import (
    MentionBudgetState,
    NotificationPolicy,
    QuietHours,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)
GUILD_ID = 111


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.nonce: str | None = None

    async def edit(self, **kwargs: object) -> None:
        return None


class FakeTextChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.sent: list[dict[str, object]] = []
        self.messages: dict[int, FakeMessage] = {}

    def permissions_for(self, member: object) -> object:
        return self.permissions

    async def send(self, **kwargs: object) -> FakeMessage:
        message = FakeMessage(1000 + len(self.sent) + 1)
        message.nonce = cast(str | None, kwargs.get("nonce"))
        self.sent.append(kwargs)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.messages[message_id]

    async def history(self, *, limit: int):  # noqa: ANN201 - test double
        for message in list(self.messages.values())[-limit:]:
            yield message


class FakeGuild:
    def __init__(self, channel: FakeTextChannel) -> None:
        self.id = GUILD_ID
        self.channel = channel
        self.me = SimpleNamespace()

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        return self.channel if channel_id == self.channel.id else None


def as_guild(guild: FakeGuild) -> discord.Guild:
    return cast(discord.Guild, guild)


def account() -> CreatorAccount:
    return CreatorAccount(
        guild_id=GUILD_ID,
        account_id=creator_account_id(Platform.BLUESKY, "bsky-one"),
        owner_member_id=222,
        platform=Platform.BLUESKY,
        handle="bsky-one",
        canonical_url="https://bsky.app/profile/bsky-one",
        external_id="bsky-one",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def route() -> CreatorRoute:
    return CreatorRoute(
        guild_id=GUILD_ID,
        account_id=account().account_id,
        content_kind=ContentKind.POST,
        channel_id=444,
        mention_role_id=None,
        updated_at=NOW,
    )


def social_item(external_id: str, *, state: ContentState) -> Mapping[str, JSONValue]:
    return {
        "external_id": external_id,
        "kind": ContentKind.POST.value,
        "state": state.value,
        "canonical_url": f"https://bsky.app/profile/bsky-one/post/{external_id}",
        "title": f"Post {external_id}",
    }


class ScriptedBluesky:
    """A `Connector` whose `fetch_page` returns one scripted page per call."""

    descriptor = CATALOG[Platform.BLUESKY]

    def __init__(self) -> None:
        self._pages: list[ConnectorPage] = []
        self.calls = 0

    def queue(self, page: ConnectorPage) -> None:
        self._pages.append(page)

    async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
        raise NotImplementedError

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        self.calls += 1
        return self._pages.pop(0) if self._pages else ConnectorPage(items=())

    async def health(
        self, account: CreatorAccount | None = None
    ) -> ConnectorHealth:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[SQLiteStore, FakeGuild]]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(GUILD_ID, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    try:
        yield store, guild
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_scheduler_ingested_transition_is_delivered_through_on_plans_hook(
    env: tuple[SQLiteStore, FakeGuild],
) -> None:
    store, guild = env
    connector = ScriptedBluesky()
    connector.queue(ConnectorPage(items=(social_item("post-1", state=ContentState.SCHEDULED),)))
    connector.queue(ConnectorPage(items=(social_item("post-1", state=ContentState.PUBLISHED),)))
    runtime = ContentRuntime(store, now=lambda: NOW)
    delivered: list[int] = []

    async def on_plans(guild_id: int, plans: tuple[ContentPlan, ...]) -> None:
        delivered.append(await runtime.apply_plans(as_guild(guild), plans))

    clock = {"value": NOW}
    supervisor = ConnectorScheduler(
        store,
        {Platform.BLUESKY: connector},
        guild_ids=lambda: (GUILD_ID,),
        now=lambda: clock["value"],
        jitter=lambda: 0.0,
        on_plans=on_plans,
    )

    await supervisor.run_cycle()  # baseline page: stores identity, claims nothing
    assert delivered == []
    assert guild.channel.sent == []

    clock["value"] = NOW + timedelta(minutes=3)  # past Bluesky's 2-minute default interval
    await supervisor.run_cycle()  # transition page: claims and delivers

    assert delivered == [1]
    assert len(guild.channel.sent) == 1
    delivery = await store.get_content_delivery(GUILD_ID, Platform.BLUESKY, "post-1")
    assert delivery is not None
    assert delivery.status == "delivered"
    assert delivery.discord_message_id is not None


@pytest.mark.asyncio
async def test_quiet_hours_queues_then_recover_pending_releases_it(
    env: tuple[SQLiteStore, FakeGuild],
) -> None:
    store, guild = env

    def quiet_policy_factory(
        _guild: discord.Guild, resolved_route: CreatorRoute
    ) -> NotificationPolicy:
        return NotificationPolicy(
            quiet_hours=QuietHours(time(22, 0), time(7, 0), ZoneInfo("UTC")),
            live_everyone_budget=MentionBudgetState(limit=None),
            social_role_budget=MentionBudgetState(limit=None),
            social_mention_role_id=resolved_route.mention_role_id,
            live_bypass_quiet_hours=True,
        )

    clock = {"value": NOW}  # 23:00 UTC: inside the 22:00-07:00 quiet window
    runtime = ContentRuntime(store, now=lambda: clock["value"], policy_factory=quiet_policy_factory)
    connector = ScriptedBluesky()
    connector.queue(ConnectorPage(items=(social_item("post-1", state=ContentState.SCHEDULED),)))
    connector.queue(ConnectorPage(items=(social_item("post-1", state=ContentState.PUBLISHED),)))

    async def on_plans(guild_id: int, plans: tuple[ContentPlan, ...]) -> None:
        await runtime.apply_plans(as_guild(guild), plans)

    supervisor = ConnectorScheduler(
        store,
        {Platform.BLUESKY: connector},
        guild_ids=lambda: (GUILD_ID,),
        now=lambda: clock["value"],
        jitter=lambda: 0.0,
        on_plans=on_plans,
    )
    await supervisor.run_cycle()
    clock["value"] = NOW + timedelta(minutes=3)
    await supervisor.run_cycle()

    # Still quiet hours: nothing was sent, and the delivery stays durably pending.
    assert guild.channel.sent == []
    pending = await store.list_pending_content_deliveries(GUILD_ID)
    assert len(pending) == 1

    # Advance past the quiet window and sweep the backlog — the queued delivery
    # releases without ever being re-ingested from the connector.
    clock["value"] = NOW.replace(hour=8, minute=0)
    applied = await runtime.recover_pending(as_guild(guild))

    assert applied == 1
    assert len(guild.channel.sent) == 1
    delivery = await store.get_content_delivery(GUILD_ID, Platform.BLUESKY, "post-1")
    assert delivery is not None and delivery.status == "delivered"


@pytest.mark.asyncio
async def test_restarted_scheduler_still_delivers_exactly_once(
    env: tuple[SQLiteStore, FakeGuild],
) -> None:
    """A fresh `ConnectorScheduler` (simulating a process restart) sharing the same
    store and a fresh `ContentRuntime` must not re-deliver a transition the prior
    process already claimed and sent."""
    store, guild = env
    runtime = ContentRuntime(store, now=lambda: NOW)

    async def on_plans(guild_id: int, plans: tuple[ContentPlan, ...]) -> None:
        await runtime.apply_plans(as_guild(guild), plans)

    first_connector = ScriptedBluesky()
    first_connector.queue(
        ConnectorPage(items=(social_item("post-1", state=ContentState.SCHEDULED),))
    )
    first_supervisor = ConnectorScheduler(
        store,
        {Platform.BLUESKY: first_connector},
        guild_ids=lambda: (GUILD_ID,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
        on_plans=on_plans,
    )
    await first_supervisor.run_cycle()  # baseline only

    second_connector = ScriptedBluesky()
    second_connector.queue(
        ConnectorPage(items=(social_item("post-1", state=ContentState.PUBLISHED),))
    )
    second_supervisor = ConnectorScheduler(
        store,
        {Platform.BLUESKY: second_connector},
        guild_ids=lambda: (GUILD_ID,),
        now=lambda: NOW + timedelta(minutes=3),
        jitter=lambda: 0.0,
        on_plans=on_plans,
    )
    await second_supervisor.run_cycle()  # claims and delivers exactly once

    assert len(guild.channel.sent) == 1

    # A later recovery sweep (another simulated restart) must edit in place, never
    # send a second message for the same transition.
    applied = await runtime.recover_pending(as_guild(guild))
    assert applied == 0
    assert len(guild.channel.sent) == 1
