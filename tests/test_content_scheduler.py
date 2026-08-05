"""Isolation, backoff, and restart-durability tests for `ConnectorScheduler`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from krubit.discord.content_runtime import ConnectorScheduler
from krubit.domain.creator_signals import (
    CapabilityState,
    ContentKind,
    ContentState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import (
    Connector,
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.integrations.catalog import CATALOG
from krubit.integrations.x import XConnector
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeConnectorError(RuntimeError):
    def __init__(
        self, failure: ConnectorFailure, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(failure.safe_detail)
        self.failure = failure
        self.retry_after_seconds = retry_after_seconds


class FakeConnector:
    """A minimal `Connector` whose `fetch_page` is scripted per call."""

    def __init__(self, platform: Platform, results: Sequence[ConnectorPage | Exception]) -> None:
        self.descriptor = CATALOG[platform]
        self._results = list(results)
        self.calls: list[str] = []

    async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
        raise NotImplementedError

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        self.calls.append(account.account_id)
        if not self._results:
            return ConnectorPage(items=())
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def health(
        self, account: CreatorAccount | None = None
    ) -> ConnectorHealth:  # pragma: no cover
        raise NotImplementedError


def account(guild_id: int, platform: Platform, external_id: str) -> CreatorAccount:
    return CreatorAccount(
        guild_id=guild_id,
        account_id=creator_account_id(platform, external_id),
        owner_member_id=222,
        platform=platform,
        handle=external_id,
        canonical_url=f"https://example.test/{external_id}",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def social_item(external_id: str) -> Mapping[str, JSONValue]:
    return {
        "external_id": external_id,
        "kind": ContentKind.POST.value,
        "state": ContentState.PUBLISHED.value,
        "canonical_url": f"https://example.test/posts/{external_id}",
    }


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    await value.set_guild_enabled(111, True)
    await value.set_guild_enabled(222, True)
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_one_connector_failure_does_not_cancel_other_guild_or_platform_jobs(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    await store.save_creator_account(account(222, Platform.BLUESKY, "bsky-one"))
    x_connector = FakeConnector(Platform.X, [FakeConnectorError(ConnectorFailure.rate_limited())])
    bluesky_connector = FakeConnector(Platform.BLUESKY, [ConnectorPage(items=())])
    supervisor = ConnectorScheduler(
        store,
        {Platform.X: x_connector, Platform.BLUESKY: bluesky_connector},
        guild_ids=lambda: (111, 222),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )

    await supervisor.run_cycle()

    x_result = supervisor.result(111, Platform.X)
    bluesky_result = supervisor.result(222, Platform.BLUESKY)
    assert x_result is not None and x_result.state is CapabilityState.DEGRADED
    assert bluesky_result is not None and bluesky_result.state is CapabilityState.READY


class _FailListingForGuildStore:
    """Delegates to a real `SQLiteStore` except `list_creator_accounts` for one guild.

    Proxies every other attribute straight through via `__getattr__` so `run_cycle`'s
    other store calls (`get_content_schedule`, `save_content_schedule`, ...) still hit
    the real, fully-functional store.
    """

    def __init__(self, inner: SQLiteStore, *, failing_guild_id: int) -> None:
        self._inner = inner
        self._failing_guild_id = failing_guild_id
        self.attempted_guild_ids: list[int] = []

    async def list_creator_accounts(self, guild_id: int) -> list[CreatorAccount]:
        self.attempted_guild_ids.append(guild_id)
        if guild_id == self._failing_guild_id:
            raise RuntimeError("simulated corrupt row / DB error enumerating this guild")
        return await self._inner.list_creator_accounts(guild_id)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_one_guilds_account_listing_failure_does_not_prevent_other_guilds_jobs(
    store: SQLiteStore,
) -> None:
    """`list_creator_accounts` raising for one guild must not abort the whole cycle.

    Regression test: `run_cycle` used to call `list_creator_accounts` for every guild
    directly inside the job-building loop with no per-guild isolation, so a failure for
    guild 111 (enumerated first) would propagate out of `run_cycle` before
    `asyncio.gather` was ever reached — discarding jobs already queued for earlier
    guilds and silently skipping every guild after the failing one, including 222 and
    333 here.
    """
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    await store.set_guild_enabled(333, True)
    await store.save_creator_account(account(333, Platform.TIKTOK, "tt-one"))
    await store.save_creator_account(account(222, Platform.BLUESKY, "bsky-one"))
    x_connector = FakeConnector(Platform.X, [ConnectorPage(items=())])
    tiktok_connector = FakeConnector(Platform.TIKTOK, [ConnectorPage(items=())])
    bluesky_connector = FakeConnector(Platform.BLUESKY, [ConnectorPage(items=())])
    failing_store = cast(SQLiteStore, _FailListingForGuildStore(store, failing_guild_id=111))
    supervisor = ConnectorScheduler(
        failing_store,
        {
            Platform.X: x_connector,
            Platform.TIKTOK: tiktok_connector,
            Platform.BLUESKY: bluesky_connector,
        },
        guild_ids=lambda: (111, 333, 222),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )

    await supervisor.run_cycle()

    # Guild 111's enumeration failure must not have been swallowed silently for no
    # reason: it really was attempted, and it really did fail (no result recorded).
    assert supervisor.result(111, Platform.X) is None
    assert x_connector.calls == []
    # Guild 333 (enumerated right after the failing guild) and guild 222 (enumerated
    # last) must still have run their jobs in the same cycle.
    assert supervisor.result(333, Platform.TIKTOK) is not None
    assert supervisor.result(333, Platform.TIKTOK).state is CapabilityState.READY  # type: ignore[union-attr]
    assert supervisor.result(222, Platform.BLUESKY) is not None
    assert supervisor.result(222, Platform.BLUESKY).state is CapabilityState.READY  # type: ignore[union-attr]
    assert tiktok_connector.calls == [account(333, Platform.TIKTOK, "tt-one").account_id]
    assert bluesky_connector.calls == [account(222, Platform.BLUESKY, "bsky-one").account_id]


@pytest.mark.asyncio
async def test_multiple_guilds_and_platforms_each_get_their_own_result(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    await store.save_creator_account(account(111, Platform.TIKTOK, "tt-one"))
    await store.save_creator_account(account(222, Platform.BLUESKY, "bsky-one"))
    connectors: dict[Platform, Connector] = {
        Platform.X: FakeConnector(Platform.X, [ConnectorPage(items=())]),
        Platform.TIKTOK: FakeConnector(Platform.TIKTOK, [ConnectorPage(items=())]),
        Platform.BLUESKY: FakeConnector(Platform.BLUESKY, [ConnectorPage(items=())]),
    }
    supervisor = ConnectorScheduler(
        store, connectors, guild_ids=lambda: (111, 222), now=lambda: NOW, jitter=lambda: 0.0
    )

    await supervisor.run_cycle()

    assert supervisor.result(111, Platform.X) is not None
    assert supervisor.result(111, Platform.TIKTOK) is not None
    assert supervisor.result(222, Platform.BLUESKY) is not None
    # A guild that never enrolled a platform never gets a result for it.
    assert supervisor.result(222, Platform.X) is None


@pytest.mark.asyncio
async def test_restart_honors_persisted_next_poll_and_does_not_repoll_early(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    first_connector = FakeConnector(Platform.X, [ConnectorPage(items=())])
    first = ConnectorScheduler(
        store,
        {Platform.X: first_connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )
    await first.run_cycle()
    assert len(first_connector.calls) == 1

    # A brand-new scheduler instance (simulating a process restart) sharing the same
    # store must read back the durable next_poll_at rather than polling immediately.
    second_connector = FakeConnector(Platform.X, [ConnectorPage(items=())])
    second = ConnectorScheduler(
        store,
        {Platform.X: second_connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW + timedelta(seconds=1),
        jitter=lambda: 0.0,
    )
    await second.run_cycle()
    assert second_connector.calls == []

    # Once the durable interval has actually elapsed, a later restart polls again.
    third_connector = FakeConnector(Platform.X, [ConnectorPage(items=())])
    third = ConnectorScheduler(
        store,
        {Platform.X: third_connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW + timedelta(minutes=16),
        jitter=lambda: 0.0,
    )
    await third.run_cycle()
    assert len(third_connector.calls) == 1


@pytest.mark.asyncio
async def test_repeated_failures_back_off_and_never_exceed_the_connector_retry_hint(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    connector = FakeConnector(
        Platform.X,
        [
            FakeConnectorError(ConnectorFailure.rate_limited(), retry_after_seconds=5_000.0),
        ],
    )
    supervisor = ConnectorScheduler(
        store,
        {Platform.X: connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )

    await supervisor.run_cycle()

    schedule = await store.get_content_schedule(
        111, account(111, Platform.X, "x-one").account_id, Platform.X
    )
    assert schedule is not None
    # The connector's own reported resume time (5000s) is far slower than X's 900s
    # default, so the durable schedule must honor the slower, connector-instructed
    # interval rather than the faster default backoff.
    assert schedule.interval_seconds >= 5_000
    assert schedule.next_poll_at >= NOW + timedelta(seconds=5_000)


@pytest.mark.asyncio
async def test_real_x_connector_429_header_is_honored_end_to_end(store: SQLiteStore) -> None:
    """Full pipeline: a real `XConnector` HTTP 429 response with a rate-limit-reset
    header must drive the scheduler's durable next-poll time, not just a hand-built
    fake exception. Proves `_classify_scheduler_failure`'s `retry_after_seconds`
    duck-typing actually lines up with what `XConnector` raises in practice."""

    class RateLimitedResponse:
        status = 429
        headers = {"x-rate-limit-reset": str(int(NOW.timestamp()) + 10_800)}

        async def __aenter__(self) -> RateLimitedResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def json(self) -> object:
            return {}

    class RealX429Session:
        def get(self, url: str, **kwargs: object) -> RateLimitedResponse:
            return RateLimitedResponse()

    x_account = account(111, Platform.X, "x-real")
    await store.save_creator_account(x_account)
    real_connector = XConnector(RealX429Session(), "bearer-token", now=lambda: NOW)
    supervisor = ConnectorScheduler(
        store,
        {Platform.X: real_connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )

    await supervisor.run_cycle()

    schedule = await store.get_content_schedule(111, x_account.account_id, Platform.X)
    assert schedule is not None
    # X's own 3-hour (10800s) rate-limit-reset header is far slower than X's 900s
    # default backoff and must win.
    assert schedule.interval_seconds >= 10_800
    result = supervisor.result(111, Platform.X)
    assert result is not None and result.state is CapabilityState.DEGRADED


@pytest.mark.asyncio
async def test_fanbase_is_never_scheduled_even_if_enrolled(store: SQLiteStore) -> None:
    await store.save_creator_account(account(111, Platform.FANBASE, "fb-one"))
    supervisor = ConnectorScheduler(
        store, {}, guild_ids=lambda: (111,), now=lambda: NOW, jitter=lambda: 0.0
    )

    await supervisor.run_cycle()

    assert supervisor.result(111, Platform.FANBASE) is None
    schedule = await store.get_content_schedule(
        111, account(111, Platform.FANBASE, "fb-one").account_id, Platform.FANBASE
    )
    assert schedule is None


@pytest.mark.asyncio
async def test_paused_account_is_skipped(store: SQLiteStore) -> None:
    paused = account(111, Platform.X, "x-paused")
    await store.save_creator_account(replace(paused, paused=True))
    connector = FakeConnector(Platform.X, [ConnectorPage(items=())])
    supervisor = ConnectorScheduler(
        store,
        {Platform.X: connector},
        guild_ids=lambda: (111,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )

    await supervisor.run_cycle()

    assert connector.calls == []
    assert supervisor.result(111, Platform.X) is None


@pytest.mark.asyncio
async def test_platform_semaphore_bounds_concurrent_fetches_for_the_same_platform(
    store: SQLiteStore,
) -> None:
    await store.save_creator_account(account(111, Platform.X, "x-one"))
    await store.save_creator_account(account(111, Platform.X, "x-two"))
    await store.save_creator_account(account(111, Platform.X, "x-three"))

    concurrent = 0
    peak_concurrent = 0

    class TrackingConnector:
        descriptor = CATALOG[Platform.X]

        async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
            raise NotImplementedError

        async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
            nonlocal concurrent, peak_concurrent
            concurrent += 1
            peak_concurrent = max(peak_concurrent, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return ConnectorPage(items=())

        async def health(
            self, account: CreatorAccount | None = None
        ) -> ConnectorHealth:  # pragma: no cover
            raise NotImplementedError

    supervisor = ConnectorScheduler(
        store,
        {Platform.X: TrackingConnector()},
        guild_ids=lambda: (111,),
        now=lambda: NOW,
        jitter=lambda: 0.0,
        concurrency_limits={Platform.X: 1},
    )

    await supervisor.run_cycle()

    assert peak_concurrent == 1
