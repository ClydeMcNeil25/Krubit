from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.content_runtime import ContentRuntime, delivery_id_for
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
from krubit.integrations.base import ConnectorPage
from krubit.services.content_signals import ContentSignalService
from krubit.services.notification_policy import MentionBudgetState, NotificationPolicy
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.nonce: str | None = None
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class FakeTextChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.sent: list[dict[str, object]] = []
        self.messages: dict[int, FakeMessage] = {}
        self.send_failure: BaseException | None = None
        self.fetch_failure: BaseException | None = None

    def permissions_for(self, member: object) -> object:
        return self.permissions

    async def send(self, **kwargs: object) -> FakeMessage:
        if self.send_failure is not None:
            raise self.send_failure
        message = FakeMessage(1000 + len(self.sent) + 1)
        message.nonce = cast(str | None, kwargs.get("nonce"))
        self.sent.append(kwargs)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if self.fetch_failure is not None:
            raise self.fetch_failure
        return self.messages[message_id]

    async def history(self, *, limit: int) -> AsyncIterator[FakeMessage]:
        for message in list(self.messages.values())[-limit:]:
            yield message


class FakeGuild:
    def __init__(self, channel: FakeTextChannel) -> None:
        self.id = 111
        self.channel = channel
        self.me = SimpleNamespace()

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        return self.channel if channel_id == self.channel.id else None


def as_guild(guild: FakeGuild) -> discord.Guild:
    return cast(discord.Guild, guild)


def account() -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.TWITCH, "twitch-one"),
        owner_member_id=222,
        platform=Platform.TWITCH,
        handle="Krucial Studios",
        canonical_url="https://www.twitch.tv/krucialstudios",
        external_id="twitch-one",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def route(*, mention_role_id: int | None = None) -> CreatorRoute:
    return CreatorRoute(
        guild_id=111,
        account_id=account().account_id,
        content_kind=ContentKind.LIVE,
        channel_id=444,
        mention_role_id=mention_role_id,
        updated_at=NOW,
    )


def live_item(external_id: str, *, state: ContentState) -> Mapping[str, JSONValue]:
    return {
        "external_id": external_id,
        "kind": ContentKind.LIVE.value,
        "state": state.value,
        "canonical_url": f"https://www.twitch.tv/krucialstudios/{external_id}",
        "title": f"Stream {external_id}",
    }


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[ContentRuntime, SQLiteStore, FakeGuild]]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    runtime = ContentRuntime(store, now=lambda: NOW)
    try:
        yield runtime, store, guild
    finally:
        await store.close()


async def claim_live_plan(store: SQLiteStore, external_id: str = "stream-1") -> ContentPlan:
    """Ingest a baseline page then a page reaching LIVE, returning the claimed plan."""
    service = ContentSignalService(store)
    await service.ingest_page(
        account(),
        ConnectorPage(items=(live_item(external_id, state=ContentState.SCHEDULED),)),
        now=NOW,
    )
    result = await service.ingest_page(
        account(),
        ConnectorPage(items=(live_item(external_id, state=ContentState.LIVE),)),
        now=NOW,
    )
    assert len(result.plans) == 1
    return result.plans[0]


async def end_plan(store: SQLiteStore, plan: ContentPlan) -> ContentPlan:
    """Advance the same content item to ENDED without claiming a new delivery."""
    service = ContentSignalService(store)
    await service.ingest_page(
        account(),
        ConnectorPage(items=(live_item(plan.event.external_id, state=ContentState.ENDED),)),
        now=NOW,
    )
    ended_event = await store.get_content_event(111, plan.event.platform, plan.event.external_id)
    assert ended_event is not None
    return ContentPlan(event=ended_event, delivery=plan.delivery)


@pytest.mark.asyncio
async def test_apply_plan_sends_live_card_with_watch_button_and_everyone_mention(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)

    applied = await runtime.apply_plan(as_guild(guild), plan)

    assert applied is True
    assert len(guild.channel.sent) == 1
    sent = guild.channel.sent[0]
    assert "@everyone" in cast(str, sent["content"])
    assert sent["allowed_mentions"].everyone is True  # type: ignore[union-attr]
    delivered = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivered is not None
    assert delivered.status == "delivered"
    assert delivered.discord_channel_id == 444
    assert delivered.discord_message_id == guild.channel.messages[1001].id


@pytest.mark.asyncio
async def test_recovery_edits_matching_receipted_message_instead_of_resending(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)

    await runtime.apply_plan(as_guild(guild), plan)
    ended = await end_plan(store, plan)
    await runtime.apply_plan(as_guild(guild), ended)

    assert len(guild.channel.sent) == 1
    assert len(guild.channel.messages[1001].edits) == 1


