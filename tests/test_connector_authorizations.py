from __future__ import annotations

import json
import sqlite3
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

    # Finding #5: the written receipt itself must be redacted -- neither the sealed
    # secret nor either identifier may appear in it, only capability/account_id, as
    # the implementation already claims to do.
    receipts = await store.list_creator_registry_receipts(1, "acct-1")
    deauthorized = next(r for r in receipts if r.action == "connector_deauthorized")
    assert deauthorized.detail["account_id"] == "acct-1"
    assert deauthorized.detail["platform_capability"] == "content"
    detail_json = json.dumps(deauthorized.detail, sort_keys=True)
    assert "v1:x" not in detail_json
    assert "r-1" not in detail_json
    assert "subj-1" not in detail_json
    assert "secret_ref" not in deauthorized.detail
    assert "provider_resource_id" not in deauthorized.detail
    assert "authorization_subject_id" not in deauthorized.detail
    await store.close()


async def test_initialize_migrates_a_pre_existing_seven_column_connector_authorizations_table(
    tmp_path,
):
    """CRITICAL FIX #1 regression test: `connector_authorizations` shipped in a
    pre-this-branch release with 7 columns (no `provider_resource_id`/
    `authorization_subject_id`). `CREATE TABLE IF NOT EXISTS` is a no-op against an
    already-deployed database with that shape, so `initialize()` must explicitly
    backfill the two new columns via `ALTER TABLE` -- proven here against a database
    seeded with exactly the old 7-column shape, not the fresh-database path every
    other test in this file exercises.
    """
    db_path = tmp_path / "pre_existing.db"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            """
            CREATE TABLE connector_authorizations (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                secret_ref TEXT,
                status TEXT NOT NULL,
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id, capability)
            )
            """
        )
        raw.commit()
    finally:
        raw.close()

    store = await SQLiteStore.open(db_path)
    await store.initialize()

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
