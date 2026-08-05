"""Integration tests for guild-scoped creator registry persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    ContentKind,
    CreatorAccount,
    CreatorRoute,
    Platform,
    creator_account_id,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
LATER = datetime(2026, 8, 4, 20, 30, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def creator_account(
    *,
    guild_id: int = 111,
    owner_member_id: int = 222,
    platform: Platform = Platform.YOUTUBE,
    handle: str = "krucialstudios",
    external_id: str = "UC-one",
    paused: bool = True,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
) -> CreatorAccount:
    return CreatorAccount(
        guild_id=guild_id,
        account_id=creator_account_id(platform, external_id),
        owner_member_id=owner_member_id,
        platform=platform,
        handle=handle,
        canonical_url=f"https://www.youtube.com/@{handle}",
        external_id=external_id,
        paused=paused,
        created_at=created_at,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_creator_accounts_are_guild_scoped_and_stable_id_unique(store: SQLiteStore) -> None:
    first = creator_account(guild_id=111, owner_member_id=222, external_id="UC-one")
    await store.save_creator_account(first)
    await store.save_creator_account(replace(first, guild_id=999, owner_member_id=888))

    first_read = await store.get_creator_account(111, first.account_id)
    second_read = await store.get_creator_account(999, first.account_id)
    assert first_read is not None and first_read.owner_member_id == 222
    assert second_read is not None and second_read.owner_member_id == 888


@pytest.mark.asyncio
async def test_same_platform_identity_cannot_have_two_owners_in_one_guild(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(creator_account(owner_member_id=222))
    with pytest.raises(ValueError, match="already registered"):
        await store.save_creator_account(creator_account(owner_member_id=333))


@pytest.mark.asyncio
async def test_save_creator_account_is_idempotent_for_the_same_owner(store: SQLiteStore) -> None:
    account = creator_account(paused=True)
    await store.save_creator_account(account)
    updated = await store.save_creator_account(replace(account, paused=False, updated_at=LATER))

    assert updated.paused is False
    assert updated.updated_at == LATER
    stored = await store.get_creator_account(account.guild_id, account.account_id)
    assert stored is not None and stored.paused is False


@pytest.mark.asyncio
async def test_get_creator_account_returns_none_when_missing(store: SQLiteStore) -> None:
    assert await store.get_creator_account(111, "missing-account") is None


@pytest.mark.asyncio
async def test_list_creator_accounts_for_owner_is_guild_scoped(store: SQLiteStore) -> None:
    mine = creator_account(guild_id=111, owner_member_id=222, external_id="UC-mine")
    other_owner = creator_account(guild_id=111, owner_member_id=333, external_id="UC-other")
    other_guild = creator_account(guild_id=999, owner_member_id=222, external_id="UC-elsewhere")
    for account in (mine, other_owner, other_guild):
        await store.save_creator_account(account)

    owned = await store.list_creator_accounts_for_owner(111, 222)
    assert [account.account_id for account in owned] == [mine.account_id]

    guild_accounts = await store.list_creator_accounts(111)
    assert {account.account_id for account in guild_accounts} == {
        mine.account_id,
        other_owner.account_id,
    }


@pytest.mark.asyncio
async def test_creator_profile_is_created_and_kept_on_account_save(store: SQLiteStore) -> None:
    await store.save_creator_account(creator_account(guild_id=111, owner_member_id=222))

    profile = await store.get_creator_profile(111, 222)
    assert profile is not None
    assert profile.guild_id == 111
    assert profile.owner_member_id == 222
    assert await store.get_creator_profile(111, 999) is None


@pytest.mark.asyncio
async def test_transfer_creator_account_changes_owner_and_creates_new_profile(
    store: SQLiteStore,
) -> None:
    account = creator_account(guild_id=111, owner_member_id=222)
    await store.save_creator_account(account)

    transferred = await store.transfer_creator_account(111, account.account_id, 444, LATER)

    assert transferred.owner_member_id == 444
    assert transferred.updated_at == LATER
    assert (await store.get_creator_profile(111, 444)) is not None
    with pytest.raises(ValueError, match="not found"):
        await store.transfer_creator_account(111, "missing-account", 444, LATER)


@pytest.mark.asyncio
async def test_creator_route_round_trips_and_updates_in_place(store: SQLiteStore) -> None:
    account = creator_account()
    await store.save_creator_account(account)
    route = CreatorRoute(
        guild_id=111,
        account_id=account.account_id,
        content_kind=ContentKind.VIDEO,
        channel_id=555,
        mention_role_id=None,
        updated_at=NOW,
    )
    saved = await store.save_creator_route(route)
    assert saved.channel_id == 555
    assert saved.mention_role_id is None

    updated = await store.save_creator_route(replace(route, mention_role_id=777, updated_at=LATER))
    assert updated.mention_role_id == 777

    routes = await store.list_creator_routes(111, account.account_id)
    assert [item.content_kind for item in routes] == [ContentKind.VIDEO]


@pytest.mark.asyncio
async def test_creator_registry_receipts_are_recorded_redacted_and_guild_scoped(
    store: SQLiteStore,
) -> None:
    account = creator_account()
    await store.save_creator_account(account)
    await store.record_creator_registry_receipt(
        guild_id=111,
        receipt_id="creator-registry:receipt-1",
        account_id=account.account_id,
        action="add_account",
        actor_member_id=222,
        detail={"secret": "raw-token-value", "paused": True},
        created_at=NOW,
    )

    receipts = await store.list_creator_registry_receipts(111, account.account_id)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.action == "add_account"
    assert receipt.actor_member_id == 222
    assert receipt.detail["secret"] == "[REDACTED]"
    assert receipt.detail["paused"] is True

    assert await store.list_creator_registry_receipts(222, account.account_id) == []
