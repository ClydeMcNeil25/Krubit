"""Integration tests for the durable content ledger's SQLite storage layer.

Exercises `SQLiteStore.record_content_observations` directly (the atomic baseline/
transition/claim primitive `ContentSignalService` delegates to), plus the plain
accessor methods (`get_content_cursor`, `get_content_event`, `get_content_delivery`,
`list_pending_content_deliveries`, content receipts) built on top of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    ContentKind,
    ContentObservation,
    ContentState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
EVEN_LATER = NOW + timedelta(minutes=10)

GUILD_ID = 111
EXTERNAL_ACCOUNT_ID = "UC-one"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def creator_account(
    *, guild_id: int = GUILD_ID, external_id: str = EXTERNAL_ACCOUNT_ID
) -> CreatorAccount:
    return CreatorAccount(
        guild_id=guild_id,
        account_id=creator_account_id(Platform.YOUTUBE, external_id),
        owner_member_id=222,
        platform=Platform.YOUTUBE,
        handle="krucialstudios",
        canonical_url="https://www.youtube.com/@krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def observation(
    external_id: str,
    *,
    kind: ContentKind = ContentKind.VIDEO,
    state: ContentState = ContentState.PUBLISHED,
    title: str | None = "A video",
) -> ContentObservation:
    return ContentObservation(
        external_id=external_id,
        content_kind=kind,
        state=state,
        canonical_url=f"https://example.com/videos/{external_id}",
        title=title,
    )


async def _seeded_account(store: SQLiteStore, *, guild_id: int = GUILD_ID) -> CreatorAccount:
    account = creator_account(guild_id=guild_id)
    return await store.save_creator_account(account)


@pytest.mark.asyncio
async def test_get_content_cursor_returns_none_when_missing(store: SQLiteStore) -> None:
    assert await store.get_content_cursor(GUILD_ID, "missing-account") is None


@pytest.mark.asyncio
async def test_baseline_page_stores_identities_without_claiming_delivery(
    store: SQLiteStore,
) -> None:
    account = await _seeded_account(store)
    cursor, plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1"),),
        cursor_value="c1",
        now=NOW,
    )

    assert plans == ()
    assert cursor.value == "c1"
    assert cursor.baselined_at == NOW

    event = await store.get_content_event(GUILD_ID, Platform.YOUTUBE, "v1")
    assert event is not None
    assert event.state is ContentState.PUBLISHED
    assert event.first_observed_at == NOW
    assert await store.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v1") is None
    assert await store.list_pending_content_deliveries(GUILD_ID) == []


@pytest.mark.asyncio
async def test_new_item_after_baseline_claims_exactly_one_delivery(store: SQLiteStore) -> None:
    account = await _seeded_account(store)
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1"),),
        cursor_value="c1",
        now=NOW,
    )

    cursor, plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2"), observation("v1")),
        cursor_value="c2",
        now=LATER,
    )

    assert [plan.event.external_id for plan in plans] == ["v2"]
    assert cursor.value == "c2"
    assert cursor.baselined_at == NOW  # unchanged from the first successful page

    delivery = await store.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v2")
    assert delivery is not None
    assert delivery.status == "pending"
    assert delivery.account_id == account.account_id
    assert await store.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v1") is None
    assert [d.external_id for d in await store.list_pending_content_deliveries(GUILD_ID)] == ["v2"]


@pytest.mark.asyncio
async def test_reobserving_same_state_does_not_reclaim_or_duplicate(store: SQLiteStore) -> None:
    account = await _seeded_account(store)
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1"),),
        cursor_value="c1",
        now=NOW,
    )
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2"),),
        cursor_value="c2",
        now=LATER,
    )

    cursor, plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2"),),
        cursor_value="c3",
        now=EVEN_LATER,
    )

    assert plans == ()
    assert cursor.value == "c3"
    assert await store.list_pending_content_deliveries(GUILD_ID) == [
        await store.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v2")
    ]


@pytest.mark.asyncio
async def test_scheduled_to_live_transition_claims_delivery_once(store: SQLiteStore) -> None:
    account = await _seeded_account(store)
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1", state=ContentState.PUBLISHED),),
        cursor_value="c1",
        now=NOW,
    )
    scheduled_cursor, scheduled_plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(
            observation("v2", kind=ContentKind.LIVE, state=ContentState.SCHEDULED),
        ),
        cursor_value="c2",
        now=LATER,
    )
    assert scheduled_plans == ()
    del scheduled_cursor

    live_cursor, live_plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2", kind=ContentKind.LIVE, state=ContentState.LIVE),),
        cursor_value="c3",
        now=EVEN_LATER,
    )
    assert [plan.event.external_id for plan in live_plans] == ["v2"]
    del live_cursor

    # Ending the stream is a lifecycle update, not a new publish/live transition.
    ended_cursor, ended_plans = await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2", kind=ContentKind.LIVE, state=ContentState.ENDED),),
        cursor_value="c4",
        now=EVEN_LATER + timedelta(minutes=1),
    )
    assert ended_plans == ()
    del ended_cursor
    event = await store.get_content_event(GUILD_ID, Platform.YOUTUBE, "v2")
    assert event is not None
    assert event.state is ContentState.ENDED


@pytest.mark.asyncio
async def test_content_events_are_guild_scoped_despite_shared_external_id(
    store: SQLiteStore,
) -> None:
    first_account = await _seeded_account(store, guild_id=111)
    second_account = await _seeded_account(store, guild_id=999)

    await store.record_content_observations(
        guild_id=111,
        account_id=first_account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("shared-id"),),
        cursor_value="c1",
        now=NOW,
    )
    await store.record_content_observations(
        guild_id=999,
        account_id=second_account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("shared-id"),),
        cursor_value="c1",
        now=NOW,
    )

    assert await store.get_content_event(111, Platform.YOUTUBE, "shared-id") is not None
    assert await store.get_content_event(999, Platform.YOUTUBE, "shared-id") is not None
    first_cursor = await store.get_content_cursor(111, first_account.account_id)
    second_cursor = await store.get_content_cursor(999, second_account.account_id)
    assert first_cursor != second_cursor


@pytest.mark.asyncio
async def test_concurrent_duplicate_ingestion_claims_exactly_one_delivery(
    store: SQLiteStore,
) -> None:
    account = await _seeded_account(store)
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1"),),
        cursor_value="c1",
        now=NOW,
    )

    async def ingest_once() -> tuple[object, ...]:
        _, plans = await store.record_content_observations(
            guild_id=GUILD_ID,
            account_id=account.account_id,
            platform=Platform.YOUTUBE,
            observations=(observation("v2"),),
            cursor_value="c2",
            now=LATER,
        )
        return plans

    results = await asyncio.gather(ingest_once(), ingest_once(), ingest_once())
    claimed = [plan for result in results for plan in result]

    assert len(claimed) == 1
    delivery = await store.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v2")
    assert delivery is not None
    assert delivery.attempt == 1


@pytest.mark.asyncio
async def test_content_cursor_persists_across_reopened_database(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.db"
    store = await SQLiteStore.open(db_path)
    await store.initialize()
    account = await _seeded_account(store)
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v1"),),
        cursor_value="c1",
        now=NOW,
    )
    await store.record_content_observations(
        guild_id=GUILD_ID,
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        observations=(observation("v2"),),
        cursor_value="c2",
        now=LATER,
    )
    await store.close()

    reopened = await SQLiteStore.open(db_path)
    await reopened.initialize()
    try:
        cursor = await reopened.get_content_cursor(GUILD_ID, account.account_id)
        assert cursor is not None
        assert cursor.value == "c2"
        assert cursor.baselined_at == NOW

        delivery = await reopened.get_content_delivery(GUILD_ID, Platform.YOUTUBE, "v2")
        assert delivery is not None
        assert delivery.status == "pending"

        event = await reopened.get_content_event(GUILD_ID, Platform.YOUTUBE, "v1")
        assert event is not None
        assert event.last_observed_at == NOW
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_content_receipts_are_recorded_redacted_and_guild_scoped(
    store: SQLiteStore,
) -> None:
    account = await _seeded_account(store)
    await store.record_content_receipt(
        guild_id=GUILD_ID,
        receipt_id="content:receipt-1",
        account_id=account.account_id,
        platform=Platform.YOUTUBE,
        external_id="v1",
        action="malformed_event",
        detail={"error": "kind must be a string", "secret": "raw-token-value"},
        created_at=NOW,
    )

    receipts = await store.list_content_receipts(GUILD_ID, account.account_id)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.action == "malformed_event"
    assert receipt.platform is Platform.YOUTUBE
    assert receipt.detail["secret"] == "[REDACTED]"
    assert receipt.detail["error"] == "kind must be a string"
    assert await store.list_content_receipts(999, account.account_id) == []
