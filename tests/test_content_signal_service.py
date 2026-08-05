"""Behavioral tests for `ContentSignalService.ingest_page`.

Covers baseline suppression, new-item and lifecycle-transition delivery claiming,
malformed-item resilience, and concurrent/duplicate ingestion safety — exercising the
service's connector-page parsing on top of the storage-layer atomicity already covered
by `tests/test_content_signal_storage.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    ContentKind,
    ContentState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import ConnectorPage
from krubit.services.content_signals import ContentSignalService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
EVEN_LATER = NOW + timedelta(minutes=10)


def account() -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.YOUTUBE, "UC-one"),
        owner_member_id=222,
        platform=Platform.YOUTUBE,
        handle="krucialstudios",
        canonical_url="https://www.youtube.com/@krucialstudios",
        external_id="UC-one",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def page(
    *, items: tuple[Mapping[str, JSONValue], ...], cursor: str | None
) -> ConnectorPage:
    return ConnectorPage(items=items, next_cursor=cursor)


def video(
    external_id: str, *, state: ContentState = ContentState.PUBLISHED
) -> Mapping[str, JSONValue]:
    return {
        "external_id": external_id,
        "kind": ContentKind.VIDEO.value,
        "state": state.value,
        "canonical_url": f"https://example.com/videos/{external_id}",
        "title": f"Video {external_id}",
    }


def live_stream(
    external_id: str, *, state: ContentState
) -> Mapping[str, JSONValue]:
    return {
        "external_id": external_id,
        "kind": ContentKind.LIVE.value,
        "state": state.value,
        "canonical_url": f"https://example.com/live/{external_id}",
        "title": f"Live {external_id}",
    }


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    await value.save_creator_account(account())
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_first_page_establishes_baseline_without_delivery(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    result = await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    assert result.plans == ()
    assert (await store.get_content_cursor(111, account().account_id)).value == "c1"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_new_item_after_baseline_claims_one_delivery(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    result = await service.ingest_page(
        account(), page(items=(video("v2"), video("v1")), cursor="c2"), now=LATER
    )
    assert [plan.event.external_id for plan in result.plans] == ["v2"]


@pytest.mark.asyncio
async def test_ingestion_result_carries_the_resulting_cursor(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    result = await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    assert result.account_id == account().account_id
    assert result.cursor.value == "c1"
    assert result.cursor.baselined_at == NOW


@pytest.mark.asyncio
async def test_scheduled_to_live_transition_claims_exactly_one_delivery(
    store: SQLiteStore,
) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    scheduled = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.SCHEDULED),), cursor="c2"),
        now=LATER,
    )
    assert scheduled.plans == ()

    live = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.LIVE),), cursor="c3"),
        now=EVEN_LATER,
    )
    assert [plan.event.external_id for plan in live.plans] == ["s1"]
    assert live.plans[0].event.state is ContentState.LIVE
    assert live.plans[0].delivery.status == "pending"
    assert live.plans[0].delivery.transition_seq == 1

    # Re-observing the same live state again must not reclaim a second delivery.
    repeat = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.LIVE),), cursor="c4"),
        now=EVEN_LATER,
    )
    assert repeat.plans == ()


@pytest.mark.asyncio
async def test_recurring_live_transition_claims_a_new_delivery_each_time(
    store: SQLiteStore,
) -> None:
    """A restart (`live -> ended -> live`) claims a second, independent delivery."""
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    first_live = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.LIVE),), cursor="c2"),
        now=LATER,
    )
    assert [plan.event.external_id for plan in first_live.plans] == ["s1"]
    assert first_live.plans[0].delivery.transition_seq == 1

    ended = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.ENDED),), cursor="c3"),
        now=EVEN_LATER,
    )
    assert ended.plans == ()

    second_live = await service.ingest_page(
        account(),
        page(items=(live_stream("s1", state=ContentState.LIVE),), cursor="c4"),
        now=EVEN_LATER + timedelta(minutes=1),
    )
    assert [plan.event.external_id for plan in second_live.plans] == ["s1"]
    assert second_live.plans[0].delivery.transition_seq == 2

    history = await store.list_content_deliveries(111, Platform.YOUTUBE, "s1")
    assert [delivery.transition_seq for delivery in history] == [1, 2]


@pytest.mark.asyncio
async def test_malformed_item_is_skipped_receipted_and_does_not_abort_the_page(
    store: SQLiteStore,
) -> None:
    service = ContentSignalService(store)
    malformed_kind: Mapping[str, JSONValue] = {
        "external_id": "bad-kind",
        "kind": "movie",
        "state": "published",
        "canonical_url": "https://example.com/videos/bad-kind",
    }
    missing_external_id: Mapping[str, JSONValue] = {
        "kind": "video",
        "state": "published",
        "canonical_url": "https://example.com/videos/missing",
    }
    result = await service.ingest_page(
        account(),
        page(items=(video("v1"), malformed_kind, missing_external_id), cursor="c1"),
        now=NOW,
    )

    assert result.plans == ()  # baseline page: nothing claims regardless
    event = await store.get_content_event(111, Platform.YOUTUBE, "v1")
    assert event is not None

    receipts = await store.list_content_receipts(111, account().account_id)
    assert len(receipts) == 2
    assert all(receipt.action == "malformed_event" for receipt in receipts)
    external_ids = {receipt.external_id for receipt in receipts}
    assert external_ids == {"bad-kind", None}


@pytest.mark.asyncio
async def test_malformed_item_after_baseline_does_not_block_a_genuine_new_claim(
    store: SQLiteStore,
) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)
    malformed: Mapping[str, JSONValue] = {
        "external_id": "bad",
        "kind": "not-a-real-kind",
        "state": "published",
        "canonical_url": "https://example.com/videos/bad",
    }
    result = await service.ingest_page(
        account(), page(items=(video("v2"), malformed), cursor="c2"), now=LATER
    )
    assert [plan.event.external_id for plan in result.plans] == ["v2"]
    receipts = await store.list_content_receipts(111, account().account_id)
    assert [receipt.external_id for receipt in receipts] == ["bad"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_page_ingestion_claims_exactly_one_delivery(
    store: SQLiteStore,
) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(account(), page(items=(video("v1"),), cursor="c1"), now=NOW)

    async def ingest_once() -> tuple[object, ...]:
        result = await service.ingest_page(
            account(), page(items=(video("v2"),), cursor="c2"), now=LATER
        )
        return result.plans

    results = await asyncio.gather(ingest_once(), ingest_once(), ingest_once())
    claimed = [plan for plans in results for plan in plans]

    assert len(claimed) == 1
    delivery = await store.get_content_delivery(111, Platform.YOUTUBE, "v2")
    assert delivery is not None
    assert delivery.attempt == 1
