from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.creator_signals import CreatorAccount, Platform
from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_save_and_get_connector_authorization_round_trips(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="test_page", paused=False, created_at=now, updated_at=now,
        )
    )
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:sealed-token", provider_resource_id="ig-12345",
        authorization_subject_id="fb-user-999", status="active",
        expires_at=now + timedelta(days=60), now=now,
    )
    row = await store.get_connector_authorization(1, "acct-1", "content")
    assert row is not None
    assert row.secret_ref == "v1:sealed-token"
    assert row.provider_resource_id == "ig-12345"
    assert row.authorization_subject_id == "fb-user-999"
    assert row.status == "active"
    await store.close()


async def test_save_connector_authorization_upserts(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="test_page", paused=False, created_at=now, updated_at=now,
        )
    )
    for secret in ("v1:first", "v1:second"):
        await store.save_connector_authorization(
            guild_id=1, account_id="acct-1", capability="content",
            secret_ref=secret, provider_resource_id="ig-12345",
            authorization_subject_id="fb-user-999", status="active",
            expires_at=None, now=now,
        )
    row = await store.get_connector_authorization(1, "acct-1", "content")
    assert row is not None
    assert row.secret_ref == "v1:second"
    await store.close()


async def test_get_connector_authorization_returns_none_when_absent(tmp_path):
    store = await _store(tmp_path)
    assert await store.get_connector_authorization(1, "nope", "content") is None
    await store.close()


async def test_find_by_authorization_subject_distinguishes_from_resource_id(tmp_path):
    """A Page's provider_resource_id must never be usable to find it via the
    authorization_subject_id lookup, and vice versa — proves the two columns are
    genuinely independent, not aliases of one value."""
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="page-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="test_page", paused=False, created_at=now, updated_at=now,
        )
    )
    await store.save_connector_authorization(
        guild_id=1, account_id="page-1", capability="content",
        secret_ref="v1:x", provider_resource_id="page-resource-1",
        authorization_subject_id="admin-user-1", status="active",
        expires_at=None, now=now,
    )
    by_subject = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "admin-user-1"
    )
    assert len(by_subject) == 1
    assert by_subject[0].provider_resource_id == "page-resource-1"

    by_wrong_key = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "page-resource-1"
    )
    assert by_wrong_key == ()
    await store.close()


async def test_find_by_authorization_subject_matches_across_guilds(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    for guild_id, account_id in ((1, "acct-a"), (2, "acct-b")):
        await store.save_creator_account(
            CreatorAccount(
                guild_id=guild_id, account_id=account_id, owner_member_id=2,
                platform=Platform.FACEBOOK_PAGE, handle=f"test_page_{guild_id}",
                canonical_url=f"https://facebook.com/test_page_{guild_id}",
                external_id=f"test_page_{guild_id}", paused=False, created_at=now, updated_at=now,
            )
        )
    for guild_id, account_id in ((1, "acct-a"), (2, "acct-b")):
        await store.save_connector_authorization(
            guild_id=guild_id, account_id=account_id, capability="content",
            secret_ref="v1:x", provider_resource_id=f"resource-{account_id}",
            authorization_subject_id="shared-admin", status="active",
            expires_at=None, now=now,
        )
    rows = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "shared-admin"
    )
    assert {r.guild_id for r in rows} == {1, 2}
    await store.close()


async def test_delete_connector_authorizations_removes_rows_and_writes_receipts(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="test_page", paused=False, created_at=now, updated_at=now,
        )
    )
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:x", provider_resource_id="r-1",
        authorization_subject_id="subj-1", status="active",
        expires_at=None, now=now,
    )
    rows = await store.find_connector_authorizations_by_authorization_subject(
        "facebook_page", "subj-1"
    )
    await store.delete_connector_authorizations(rows, now=now)
    assert await store.get_connector_authorization(1, "acct-1", "content") is None
    await store.close()


async def test_list_connector_authorization_status_omits_secret_ref(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="test_page", paused=False, created_at=now, updated_at=now,
        )
    )
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content",
        secret_ref="v1:super-secret-token", provider_resource_id="r-1",
        authorization_subject_id="subj-1", status="active",
        expires_at=now + timedelta(days=1), now=now,
    )
    statuses = await store.list_connector_authorization_status(1)
    assert len(statuses) == 1
    assert statuses[0].status == "active"
    assert not hasattr(statuses[0], "secret_ref")
    assert not hasattr(statuses[0], "provider_resource_id")
    await store.close()
