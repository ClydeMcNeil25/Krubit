from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from krubit.discord.content_commands import (
    ActorContext,
    CommandStatus,
    ContentCommandService,
    GuildLike,
)
from krubit.discord.content_runtime import ContentRuntime, delivery_id_for
from krubit.domain.creator_signals import (
    Capability,
    ContentKind,
    CreatorRoute,
    Platform,
)
from krubit.services.content_signals import ContentSignalService
from krubit.storage.sqlite import CreatorRegistryReceipt, SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

YOUTUBE_URL = "https://www.youtube.com/@examplecreator"

GUILD_ID = 111
OWNER_ID = 222
OTHER_ID = 333
ADMIN_ID = 999


def creator_member() -> ActorContext:
    return ActorContext(
        guild_id=GUILD_ID, member_id=OWNER_ID, is_admin=False, has_creator_role=True
    )


def other_member() -> ActorContext:
    return ActorContext(
        guild_id=GUILD_ID, member_id=OTHER_ID, is_admin=False, has_creator_role=True
    )


def admin_member() -> ActorContext:
    return ActorContext(
        guild_id=GUILD_ID, member_id=ADMIN_ID, is_admin=True, has_creator_role=False
    )


def no_role_member() -> ActorContext:
    return ActorContext(
        guild_id=GUILD_ID, member_id=OWNER_ID, is_admin=False, has_creator_role=False
    )


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

    async def history(self, *, limit: int) -> AsyncIterator[FakeMessage]:
        for message in list(self.messages.values())[-limit:]:
            yield message


class FakeGuild:
    def __init__(self, channel: FakeTextChannel) -> None:
        self.id = GUILD_ID
        self.channel = channel
        self.me = SimpleNamespace()

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        return self.channel if channel_id == self.channel.id else None


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(GUILD_ID, True)
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def commands(store: SQLiteStore) -> ContentCommandService:
    return ContentCommandService(store, now=lambda: NOW)


