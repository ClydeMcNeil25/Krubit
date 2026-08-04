"""State-machine tests for durable live-stream signal reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.live_signals import (
    LiveSignalAction,
    LiveSignalConfig,
    LiveSignalStatus,
    StreamingObservation,
    TwitchLookup,
    TwitchLookupKind,
    TwitchStream,
)
from krubit.services.live_signals import LiveSignalService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)


class FakeTwitch:
    def __init__(self, results: Sequence[TwitchLookup | BaseException]) -> None:
        self._results = list(results)
        self.logins: list[str] = []

    @classmethod
    def live(cls, stream_id: str = "stream-1") -> FakeTwitch:
        return cls([TwitchLookup(TwitchLookupKind.LIVE, stream(stream_id))])

    async def get_stream(self, login: str) -> TwitchLookup:
        self.logins.append(login)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class HangingTwitch:
    async def get_stream(self, login: str) -> TwitchLookup:
        await asyncio.Future[TwitchLookup]()
        raise AssertionError("unreachable")


class BlockingUnavailableTwitch:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def get_stream(self, login: str) -> TwitchLookup:
        self.calls += 1
        if self.calls == 1:
            return TwitchLookup(TwitchLookupKind.LIVE, stream())
        if self.calls == 2:
            self.started.set()
            await self.release.wait()
        return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    await value.set_live_signal_config(LiveSignalConfig(111, 444, 333, NOW))
    try:
        yield value
    finally:
        await value.close()


def observation(guild_id: int = 111) -> StreamingObservation:
    return StreamingObservation(
        guild_id=guild_id,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://twitch.tv/krucialstudios",
        activity_started_at=NOW - timedelta(minutes=2),
        observed_at=NOW,
    )


def stream(stream_id: str = "stream-1") -> TwitchStream:
    return TwitchStream(
        stream_id=stream_id,
        user_login="krucialstudios",
        user_name="Krucial Studios",
        title="Building Krucial Town",
        game_name="Just Chatting",
        started_at=NOW - timedelta(minutes=2),
        thumbnail_url="https://example.test/preview.jpg",
    )


@pytest.mark.asyncio
async def test_replayed_presence_cannot_duplicate_announcement(store: SQLiteStore) -> None:
    service = LiveSignalService(store, FakeTwitch.live())

    first = await service.observe(observation(), now=NOW)
    second = await service.observe(observation(), now=NOW + timedelta(seconds=10))

    assert first.actions == (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE)
    assert second.actions == ()
    assert first.stream == stream()
    saved = await store.get_live_session(111, first.session_key)
    assert saved is not None and saved.stream == stream()


@pytest.mark.asyncio
async def test_initial_lookup_timeout_posts_a_degraded_announcement_after_five_seconds(
    store: SQLiteStore,
) -> None:
    service = LiveSignalService(store, HangingTwitch())

    plan = await service.observe(observation(), now=NOW)

    assert plan.actions == (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE)
    assert plan.stream is None
    saved = await store.get_live_session(111, plan.session_key)
    assert saved is not None and saved.status is LiveSignalStatus.DETECTED
    assert saved.last_twitch_at is None


@pytest.mark.asyncio
async def test_live_recovery_edits_existing_announcement_without_a_second_ping(
    store: SQLiteStore,
) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch([TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout"),
                    TwitchLookup(TwitchLookupKind.LIVE, stream())]),
    )
    initial = await service.observe(observation(), now=NOW)
    assert initial.delivery_attempt == 1
    await service.record_delivery_result(
        111,
        initial.session_key,
        status="succeeded",
        channel_id=444,
        message_id=1001,
        attempt=initial.delivery_attempt,
    )

    recovered = await service.observe(observation(), now=NOW + timedelta(minutes=1))

    assert recovered.actions == (LiveSignalAction.EDIT_ANNOUNCEMENT,)
    assert recovered.stream == stream()


@pytest.mark.asyncio
async def test_discord_disappearance_does_not_remove_krubit_role_while_twitch_is_live(
    store: SQLiteStore,
) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
            ]
        ),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )

    ended = await service.presence_ended(111, 222, now=NOW + timedelta(seconds=5))
    plans = await service.reconcile(111, now=NOW + timedelta(seconds=6))

    assert ended is not None and ended.actions == ()
    assert plans == ()
    saved = await store.get_live_session(111, initial.session_key)
    assert saved is not None and saved.status is LiveSignalStatus.LIVE
    assert saved.role_assigned_by_krubit is True


@pytest.mark.asyncio
async def test_twitch_offline_ends_session_and_removes_only_owned_role(store: SQLiteStore) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
                TwitchLookup(TwitchLookupKind.OFFLINE),
            ]
        ),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )

    plans = await service.reconcile(111, now=NOW + timedelta(minutes=1))

    assert len(plans) == 1
    assert plans[0].actions == (LiveSignalAction.REMOVE_ROLE,)
    saved = await store.get_live_session(111, initial.session_key)
    assert saved is not None and saved.status is LiveSignalStatus.ENDED


@pytest.mark.asyncio
async def test_unavailable_sources_preserve_role_for_exactly_five_minute_grace(
    store: SQLiteStore,
) -> None:
    unavailable = TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
    service = LiveSignalService(
        store,
        FakeTwitch([TwitchLookup(TwitchLookupKind.LIVE, stream())] + [unavailable] * 3),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )
    await service.presence_ended(111, 222, now=NOW + timedelta(minutes=1))

    before = await service.reconcile(111, now=NOW + timedelta(minutes=5, seconds=59))
    at_boundary = await service.reconcile(111, now=NOW + timedelta(minutes=6))

    assert before == ()
    assert len(at_boundary) == 1
    assert at_boundary[0].actions == (LiveSignalAction.REMOVE_ROLE,)


@pytest.mark.asyncio
async def test_restart_and_provisional_to_stream_recovery_cannot_reannounce(
    store: SQLiteStore,
) -> None:
    unavailable = TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
    initial_service = LiveSignalService(store, FakeTwitch([unavailable]))
    initial = await initial_service.observe(observation(), now=NOW)

    restarted = LiveSignalService(store, FakeTwitch.live())
    recovered = await restarted.observe(observation(), now=NOW + timedelta(minutes=1))

    assert initial.actions == (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE)
    assert recovered.actions == ()
    saved = await store.get_live_session(111, initial.session_key)
    assert saved is not None and saved.stream == stream()


@pytest.mark.asyncio
async def test_failed_provisional_delivery_retries_once_under_the_stream_identity(
    store: SQLiteStore,
) -> None:
    unavailable = TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
    service = LiveSignalService(
        store,
        FakeTwitch([unavailable, TwitchLookup(TwitchLookupKind.LIVE, stream())]),
    )
    initial = await service.observe(observation(), now=NOW)
    assert initial.delivery_attempt == 1
    await service.record_delivery_result(
        111,
        initial.session_key,
        status="failed",
        channel_id=444,
        message_id=None,
        attempt=initial.delivery_attempt,
    )

    retried = await service.observe(observation(), now=NOW + timedelta(minutes=1))

    assert retried.actions == (LiveSignalAction.ANNOUNCE,)
    delivery = await store.get_live_delivery(111, "stream:stream-1")
    assert delivery is not None and delivery.status == "claimed"


@pytest.mark.asyncio
async def test_preexisting_role_is_never_removed(store: SQLiteStore) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
                TwitchLookup(TwitchLookupKind.OFFLINE),
            ]
        ),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=False, status="succeeded"
    )

    plans = await service.reconcile(111, now=NOW + timedelta(minutes=1))

    assert plans == ()


@pytest.mark.asyncio
async def test_results_require_existing_sessions_and_supported_outcomes(store: SQLiteStore) -> None:
    service = LiveSignalService(store, FakeTwitch.live())

    with pytest.raises(ValueError, match="session does not exist"):
        await service.record_role_result(
            111, "missing", role_id=333, assigned_by_krubit=True, status="succeeded"
        )
    with pytest.raises(ValueError, match="status must be succeeded or failed"):
        await service.record_delivery_result(
            111, "missing", status="unknown", channel_id=444, message_id=None, attempt=1
        )


@pytest.mark.asyncio
async def test_delivery_result_requires_a_durable_claim(store: SQLiteStore) -> None:
    service = LiveSignalService(store, FakeTwitch([TwitchLookup(TwitchLookupKind.OFFLINE)]))
    plan = await service.observe(observation(), now=NOW)

    with pytest.raises(ValueError, match="delivery claim does not exist"):
        await service.record_delivery_result(
            111,
            plan.session_key,
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=1,
        )


@pytest.mark.asyncio
async def test_status_and_integration_health_are_guild_scoped(store: SQLiteStore) -> None:
    unavailable = TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
    service = LiveSignalService(store, FakeTwitch([unavailable]))
    plan = await service.observe(observation(), now=NOW)

    assert [item.session_key for item in await service.status(111)] == [plan.session_key]
    assert await service.status(222) == ()
    assert await service.integration_health(111) == "limited"
    assert await service.integration_health(222) == "healthy"


@pytest.mark.asyncio
async def test_observe_offline_ends_an_owned_degraded_session_and_returns_its_role(
    store: SQLiteStore,
) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout"),
                TwitchLookup(TwitchLookupKind.OFFLINE),
            ]
        ),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )

    offline = await service.observe(observation(), now=NOW + timedelta(minutes=1))

    assert offline.actions == (LiveSignalAction.REMOVE_ROLE,)
    assert offline.role_id == 333
    saved = await store.get_live_session(111, initial.session_key)
    assert saved is not None and saved.status is LiveSignalStatus.ENDED


@pytest.mark.asyncio
async def test_reconciliation_cannot_overwrite_a_concurrent_presence_end(
    store: SQLiteStore,
) -> None:
    twitch = BlockingUnavailableTwitch()
    service = LiveSignalService(store, twitch)
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )

    reconciliation = asyncio.create_task(service.reconcile(111, now=NOW + timedelta(minutes=1)))
    await twitch.started.wait()
    ending = asyncio.create_task(service.presence_ended(111, 222, now=NOW + timedelta(minutes=2)))
    await asyncio.sleep(0)
    twitch.release.set()
    await asyncio.gather(reconciliation, ending)

    saved = await store.get_live_session(111, initial.session_key)
    assert saved is not None and saved.presence_active is False
    assert saved.missing_since == NOW + timedelta(minutes=2)
    expiry = await service.reconcile(111, now=NOW + timedelta(minutes=7))
    assert expiry[0].actions == (LiveSignalAction.REMOVE_ROLE,)


@pytest.mark.asyncio
async def test_results_require_the_configured_role_and_channel(store: SQLiteStore) -> None:
    service = LiveSignalService(store, FakeTwitch([TwitchLookup(TwitchLookupKind.UNAVAILABLE)]))
    plan = await service.observe(observation(), now=NOW)

    with pytest.raises(ValueError, match="configured streaming role"):
        await service.record_role_result(
            111, plan.session_key, role_id=999, assigned_by_krubit=True, status="succeeded"
        )
    with pytest.raises(ValueError, match="configured notification channel"):
        await service.record_delivery_result(
            111,
            plan.session_key,
            status="failed",
            channel_id=999,
            message_id=None,
            attempt=1,
        )


@pytest.mark.asyncio
async def test_results_reject_a_guild_without_live_signal_configuration(tmp_path: Path) -> None:
    unconfigured = await SQLiteStore.open(tmp_path / "unconfigured.db")
    await unconfigured.initialize()
    try:
        service = LiveSignalService(
            unconfigured,
            FakeTwitch([TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")]),
        )
        plan = await service.observe(observation(), now=NOW)

        with pytest.raises(ValueError, match="configured streaming role"):
            await service.record_role_result(
                111, plan.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
            )
    finally:
        await unconfigured.close()


@pytest.mark.asyncio
async def test_removal_plan_keeps_the_original_role_after_configuration_changes(
    store: SQLiteStore,
) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
                TwitchLookup(TwitchLookupKind.OFFLINE),
            ]
        ),
    )
    initial = await service.observe(observation(), now=NOW)
    await service.record_role_result(
        111, initial.session_key, role_id=333, assigned_by_krubit=True, status="succeeded"
    )
    await store.set_live_signal_config(LiveSignalConfig(111, 777, 666, NOW + timedelta(minutes=1)))

    plans = await service.reconcile(111, now=NOW + timedelta(minutes=2))

    assert plans[0].actions == (LiveSignalAction.REMOVE_ROLE,)
    assert plans[0].role_id == 333


@pytest.mark.asyncio
async def test_stale_delivery_completion_cannot_overwrite_a_reclaimed_attempt(
    store: SQLiteStore,
) -> None:
    unavailable = TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
    service = LiveSignalService(
        store,
        FakeTwitch([unavailable, TwitchLookup(TwitchLookupKind.LIVE, stream())]),
    )
    initial = await service.observe(observation(), now=NOW)
    assert initial.delivery_attempt == 1
    await service.record_delivery_result(
        111,
        initial.session_key,
        status="failed",
        channel_id=444,
        message_id=None,
        attempt=initial.delivery_attempt,
    )
    retried = await service.observe(observation(), now=NOW + timedelta(minutes=1))
    assert retried.delivery_attempt == 2

    with pytest.raises(ValueError, match="stale delivery attempt"):
        await service.record_delivery_result(
            111,
            initial.session_key,
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=initial.delivery_attempt,
        )
    delivery = await store.get_live_delivery(111, "stream:stream-1")
    assert delivery is not None and delivery.status == "claimed" and delivery.attempt == 2


@pytest.mark.asyncio
async def test_integration_health_uses_the_latest_twitch_check(store: SQLiteStore) -> None:
    service = LiveSignalService(
        store,
        FakeTwitch(
            [
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
                TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout"),
                TwitchLookup(TwitchLookupKind.LIVE, stream()),
            ]
        ),
    )
    await service.observe(observation(), now=NOW)
    await service.reconcile(111, now=NOW + timedelta(minutes=1))
    assert await service.integration_health(111) == "limited"

    await service.reconcile(111, now=NOW + timedelta(minutes=2))

    assert await service.integration_health(111) == "healthy"
