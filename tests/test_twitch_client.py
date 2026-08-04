from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import aiohttp
import pytest

from krubit.domain.live_signals import TwitchLookupKind
from krubit.integrations.twitch import (
    STREAMS_URL,
    TOKEN_URL,
    VALIDATE_URL,
    TwitchHelixClient,
)


class FakeResponse:
    def __init__(
        self, status: int, payload: object, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        return self.payload


class InvalidJsonResponse(FakeResponse):
    async def json(self) -> object:
        raise ValueError("response body is not JSON")


class ContentTypeJsonResponse(FakeResponse):
    async def json(self) -> object:
        raise aiohttp.ContentTypeError(
            cast(aiohttp.RequestInfo, None), (), message="unexpected content type"
        )


class BlockingResponse(FakeResponse):
    def __init__(self, status: int, payload: object) -> None:
        super().__init__(status, payload)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self) -> BlockingResponse:
        self.entered.set()
        await self.release.wait()
        return self


class FakeSession:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def token_response(expires_in: object = 3_600, token: str = "app-token") -> FakeResponse:
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in})


def offline_response() -> FakeResponse:
    return FakeResponse(200, {"data": []})


def client(
    session: FakeSession,
    clock: FrozenClock,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> TwitchHelixClient:
    return TwitchHelixClient(
        session,
        "client-id",
        "super-secret",
        now=clock,
        sleep=sleep or asyncio.sleep,
    )


@pytest.mark.asyncio
async def test_get_stream_acquires_an_app_token_before_the_helix_request() -> None:
    session = FakeSession([token_response(), offline_response()])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.OFFLINE
    assert session.requests == [
        (
            "POST",
            TOKEN_URL,
            {
                "data": {
                    "client_id": "client-id",
                    "client_secret": "super-secret",
                    "grant_type": "client_credentials",
                }
            },
        ),
        (
            "GET",
            STREAMS_URL,
            {
                "params": {"user_login": "krucialstudios"},
                "headers": {
                    "Client-Id": "client-id",
                    "Authorization": "Bearer app-token",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_get_stream_reuses_a_cached_token_until_its_refresh_window() -> None:
    session = FakeSession([token_response(), offline_response(), offline_response()])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)

    await twitch.get_stream("first")
    await twitch.get_stream("second")

    assert [request[1] for request in session.requests] == [TOKEN_URL, STREAMS_URL, STREAMS_URL]


@pytest.mark.asyncio
async def test_get_stream_refreshes_the_token_sixty_seconds_before_expiry() -> None:
    session = FakeSession(
        [token_response(120), offline_response(), token_response(), offline_response()]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)

    await twitch.get_stream("first")
    clock.value += timedelta(seconds=60)
    await twitch.get_stream("second")

    assert [request[1] for request in session.requests] == [
        TOKEN_URL,
        STREAMS_URL,
        TOKEN_URL,
        STREAMS_URL,
    ]


@pytest.mark.asyncio
async def test_get_stream_validates_a_cached_token_once_an_hour() -> None:
    session = FakeSession(
        [
            token_response(7_200),
            offline_response(),
            FakeResponse(
                200,
                {
                    "client_id": "client-id",
                    "login": "krubit-app",
                    "scopes": [],
                    "user_id": "",
                    "expires_in": 3_600,
                },
            ),
            offline_response(),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)

    await twitch.get_stream("first")
    clock.value += timedelta(hours=1)
    await twitch.get_stream("second")

    assert [request[1] for request in session.requests] == [
        TOKEN_URL,
        STREAMS_URL,
        VALIDATE_URL,
        STREAMS_URL,
    ]
    assert session.requests[2][2] == {"headers": {"Authorization": "OAuth app-token"}}


@pytest.mark.asyncio
async def test_get_stream_reacquires_the_token_and_retries_once_after_a_401() -> None:
    session = FakeSession(
        [
            token_response(),
            FakeResponse(401, {"message": "expired"}),
            token_response(),
            offline_response(),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.OFFLINE
    assert [request[1] for request in session.requests] == [
        TOKEN_URL,
        STREAMS_URL,
        TOKEN_URL,
        STREAMS_URL,
    ]


@pytest.mark.asyncio
async def test_get_stream_maps_a_live_helix_payload() -> None:
    session = FakeSession(
        [
            token_response(),
            FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "stream-1",
                            "user_login": "krucialstudios",
                            "user_name": "Krucial Studios",
                            "title": "Building Krucial Town",
                            "game_name": "Just Chatting",
                            "started_at": "2026-08-04T20:12:00Z",
                            "thumbnail_url": "https://static-cdn.jtvnw.net/preview-{width}x{height}.jpg",
                        }
                    ]
                },
            ),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.LIVE
    assert result.stream is not None
    assert result.stream.stream_id == "stream-1"
    assert result.stream.started_at == datetime(2026, 8, 4, 20, 12, tzinfo=UTC)
    assert result.stream.thumbnail_url.endswith("preview-640x360.jpg")


@pytest.mark.asyncio
async def test_get_stream_maps_an_empty_data_array_as_offline() -> None:
    session = FakeSession([token_response(), offline_response()])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result == result.__class__(TwitchLookupKind.OFFLINE)


@pytest.mark.asyncio
async def test_get_stream_retries_after_a_rate_limit_reset_with_an_injected_sleeper() -> None:
    session = FakeSession(
        [
            token_response(),
            FakeResponse(429, {}, {"Ratelimit-Reset": "1785873610"}),
            offline_response(),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await client(session, clock, record_sleep).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.OFFLINE
    assert delays == [10.0]
    assert [request[1] for request in session.requests] == [TOKEN_URL, STREAMS_URL, STREAMS_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["9999999999", "-1"])
async def test_get_stream_does_not_sleep_for_an_unsafe_or_past_rate_limit_reset(reset: str) -> None:
    session = FakeSession(
        [
            token_response(),
            FakeResponse(429, {}, {"Ratelimit-Reset": reset}),
            offline_response(),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await client(session, clock, record_sleep).get_stream("krucialstudios")

    assert delays == []
    if reset == "9999999999":
        assert result.kind is TwitchLookupKind.UNAVAILABLE
        assert result.unavailable_reason == "rate_limited"
        assert [request[1] for request in session.requests] == [TOKEN_URL, STREAMS_URL]
    else:
        assert result.kind is TwitchLookupKind.OFFLINE
        assert [request[1] for request in session.requests] == [TOKEN_URL, STREAMS_URL, STREAMS_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_in",
    [float("nan"), float("inf"), float("-inf"), 120.5, "120", None, {}, 10**1000],
)
async def test_get_stream_rejects_non_integer_or_unbounded_token_lifetimes(
    expires_in: object,
) -> None:
    session = FakeSession([token_response(expires_in)])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"client_id": "another-client"},
        {"client_id": "client-id", "login": "krubit-app", "scopes": [], "user_id": ""},
    ],
)
async def test_get_stream_rejects_malformed_hourly_token_validation_payloads(
    payload: object,
) -> None:
    session = FakeSession([token_response(7_200), offline_response(), FakeResponse(200, payload)])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)

    await twitch.get_stream("first")
    clock.value += timedelta(hours=1)
    result = await twitch.get_stream("second")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == "invalid_response"


@pytest.mark.asyncio
async def test_get_stream_classifies_aiohttp_content_type_failures_as_invalid_responses() -> None:
    session = FakeSession([token_response(), ContentTypeJsonResponse(200, None)])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == "invalid_response"


@pytest.mark.asyncio
async def test_concurrent_missing_token_lookups_share_a_single_token_grant() -> None:
    blocked_token = BlockingResponse(200, {"access_token": "app-token", "expires_in": 3_600})
    session = FakeSession([blocked_token, offline_response(), offline_response()])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)

    first = asyncio.create_task(twitch.get_stream("first"))
    await blocked_token.entered.wait()
    second = asyncio.create_task(twitch.get_stream("second"))
    await asyncio.sleep(0)
    blocked_token.release.set()
    results = await asyncio.gather(first, second)

    assert [result.kind for result in results] == [
        TwitchLookupKind.OFFLINE,
        TwitchLookupKind.OFFLINE,
    ]
    assert [request[1] for request in session.requests].count(TOKEN_URL) == 1


@pytest.mark.asyncio
async def test_stale_401_does_not_clear_a_newer_token_from_another_lookup() -> None:
    delayed_401 = BlockingResponse(401, {})
    session = FakeSession(
        [
            token_response(7_200, "old-token"),
            offline_response(),
            delayed_401,
            token_response(7_200, "new-token"),
            offline_response(),
            offline_response(),
        ]
    )
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    twitch = client(session, clock)
    await twitch.get_stream("initial")

    stale_lookup = asyncio.create_task(twitch.get_stream("stale"))
    await delayed_401.entered.wait()
    clock.value += timedelta(hours=2)
    fresh_lookup = await twitch.get_stream("fresh")
    delayed_401.release.set()
    stale_result = await stale_lookup

    assert fresh_lookup.kind is TwitchLookupKind.OFFLINE
    assert stale_result.kind is TwitchLookupKind.OFFLINE
    assert [request[1] for request in session.requests].count(TOKEN_URL) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_response", "reason"),
    [
        (TimeoutError(), "timeout"),
        (InvalidJsonResponse(200, "not-json"), "invalid_response"),
    ],
)
async def test_get_stream_maps_transport_and_json_failures_to_unavailable(
    stream_response: FakeResponse | BaseException, reason: str
) -> None:
    session = FakeSession([token_response(), stream_response])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == reason


@pytest.mark.asyncio
async def test_get_stream_never_exposes_credentials_in_an_unavailable_reason() -> None:
    session = FakeSession([FakeResponse(401, {"message": "super-secret app-token"})])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == "token_rejected"
    assert "super-secret" not in result.unavailable_reason
    assert "app-token" not in result.unavailable_reason


@pytest.mark.asyncio
async def test_get_stream_contains_an_unexpected_transport_exception_without_its_details() -> None:
    session = FakeSession([token_response(), RuntimeError("super-secret app-token")])
    clock = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    result = await client(session, clock).get_stream("krucialstudios")

    assert result.kind is TwitchLookupKind.UNAVAILABLE
    assert result.unavailable_reason == "unavailable"
