from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.creator_signals import CreatorAccount, Platform
from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_issue_and_consume_oauth_attempt_round_trips(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=False, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1,
        member_id=2,
        account_id="acct-1",
        platform="tiktok",
        capability="account",
        redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now,
        ttl=timedelta(minutes=10),
    )
    attempt = await store.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    assert attempt is not None
    assert attempt.guild_id == 1
    assert attempt.member_id == 2
    assert attempt.account_id == "acct-1"
    assert attempt.platform == "tiktok"
    assert attempt.capability == "account"
    assert attempt.redirect_uri == "https://example.test/callbacks/tiktok/authorize"
    await store.close()


async def test_consume_oauth_attempt_is_single_use(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=False, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    first = await store.consume_oauth_attempt(token, now=now)
    second = await store.consume_oauth_attempt(token, now=now)
    assert first is not None
    assert second is None
    await store.close()


async def test_consume_oauth_attempt_rejects_expired(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=False, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    late = await store.consume_oauth_attempt(token, now=now + timedelta(minutes=11))
    assert late is None
    await store.close()


async def test_consume_oauth_attempt_rejects_unknown_token(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    result = await store.consume_oauth_attempt(secrets.token_urlsafe(32), now=now)
    assert result is None
    await store.close()


async def test_consume_oauth_attempt_survives_a_new_store_handle(tmp_path):
    """Durability across a simulated restart: a fresh SQLiteStore over the same
    file must still enforce single-use and expiry."""
    store_a = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store_a.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=False, created_at=now, updated_at=now,
        )
    )
    token = await store_a.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )
    await store_a.close()

    store_b = await SQLiteStore.open(tmp_path / "test.db")
    await store_b.initialize()
    first = await store_b.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    second = await store_b.consume_oauth_attempt(token, now=now + timedelta(minutes=1))
    assert first is not None
    assert second is None
    await store_b.close()


async def test_purge_oauth_attempts_removes_old_consumed_and_expired_unconsumed(tmp_path):
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)

    for account_id in ["a", "b", "c", "d"]:
        await store.save_creator_account(
            CreatorAccount(
                guild_id=1, account_id=account_id, owner_member_id=2,
                platform=Platform.TIKTOK, handle=f"handle_{account_id}",
                canonical_url=f"https://tiktok.com/@handle_{account_id}",
                external_id=f"handle_{account_id}", paused=False, created_at=now, updated_at=now,
            )
        )

    old_consumed = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="a", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=40), ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(old_consumed, now=now - timedelta(days=40))

    recent_consumed = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="b", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=1), ttl=timedelta(minutes=10),
    )
    await store.consume_oauth_attempt(recent_consumed, now=now - timedelta(days=1))

    old_unconsumed_expired = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="c", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now - timedelta(days=3), ttl=timedelta(minutes=10),
    )

    await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="d", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb",
        now=now, ttl=timedelta(minutes=10),
    )

    deleted = await store.purge_oauth_attempts(
        now, consumed_retention=timedelta(days=30), unconsumed_grace=timedelta(days=1)
    )
    assert deleted == 2

    # Recent-consumed and still-live attempts survive; an unknown consume on the
    # purged tokens still correctly returns None (never present, never resurrected).
    assert await store.consume_oauth_attempt(old_consumed, now=now) is None
    assert await store.consume_oauth_attempt(old_unconsumed_expired, now=now) is None
    await store.close()


async def test_consume_oauth_attempt_concurrent_double_consume_only_one_wins(tmp_path):
    """Finding #5: a genuine race, not a sequential double-call -- two concurrent
    `consume_oauth_attempt` calls for the same token must never both succeed."""
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=False, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="acct-1", platform="tiktok",
        capability="account", redirect_uri="https://x.test/cb", now=now,
        ttl=timedelta(minutes=10),
    )

    results = await asyncio.gather(
        store.consume_oauth_attempt(token, now=now),
        store.consume_oauth_attempt(token, now=now),
    )
    successes = [result for result in results if result is not None]
    failures = [result for result in results if result is None]
    assert len(successes) == 1
    assert len(failures) == 1
    await store.close()