@pytest.mark.asyncio
async def test_edit_failure_records_a_failed_delivery_receipt_and_clears_stored_message(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    await runtime.apply_plan(as_guild(guild), plan)
    # Simulate a moderator deleting the announcement message before the next transition.
    guild.channel.fetch_failure = discord.NotFound(
        cast(Any, SimpleNamespace(status=404, reason="Not Found", headers={})), "Unknown Message"
    )
    ended = await end_plan(store, plan)

    applied = await runtime.apply_plan(as_guild(guild), ended)

    assert applied is False
    delivery = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivery is not None
    assert delivery.status == "failed"
    assert delivery.discord_message_id is None
    assert delivery.discord_channel_id is None


@pytest.mark.asyncio
async def test_editing_an_already_delivered_message_does_not_consume_mention_budget(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)

    def limited_policy(_guild: object, creator_route: CreatorRoute) -> NotificationPolicy:
        return NotificationPolicy(
            quiet_hours=None,
            live_everyone_budget=MentionBudgetState(limit=1),
            social_role_budget=MentionBudgetState(limit=1),
            social_mention_role_id=creator_route.mention_role_id,
        )

    runtime = ContentRuntime(store, policy_factory=limited_policy, now=lambda: NOW)
    try:
        plan = await claim_live_plan(store)

        await runtime.apply_plan(as_guild(guild), plan)
        ended = await end_plan(store, plan)
        await runtime.apply_plan(as_guild(guild), ended)

        consumed = await store.mention_budget_consumed(
            111, "live_everyone", NOW.date().isoformat()
        )
        assert consumed == 1
        assert len(guild.channel.sent) == 1
        assert len(guild.channel.messages[1001].edits) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_apply_plan_recovers_unsent_record_via_bounded_history_scan(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    await runtime.apply_plan(as_guild(guild), plan)
    assert len(guild.channel.sent) == 1

    # Simulate a crash after the send succeeded but before the outcome was recorded.
    await store.update_content_delivery(
        guild_id=111,
        platform=plan.delivery.platform,
        external_id=plan.delivery.external_id,
        transition_seq=plan.delivery.transition_seq,
        status="pending",
        attempt=plan.delivery.attempt,
        channel_id=None,
        message_id=None,
        now=NOW,
    )

    applied = await runtime.apply_plan(as_guild(guild), plan)

    assert applied is True
    assert len(guild.channel.sent) == 1
    delivered = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivered is not None
    assert delivered.status == "delivered"
    assert delivered.discord_message_id == 1001


@pytest.mark.asyncio
async def test_apply_plan_marks_failed_and_never_records_a_missing_message(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    guild.channel.send_failure = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom", headers={})), "boom"
    )

    applied = await runtime.apply_plan(as_guild(guild), plan)

    assert applied is False
    delivery = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivery is not None
    assert delivery.status == "failed"
    assert delivery.discord_message_id is None


@pytest.mark.asyncio
async def test_retry_delivery_resends_after_a_failed_attempt_and_bumps_attempt(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    guild.channel.send_failure = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom", headers={})), "boom"
    )
    await runtime.apply_plan(as_guild(guild), plan)
    guild.channel.send_failure = None

    retried = await runtime.retry_delivery(
        as_guild(guild),
        delivery_id_for(
            plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
        ),
    )

    assert retried is True
    assert len(guild.channel.sent) == 1
    delivery = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivery is not None
    assert delivery.status == "delivered"
    assert delivery.attempt == 2


@pytest.mark.asyncio
async def test_retry_delivery_refuses_a_delivery_that_never_failed(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)

    retried = await runtime.retry_delivery(
        as_guild(guild),
        delivery_id_for(
            plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
        ),
    )

    assert retried is False
    assert guild.channel.sent == []


@pytest.mark.asyncio
async def test_retract_delivery_edits_stored_message_and_blocks_further_delivery(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    await runtime.apply_plan(as_guild(guild), plan)

    retracted = await runtime.retract_delivery(
        as_guild(guild),
        delivery_id_for(
            plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
        ),
    )
    reapplied = await runtime.apply_plan(as_guild(guild), await end_plan(store, plan))

    assert retracted is True
    assert reapplied is False
    assert len(guild.channel.messages[1001].edits) == 1
    assert "retracted" in str(guild.channel.messages[1001].edits[0]["content"]).lower()
    delivery = await store.get_content_delivery_by_seq(
        111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    assert delivery is not None and delivery.status == "cancelled"


@pytest.mark.asyncio
async def test_apply_plan_refuses_when_no_route_is_configured_for_the_content_kind(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    service = ContentSignalService(store)
    other = CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.YOUTUBE, "yt-one"),
        owner_member_id=333,
        platform=Platform.YOUTUBE,
        handle="Other Creator",
        canonical_url="https://www.youtube.com/@othercreator",
        external_id="yt-one",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    await store.save_creator_account(other)
    await service.ingest_page(
        other, ConnectorPage(items=({
            "external_id": "vid-1",
            "kind": ContentKind.VIDEO.value,
            "state": ContentState.SCHEDULED.value,
            "canonical_url": "https://www.youtube.com/watch?v=vid-1",
        },)), now=NOW,
    )
    result = await service.ingest_page(
        other, ConnectorPage(items=({
            "external_id": "vid-1",
            "kind": ContentKind.VIDEO.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": "https://www.youtube.com/watch?v=vid-1",
        },)), now=NOW,
    )
    plan = result.plans[0]

    applied = await runtime.apply_plan(as_guild(guild), plan)

    assert applied is False
    assert guild.channel.sent == []


@pytest.mark.asyncio
async def test_apply_plan_refuses_when_guild_is_disabled(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    await store.set_guild_enabled(111, False)

    applied = await runtime.apply_plan(as_guild(guild), plan)

    assert applied is False
    assert guild.channel.sent == []


@pytest.mark.asyncio
async def test_recover_pending_delivers_the_guilds_pending_backlog(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    await claim_live_plan(store)

    recovered = await runtime.recover_pending(as_guild(guild))

    assert recovered == 1


@pytest.mark.asyncio
async def test_apply_plans_delivers_every_freshly_claimed_plan_from_one_page(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    """A connector's `IngestionResult.plans` (e.g. from `YouTubeConnector.fetch_page`)
    is applied in one batch, mirroring how a future polling/push scheduler would call
    `apply_plans` right after `ContentSignalService.ingest_page`."""
    runtime, store, guild = env
    service = ContentSignalService(store)
    await service.ingest_page(
        account(),
        ConnectorPage(
            items=(
                live_item("stream-a", state=ContentState.SCHEDULED),
                live_item("stream-b", state=ContentState.SCHEDULED),
            )
        ),
        now=NOW,
    )
    result = await service.ingest_page(
        account(),
        ConnectorPage(
            items=(
                live_item("stream-a", state=ContentState.LIVE),
                live_item("stream-b", state=ContentState.LIVE),
            )
        ),
        now=NOW,
    )
    assert len(result.plans) == 2

    applied = await runtime.apply_plans(as_guild(guild), result.plans)

    assert applied == 2
    assert len(guild.channel.sent) == 2


@pytest.mark.asyncio
async def test_apply_plans_counts_only_the_plans_that_actually_applied(
    env: tuple[ContentRuntime, SQLiteStore, FakeGuild],
) -> None:
    runtime, store, guild = env
    plan = await claim_live_plan(store)
    await store.set_guild_enabled(111, False)

    applied = await runtime.apply_plans(as_guild(guild), (plan,))

    assert applied == 0
    assert guild.channel.sent == []


# --- KRUBIT_SOCIAL_DELIVERY_ENABLED shadow-mode gate ----------------------------


@pytest.mark.asyncio
async def test_apply_plan_sends_nothing_when_social_delivery_is_disabled(
    tmp_path: Path,
) -> None:
    """Final-review Critical #1: `social_delivery_enabled=False` must produce zero
    Discord sends, regardless of what the scheduler or a command surface claims."""
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    runtime = ContentRuntime(store, now=lambda: NOW, social_delivery_enabled=False)
    try:
        plan = await claim_live_plan(store)

        applied = await runtime.apply_plan(as_guild(guild), plan)

        assert applied is False
        assert guild.channel.sent == []
        delivery = await store.get_content_delivery_by_seq(
            111, plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
        )
        assert delivery is not None
        assert delivery.status == "pending"
        # Shadow mode must never consume a mention-budget slot for content it never
        # actually announced.
        consumed = await store.mention_budget_consumed(
            111, "live_everyone", NOW.date().isoformat()
        )
        assert consumed == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_apply_plans_and_recover_pending_send_nothing_when_delivery_is_disabled(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    runtime = ContentRuntime(store, now=lambda: NOW, social_delivery_enabled=False)
    try:
        plan = await claim_live_plan(store)

        applied = await runtime.apply_plans(as_guild(guild), (plan,))
        recovered = await runtime.recover_pending(as_guild(guild))

        assert applied == 0
        assert recovered == 0
        assert guild.channel.sent == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_retry_delivery_sends_nothing_when_social_delivery_is_disabled(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.save_creator_account(account())
    await store.save_creator_route(route())
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    # Fail one real send while delivery is enabled, to reach a `failed` delivery...
    runtime_enabled = ContentRuntime(store, now=lambda: NOW, social_delivery_enabled=True)
    plan = await claim_live_plan(store)
    channel.send_failure = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom", headers={})), "boom"
    )
    await runtime_enabled.apply_plan(as_guild(guild), plan)
    channel.send_failure = None

    # ...then retry it with delivery disabled and confirm nothing is ever sent.
    runtime_disabled = ContentRuntime(store, now=lambda: NOW, social_delivery_enabled=False)
    try:
        retried = await runtime_disabled.retry_delivery(
            as_guild(guild),
            delivery_id_for(
                plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
            ),
        )

        assert retried is False
        assert guild.channel.sent == []
    finally:
        await store.close()