# -- authority: add ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_creator_role_can_add_self_but_not_another_member(
    commands: ContentCommandService,
) -> None:
    own = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL
    )
    denied = await commands.creator_add(
        actor=creator_member(), owner=other_member(), url=YOUTUBE_URL
    )

    assert own.status is CommandStatus.CONFIRMATION_REQUIRED
    assert denied.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_add_without_creator_role_is_denied(commands: ContentCommandService) -> None:
    result = await commands.creator_add(
        actor=no_role_member(), owner=no_role_member(), url=YOUTUBE_URL
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_admin_can_add_on_behalf_of_another_member(commands: ContentCommandService) -> None:
    result = await commands.creator_add(actor=admin_member(), owner=other_member(), url=YOUTUBE_URL)
    assert result.status is CommandStatus.CONFIRMATION_REQUIRED


@pytest.mark.asyncio
async def test_confirmation_required_does_not_mutate_storage(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    await commands.creator_add(actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL)
    accounts = await store.list_creator_accounts(GUILD_ID)
    assert accounts == []


@pytest.mark.asyncio
async def test_confirm_true_persists_the_account(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    result = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    assert result.status is CommandStatus.SUCCEEDED
    accounts = await store.list_creator_accounts(GUILD_ID)
    assert len(accounts) == 1
    assert accounts[0].platform is Platform.YOUTUBE
    assert accounts[0].paused is True


@pytest.mark.asyncio
async def test_denied_actor_never_receives_a_confirmation_card(
    commands: ContentCommandService,
) -> None:
    denied = await commands.creator_add(
        actor=creator_member(), owner=other_member(), url=YOUTUBE_URL
    )
    assert denied.card is None


# -- authorize -----------------------------------------------------------------

INSTAGRAM_URL = "https://www.instagram.com/examplecreator"
TIKTOK_URL = "https://www.tiktok.com/@examplecreator"

_META_APP_ID = "test-meta-app-id"
_META_CALLBACK_BASE_URL = "https://example.com"
_TIKTOK_CLIENT_KEY = "test-tiktok-client-key"
_TIKTOK_CALLBACK_BASE_URL = "https://example.com"


async def _add_and_confirm(
    commands: ContentCommandService, *, actor: ActorContext, owner: ActorContext, url: str
) -> str:
    added = await commands.creator_add(actor=actor, owner=owner, url=url, confirm=True)
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)
    return account_id


@pytest.mark.asyncio
async def test_creator_authorize_denies_non_owner_non_admin(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL
    )

    result = await commands.creator_authorize(
        actor=other_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_creator_authorize_succeeds_for_owner_with_instagram_account(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL
    )

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "facebook.com" in result.card.description


@pytest.mark.asyncio
async def test_creator_authorize_succeeds_for_admin_on_behalf_of_another_member(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=TIKTOK_URL)

    result = await commands.creator_authorize(
        actor=admin_member(),
        owner=creator_member(),
        url=TIKTOK_URL,
        capability=Capability.SOCIAL,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "tiktok.com" in result.card.description


@pytest.mark.asyncio
async def test_creator_authorize_fails_when_account_not_yet_added(
    commands: ContentCommandService,
) -> None:
    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_rejects_facebook_page_before_issuing_any_state(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    facebook_page_url = "https://www.facebook.com/pages/Example-Page/123456789"
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=facebook_page_url
    )

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=facebook_page_url,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_rejects_live_capability_before_issuing_any_state(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL
    )

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.LIVE,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_fails_cleanly_when_meta_not_configured(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL
    )

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=None,
        meta_callback_base_url=None,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


class _FakeParent:
    """A minimal stand-in for `FetchCommands` -- `CreatorCommands.
    authorize` only reads these four credential attributes off `_parent`,
    so a full `FetchCommands`/`KrubitBot` construction is unnecessary."""

    def __init__(self) -> None:
        self._meta_app_id: str | None = _META_APP_ID
        self._meta_callback_base_url: str | None = _META_CALLBACK_BASE_URL
        self._tiktok_client_key: str | None = _TIKTOK_CLIENT_KEY
        self._tiktok_callback_base_url: str | None = _TIKTOK_CALLBACK_BASE_URL


class _AuthorizeFakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False)
        self.roles: list[object] = []


class _AuthorizeFakeGuild:
    def __init__(self, members: dict[int, _AuthorizeFakeMember]) -> None:
        self.id = GUILD_ID
        self.roles: list[object] = []
        self.text_channels: list[object] = []
        self._members = members

    def get_member(self, member_id: int) -> _AuthorizeFakeMember | None:
        return self._members.get(member_id)


class _AuthorizeFakeResponse:
    def __init__(self) -> None:
        self.deferred: dict[str, bool] | None = None
        self.sent: dict[str, object] | None = None

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.sent = {"content": content, "ephemeral": ephemeral}


class _AuthorizeFakeFollowup:
    def __init__(self) -> None:
        self.sent: dict[str, object] | None = None

    async def send(
        self, content: str | None = None, *, embed: object | None = None, ephemeral: bool = False
    ) -> None:
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral}


class _AuthorizeFakeInteraction:
    def __init__(self, guild: _AuthorizeFakeGuild, member: _AuthorizeFakeMember) -> None:
        self.guild_id = guild.id
        self.guild = guild
        self.user = member
        self.response = _AuthorizeFakeResponse()
        self.followup = _AuthorizeFakeFollowup()


@pytest.mark.asyncio
async def test_authorize_command_denies_non_owner_before_any_send(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`other` (not the account owner, not an admin) tries to authorize
    `owner`'s account. `CreatorCommands.authorize` defers (matching every
    other mutating command's defer-then-call-service shape -- see `add`)
    and only then learns from the service that the actor is unauthorized.
    The safety property this proves is the same "denied actor never sees
    the informative card" contract the module docstring documents for
    every other command: the actor's `followup.send` receives only a bare
    status string, never an embed carrying the account's handle/URL."""
    from krubit.discord.content_commands import ContentCommandService, CreatorCommands

    monkeypatch.setattr(
        "krubit.discord.content_commands.discord.Member", _AuthorizeFakeMember
    )
    service = ContentCommandService(store, now=lambda: NOW)
    await service.creator_add(
        actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL, confirm=True
    )
    commands = CreatorCommands(cast(object, _FakeParent()), service)
    owner = _AuthorizeFakeMember(OWNER_ID)
    other = _AuthorizeFakeMember(OTHER_ID)
    guild = _AuthorizeFakeGuild({OWNER_ID: owner, OTHER_ID: other})
    interaction = _AuthorizeFakeInteraction(guild, other)

    await commands.authorize.callback(  # type: ignore[attr-defined]
        commands, cast(discord.Interaction, interaction), INSTAGRAM_URL, "account", owner
    )

    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.followup.sent is not None
    assert interaction.followup.sent["embed"] is None
    assert interaction.followup.sent["content"] == "denied"


@pytest.mark.asyncio
async def test_authorize_command_sends_an_embed_for_the_owner(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from krubit.discord.content_commands import ContentCommandService, CreatorCommands

    monkeypatch.setattr(
        "krubit.discord.content_commands.discord.Member", _AuthorizeFakeMember
    )
    service = ContentCommandService(store, now=lambda: NOW)
    await service.creator_add(
        actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL, confirm=True
    )
    commands = CreatorCommands(cast(object, _FakeParent()), service)
    owner = _AuthorizeFakeMember(OWNER_ID)
    guild = _AuthorizeFakeGuild({OWNER_ID: owner})
    interaction = _AuthorizeFakeInteraction(guild, owner)

    await commands.authorize.callback(  # type: ignore[attr-defined]
        commands, cast(discord.Interaction, interaction), INSTAGRAM_URL, "account", None
    )

    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.followup.sent is not None


# -- pause/resume/remove -------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_requires_owner_or_admin_authority(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    denied = await commands.creator_pause(actor=other_member(), account_id=account_id)
    assert denied.status is CommandStatus.DENIED

    owner_ok = await commands.creator_pause(actor=creator_member(), account_id=account_id)
    assert owner_ok.status is CommandStatus.CONFIRMATION_REQUIRED

    confirmed = await commands.creator_pause(
        actor=creator_member(), account_id=account_id, confirm=True
    )
    assert confirmed.status is CommandStatus.SUCCEEDED
    stored = await store.get_creator_account(GUILD_ID, account_id)
    assert stored is not None
    assert stored.paused is True


@pytest.mark.asyncio
async def test_remove_pauses_rather_than_deletes(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    preview = await commands.creator_remove(actor=creator_member(), account_id=account_id)
    assert preview.card is not None
    assert "does not delete any data" in preview.card.description

    result = await commands.creator_remove(
        actor=creator_member(), account_id=account_id, confirm=True
    )
    assert result.card is not None
    assert "Not Deleted" in result.card.title or "No data was deleted" in result.card.description

    stored = await store.get_creator_account(GUILD_ID, account_id)
    assert stored is not None  # history preserved, never hard-deleted
    assert stored.paused is True


# -- transfer -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_is_admin_only(commands: ContentCommandService) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    denied = await commands.creator_transfer(
        actor=creator_member(), account_id=account_id, new_owner_member_id=OTHER_ID
    )
    assert denied.status is CommandStatus.DENIED

    allowed = await commands.creator_transfer(
        actor=admin_member(), account_id=account_id, new_owner_member_id=OTHER_ID
    )
    assert allowed.status is CommandStatus.CONFIRMATION_REQUIRED


# -- route: produces a redacted audit receipt ------------------------------------------


@pytest.mark.asyncio
async def test_route_change_records_a_redacted_audit_receipt(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    def route_receipts(
        receipts: list[CreatorRegistryReceipt],
    ) -> list[CreatorRegistryReceipt]:
        return [r for r in receipts if r.action == "route_account"]

    preview = await commands.creator_route(
        actor=creator_member(),
        account_id=account_id,
        content_kind=ContentKind.VIDEO,
        channel_id=444,
        mention_role_id=None,
    )
    assert preview.status is CommandStatus.CONFIRMATION_REQUIRED
    before = await store.list_creator_registry_receipts(GUILD_ID, account_id)
    assert route_receipts(before) == []  # confirmation alone never records a receipt

    result = await commands.creator_route(
        actor=creator_member(),
        account_id=account_id,
        content_kind=ContentKind.VIDEO,
        channel_id=444,
        mention_role_id=None,
        confirm=True,
    )
    assert result.status is CommandStatus.SUCCEEDED

    routes = await store.list_creator_routes(GUILD_ID, account_id)
    assert len(routes) == 1
    assert routes[0].channel_id == 444

    after = await store.list_creator_registry_receipts(GUILD_ID, account_id)
    routed = route_receipts(after)
    assert len(routed) == 1
    assert routed[0].detail["channel_id"] == 444


@pytest.mark.asyncio
async def test_route_change_denied_for_non_owner_records_no_receipt(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    denied = await commands.creator_route(
        actor=other_member(),
        account_id=account_id,
        content_kind=ContentKind.VIDEO,
        channel_id=444,
        mention_role_id=None,
        confirm=True,
    )
    assert denied.status is CommandStatus.DENIED
    receipts = await store.list_creator_registry_receipts(GUILD_ID, account_id)
    assert [r for r in receipts if r.action == "route_account"] == []
    assert await store.list_creator_routes(GUILD_ID, account_id) == []


# -- verify: discloses it is a static platform baseline, not a live per-account check --


@pytest.mark.asyncio
async def test_verify_card_discloses_it_is_a_static_platform_baseline(
    commands: ContentCommandService,
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    result = await commands.creator_verify(actor=creator_member(), account_id=account_id)
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "not a live check" in result.card.description
    assert any(
        field.name == "This account's monitoring state" for field in result.card.fields
    )


# -- notification preview: zero side effects ------------------------------------------


@pytest.mark.asyncio
async def test_notification_preview_performs_zero_side_effects(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)
    await store.save_creator_route(
        CreatorRoute(
            guild_id=GUILD_ID,
            account_id=account_id,
            content_kind=ContentKind.VIDEO,
            channel_id=444,
            mention_role_id=555,
            updated_at=NOW,
        )
    )

    result = await commands.notification_preview(
        actor=creator_member(), account_id=account_id, content_kind=ContentKind.VIDEO
    )

    assert result.status is CommandStatus.SUCCEEDED
    assert result.rendered is not None
    # No delivery, cursor, receipt, or Scheduled Event row was ever created.
    assert await store.list_content_deliveries_for_account(GUILD_ID, account_id) == []
    assert await store.list_content_receipts(GUILD_ID, account_id) == []
    assert await store.list_owned_scheduled_event_mappings(GUILD_ID) == []
    # No mention budget was consumed either.
    assert await store.mention_budget_consumed(GUILD_ID, "social_role", NOW.date().isoformat()) == 0


@pytest.mark.asyncio
async def test_notification_preview_fails_without_a_configured_route(
    commands: ContentCommandService,
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)

    result = await commands.notification_preview(
        actor=creator_member(), account_id=account_id, content_kind=ContentKind.VIDEO
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_notification_preview_denies_a_non_owning_non_admin_actor(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    """Final-review Important #7: a preview card can reveal the account's canonical
    URL and configured mention role, so only the account's owner or an admin may see
    it — a same-guild `Creator`-role member who does not own this account must be
    denied before any card is ever rendered."""
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)
    await store.save_creator_route(
        CreatorRoute(
            guild_id=GUILD_ID,
            account_id=account_id,
            content_kind=ContentKind.VIDEO,
            channel_id=444,
            mention_role_id=555,
            updated_at=NOW,
        )
    )

    result = await commands.notification_preview(
        actor=other_member(), account_id=account_id, content_kind=ContentKind.VIDEO
    )

    assert result.status is CommandStatus.DENIED
    assert result.rendered is None


@pytest.mark.asyncio
async def test_notification_preview_allows_an_admin_to_preview_any_account(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    added = await commands.creator_add(
        actor=creator_member(), owner=creator_member(), url=YOUTUBE_URL, confirm=True
    )
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)
    await store.save_creator_route(
        CreatorRoute(
            guild_id=GUILD_ID,
            account_id=account_id,
            content_kind=ContentKind.VIDEO,
            channel_id=444,
            mention_role_id=555,
            updated_at=NOW,
        )
    )

    result = await commands.notification_preview(
        actor=admin_member(), account_id=account_id, content_kind=ContentKind.VIDEO
    )

    assert result.status is CommandStatus.SUCCEEDED
    assert result.rendered is not None


# -- retry: validates route/policy and attempt ownership -------------------------------


async def _seeded_failed_delivery(
    store: SQLiteStore, guild: FakeGuild
) -> tuple[str, str]:
    """Register an account/route, claim a live delivery, then force it to fail by
    denying the channel send, returning (account_id, delivery_id)."""
    from krubit.domain.creator_signals import ContentState, CreatorAccount
    from krubit.domain.creator_signals import Platform as _Platform
    from krubit.domain.creator_signals import creator_account_id as _account_id
    from krubit.integrations.base import ConnectorPage

    account_id = _account_id(_Platform.TWITCH, "twitch-retry")
    account = CreatorAccount(
        guild_id=GUILD_ID,
        account_id=account_id,
        owner_member_id=OWNER_ID,
        platform=_Platform.TWITCH,
        handle="Retry Creator",
        canonical_url="https://www.twitch.tv/retrycreator",
        external_id="twitch-retry",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    await store.save_creator_account(account)
    await store.save_creator_route(
        CreatorRoute(
            guild_id=GUILD_ID,
            account_id=account_id,
            content_kind=ContentKind.LIVE,
            channel_id=guild.channel.id,
            mention_role_id=None,
            updated_at=NOW,
        )
    )
    service = ContentSignalService(store)
    await service.ingest_page(
        account,
        ConnectorPage(
            items=(
                {
                    "external_id": "stream-1",
                    "kind": ContentKind.LIVE.value,
                    "state": ContentState.SCHEDULED.value,
                    "canonical_url": "https://www.twitch.tv/retrycreator/stream-1",
                },
            )
        ),
        now=NOW,
    )
    result = await service.ingest_page(
        account,
        ConnectorPage(
            items=(
                {
                    "external_id": "stream-1",
                    "kind": ContentKind.LIVE.value,
                    "state": ContentState.LIVE.value,
                    "canonical_url": "https://www.twitch.tv/retrycreator/stream-1",
                },
            )
        ),
        now=NOW,
    )
    plan = result.plans[0]
    delivery_id = delivery_id_for(
        plan.delivery.platform, plan.delivery.external_id, plan.delivery.transition_seq
    )
    # Force this claimed delivery straight to "failed" without ever sending.
    await store.update_content_delivery(
        guild_id=GUILD_ID,
        platform=plan.delivery.platform,
        external_id=plan.delivery.external_id,
        transition_seq=plan.delivery.transition_seq,
        status="failed",
        attempt=1,
        channel_id=None,
        message_id=None,
        now=NOW,
    )
    return account_id, delivery_id


@pytest.mark.asyncio
async def test_retry_denies_a_non_owning_non_admin_actor(store: SQLiteStore) -> None:
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    _account_id, delivery_id = await _seeded_failed_delivery(store, guild)
    commands = ContentCommandService(
        store, runtime=ContentRuntime(store, now=lambda: NOW), now=lambda: NOW
    )

    result = await commands.notification_retry(
        actor=other_member(), delivery_id=delivery_id, guild=cast(GuildLike, guild)
    )
    assert result.status is CommandStatus.DENIED
    stored = await store.get_content_delivery_by_seq(
        GUILD_ID, Platform.TWITCH, "stream-1", 1
    )
    assert stored is not None
    assert stored.status == "failed"  # unchanged: no retry was ever attempted


@pytest.mark.asyncio
async def test_retry_requires_confirmation_then_applies(store: SQLiteStore) -> None:
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    _account_id, delivery_id = await _seeded_failed_delivery(store, guild)
    commands = ContentCommandService(
        store, runtime=ContentRuntime(store, now=lambda: NOW), now=lambda: NOW
    )

    preview = await commands.notification_retry(
        actor=creator_member(), delivery_id=delivery_id, guild=cast(GuildLike, guild)
    )
    assert preview.status is CommandStatus.CONFIRMATION_REQUIRED
    assert len(channel.sent) == 0  # confirmation alone never sends

    applied = await commands.notification_retry(
        actor=creator_member(), delivery_id=delivery_id, guild=cast(GuildLike, guild), confirm=True
    )
    assert applied.status is CommandStatus.SUCCEEDED
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_retry_fails_for_an_unknown_delivery_id(store: SQLiteStore) -> None:
    commands = ContentCommandService(store, now=lambda: NOW)
    fake_guild = cast(GuildLike, SimpleNamespace(id=GUILD_ID))
    result = await commands.notification_retry(
        actor=creator_member(), delivery_id="twitch:missing:1", guild=fake_guild
    )
    assert result.status is CommandStatus.FAILED


# -- retract: only ever touches a stored Krubit-authored message -----------------------


@pytest.mark.asyncio
async def test_retract_edits_only_the_stored_message(store: SQLiteStore) -> None:
    channel = FakeTextChannel(444)
    guild = FakeGuild(channel)
    _account_id, delivery_id = await _seeded_failed_delivery(store, guild)
    runtime = ContentRuntime(store, now=lambda: NOW)
    commands = ContentCommandService(store, runtime=runtime, now=lambda: NOW)

    # Retry first so a real message is actually sent and stored.
    await commands.notification_retry(
        actor=creator_member(), delivery_id=delivery_id, guild=cast(GuildLike, guild), confirm=True
    )
    stored = await store.get_content_delivery_by_seq(GUILD_ID, Platform.TWITCH, "stream-1", 1)
    assert stored is not None
    assert stored.discord_message_id is not None
    other_message = FakeMessage(9999)
    channel.messages[9999] = other_message

    result = await commands.notification_retract(
        actor=creator_member(), delivery_id=delivery_id, guild=cast(GuildLike, guild), confirm=True
    )
    assert result.status is CommandStatus.SUCCEEDED
    # Only the exact stored message was edited; nothing else in the channel touched.
    assert len(channel.messages[stored.discord_message_id].edits) == 1
    assert other_message.edits == []
