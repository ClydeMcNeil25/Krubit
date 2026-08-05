from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    ContentKind,
    ContentState,
    CreatorAccount,
    CreatorRoute,
    Platform,
    creator_account_id,
)
from krubit.integrations.base import ConnectorPage
from krubit.services.content_signals import ContentSignalService
from krubit.services.creator_analytics import CreatorAnalyticsService
from krubit.storage.sqlite import SQLiteStore

GUILD_ID = 111
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def account(owner_member_id: int = 222) -> CreatorAccount:
    return CreatorAccount(
        guild_id=GUILD_ID,
        account_id=creator_account_id(Platform.TWITCH, "twitch-analytics"),
        owner_member_id=owner_member_id,
        platform=Platform.TWITCH,
        handle="Analytics Creator",
        canonical_url="https://www.twitch.tv/analyticscreator",
        external_id="twitch-analytics",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(GUILD_ID, True)
    await store.save_creator_account(account())
    await store.save_creator_route(
        CreatorRoute(
            guild_id=GUILD_ID,
            account_id=account().account_id,
            content_kind=ContentKind.LIVE,
            channel_id=444,
            mention_role_id=None,
            updated_at=NOW,
        )
    )
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_counts_reflect_claimed_and_updated_deliveries(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(
        account(),
        ConnectorPage(
            items=(
                {
                    "external_id": "stream-1",
                    "kind": ContentKind.LIVE.value,
                    "state": ContentState.SCHEDULED.value,
                    "canonical_url": "https://www.twitch.tv/analyticscreator/stream-1",
                },
            )
        ),
        now=NOW,
    )
    result = await service.ingest_page(
        account(),
        ConnectorPage(
            items=(
                {
                    "external_id": "stream-1",
                    "kind": ContentKind.LIVE.value,
                    "state": ContentState.LIVE.value,
                    "canonical_url": "https://www.twitch.tv/analyticscreator/stream-1",
                },
            )
        ),
        now=NOW,
    )
    plan = result.plans[0]
    analytics = CreatorAnalyticsService(store)

    counts = await analytics.delivery_counts(GUILD_ID, account().account_id)
    assert counts.pending == 1
    assert counts.delivered == 0
    assert counts.total == 1

    delivered_at = NOW + timedelta(seconds=5)
    await store.update_content_delivery(
        guild_id=GUILD_ID,
        platform=plan.delivery.platform,
        external_id=plan.delivery.external_id,
        transition_seq=plan.delivery.transition_seq,
        status="delivered",
        attempt=1,
        channel_id=444,
        message_id=9001,
        now=delivered_at,
    )

    counts = await analytics.delivery_counts(GUILD_ID, account().account_id)
    assert counts.delivered == 1
    assert counts.pending == 0

    latency = await analytics.average_delivery_latency_seconds(GUILD_ID, account().account_id)
    assert latency == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_average_latency_is_none_without_any_delivered_item(store: SQLiteStore) -> None:
    analytics = CreatorAnalyticsService(store)
    latency = await analytics.average_delivery_latency_seconds(GUILD_ID, account().account_id)
    assert latency is None


@pytest.mark.asyncio
async def test_suppression_reasons_reflect_malformed_item_receipts(store: SQLiteStore) -> None:
    service = ContentSignalService(store)
    await service.ingest_page(
        account(),
        ConnectorPage(
            items=(
                {
                    # Missing required "canonical_url" -> malformed, redacted receipt.
                    "external_id": "bad-item",
                    "kind": ContentKind.LIVE.value,
                    "state": ContentState.SCHEDULED.value,
                },
            )
        ),
        now=NOW,
    )
    analytics = CreatorAnalyticsService(store)
    reasons = await analytics.suppression_reasons(GUILD_ID, account().account_id)
    assert reasons == ("malformed_event",)


@pytest.mark.asyncio
async def test_own_profile_returns_only_the_owners_accounts(store: SQLiteStore) -> None:
    other = CreatorAccount(
        guild_id=GUILD_ID,
        account_id=creator_account_id(Platform.YOUTUBE, "yt-other"),
        owner_member_id=333,
        platform=Platform.YOUTUBE,
        handle="Other Creator",
        canonical_url="https://www.youtube.com/@othercreator",
        external_id="yt-other",
        paused=True,
        created_at=NOW,
        updated_at=NOW,
    )
    await store.save_creator_account(other)
    analytics = CreatorAnalyticsService(store)

    owned = await analytics.own_profile(GUILD_ID, 222)
    assert [a.account_id for a in owned] == [account().account_id]

    owned_other = await analytics.own_profile(GUILD_ID, 333)
    assert [a.account_id for a in owned_other] == [other.account_id]


@pytest.mark.asyncio
async def test_account_report_bundles_every_factual_metric(store: SQLiteStore) -> None:
    analytics = CreatorAnalyticsService(store)
    report = await analytics.account_report(GUILD_ID, account().account_id)
    assert report is not None
    assert report.account.account_id == account().account_id
    assert report.delivery_counts.total == 0
    assert report.average_delivery_latency_seconds is None
    assert report.cursor is None


@pytest.mark.asyncio
async def test_account_report_is_none_for_an_unregistered_account(store: SQLiteStore) -> None:
    analytics = CreatorAnalyticsService(store)
    report = await analytics.account_report(GUILD_ID, "does-not-exist")
    assert report is None


@pytest.mark.asyncio
async def test_quota_history_lists_recorded_mention_budget_receipts(store: SQLiteStore) -> None:
    await store.record_mention_receipt(
        guild_id=GUILD_ID,
        receipt_id="mention:one",
        budget_kind="live_everyone",
        period_key=NOW.date().isoformat(),
        outcome="consumed",
        platform=Platform.TWITCH,
        external_id="stream-1",
        created_at=NOW,
    )
    analytics = CreatorAnalyticsService(store)
    history = await analytics.quota_history(GUILD_ID)
    assert len(history) == 1
    assert history[0].outcome == "consumed"
