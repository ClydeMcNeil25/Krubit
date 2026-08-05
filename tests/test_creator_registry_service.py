"""Authority and lifecycle tests for `CreatorRegistry`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import Platform, RecognizedAccountUrl, creator_account_id
from krubit.services.creator_registry import CreatorAuthorityError, CreatorRegistry
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)

RECOGNIZED = RecognizedAccountUrl(
    platform=Platform.YOUTUBE,
    handle="krucialstudios",
    canonical_url="https://www.youtube.com/@krucialstudios",
)
EXTERNAL_ID = "UC-one"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def registry(store: SQLiteStore) -> CreatorRegistry:
    return CreatorRegistry(store)


@pytest.mark.asyncio
async def test_new_account_starts_paused(registry: CreatorRegistry) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )

    assert account.paused is True
    assert account.account_id == creator_account_id(Platform.YOUTUBE, EXTERNAL_ID)
    assert account.owner_member_id == 222


@pytest.mark.asyncio
async def test_creator_role_can_add_self_but_not_another_member(
    registry: CreatorRegistry,
) -> None:
    own = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )
    assert own.owner_member_id == 222

    with pytest.raises(CreatorAuthorityError, match="administrator authority"):
        await registry.add_account(
            guild_id=111,
            actor_member_id=222,
            owner_member_id=333,
            actor_is_admin=False,
            actor_has_creator_role=True,
            recognized=RECOGNIZED,
            resolved_external_id="UC-two",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_self_service_without_creator_role_is_denied(registry: CreatorRegistry) -> None:
    with pytest.raises(CreatorAuthorityError, match="Creator role"):
        await registry.add_account(
            guild_id=111,
            actor_member_id=222,
            owner_member_id=222,
            actor_is_admin=False,
            actor_has_creator_role=False,
            recognized=RECOGNIZED,
            resolved_external_id=EXTERNAL_ID,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_admin_can_add_account_for_another_member(registry: CreatorRegistry) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=999,
        owner_member_id=333,
        actor_is_admin=True,
        actor_has_creator_role=False,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )
    assert account.owner_member_id == 333


@pytest.mark.asyncio
async def test_owner_with_creator_role_can_pause_and_resume_their_own_account(
    registry: CreatorRegistry,
) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )

    resumed = await registry.resume_account(
        guild_id=111,
        actor_member_id=222,
        account_id=account.account_id,
        actor_is_admin=False,
        actor_has_creator_role=True,
        now=LATER,
    )
    assert resumed.paused is False

    paused = await registry.pause_account(
        guild_id=111,
        actor_member_id=222,
        account_id=account.account_id,
        actor_is_admin=False,
        actor_has_creator_role=True,
        now=LATER,
    )
    assert paused.paused is True


@pytest.mark.asyncio
async def test_non_owner_cannot_pause_another_members_account(
    registry: CreatorRegistry,
) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )

    with pytest.raises(CreatorAuthorityError, match="administrator authority"):
        await registry.pause_account(
            guild_id=111,
            actor_member_id=333,
            account_id=account.account_id,
            actor_is_admin=False,
            actor_has_creator_role=True,
            now=LATER,
        )


@pytest.mark.asyncio
async def test_admin_can_pause_any_members_account(registry: CreatorRegistry) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )

    paused = await registry.pause_account(
        guild_id=111,
        actor_member_id=999,
        account_id=account.account_id,
        actor_is_admin=True,
        actor_has_creator_role=False,
        now=LATER,
    )
    assert paused.paused is True


@pytest.mark.asyncio
async def test_pausing_a_missing_account_raises(registry: CreatorRegistry) -> None:
    with pytest.raises(ValueError, match="not found"):
        await registry.pause_account(
            guild_id=111,
            actor_member_id=222,
            account_id="missing-account",
            actor_is_admin=True,
            actor_has_creator_role=False,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_transfer_requires_administrator_authority(registry: CreatorRegistry) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )

    with pytest.raises(CreatorAuthorityError, match="administrator authority"):
        await registry.transfer_account(
            guild_id=111,
            actor_member_id=222,
            account_id=account.account_id,
            new_owner_member_id=444,
            actor_is_admin=False,
            now=LATER,
        )

    transferred = await registry.transfer_account(
        guild_id=111,
        actor_member_id=999,
        account_id=account.account_id,
        new_owner_member_id=444,
        actor_is_admin=True,
        now=LATER,
    )
    assert transferred.owner_member_id == 444


@pytest.mark.asyncio
async def test_every_authority_decision_produces_a_redacted_receipt(
    store: SQLiteStore, registry: CreatorRegistry
) -> None:
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=RECOGNIZED,
        resolved_external_id=EXTERNAL_ID,
        now=NOW,
    )
    await registry.pause_account(
        guild_id=111,
        actor_member_id=222,
        account_id=account.account_id,
        actor_is_admin=False,
        actor_has_creator_role=True,
        now=LATER,
    )
    await registry.transfer_account(
        guild_id=111,
        actor_member_id=999,
        account_id=account.account_id,
        new_owner_member_id=444,
        actor_is_admin=True,
        now=LATER,
    )

    receipts = await store.list_creator_registry_receipts(111, account.account_id)
    assert receipts[-1].action == "add_account"
    assert {receipt.action for receipt in receipts} == {
        "add_account",
        "pause_account",
        "transfer_account",
    }
    assert all("secret" not in receipt.detail for receipt in receipts)
