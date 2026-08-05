from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    ContentKind,
    ContentState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
    creator_account_id,
)
from krubit.integrations.base import ConnectorFailureKind
from krubit.integrations.x import (
    USER_TWEETS_URL,
    USERS_BY_USERNAME_URL,
    XConnector,
    XConnectorError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

X_TIMELINE_FIXTURE = {
    "data": [
        {"id": "103", "text": "Original post", "created_at": "2026-08-05T10:00:00.000Z"},
        {
            "id": "102",
            "text": "RT @someone: reposted content",
            "created_at": "2026-08-05T09:00:00.000Z",
            "referenced_tweets": [{"type": "retweeted", "id": "50"}],
        },
        {
            "id": "101",
            "text": "@someone thanks for the reply",
            "created_at": "2026-08-05T08:00:00.000Z",
            "referenced_tweets": [{"type": "replied_to", "id": "40"}],
        },
    ],
    "meta": {"newest_id": "103", "oldest_id": "101", "result_count": 3},
}

X_TIMELINE_WITH_QUOTE_TWEET_FIXTURE = {
    "data": [
        {"id": "203", "text": "Original post", "created_at": "2026-08-05T10:00:00.000Z"},
        {
            "id": "202",
            "text": "Quoting with commentary",
            "created_at": "2026-08-05T09:00:00.000Z",
            "referenced_tweets": [{"type": "quoted", "id": "60"}],
        },
    ],
    "meta": {"newest_id": "203", "oldest_id": "202", "result_count": 2},
}


class FakeResponse:
    def __init__(
        self, status: int, payload: object, *, headers: dict[str, str] | None = None
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
        raise ValueError("not json")


class FakeSession:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def x_connector(payload: object, *, status: int = 200) -> XConnector:
    session = FakeSession([FakeResponse(status, payload)])
    return XConnector(session, "bearer-token", now=lambda: NOW)


def x_connector_from_session(session: FakeSession) -> XConnector:
    return XConnector(session, "bearer-token", now=lambda: NOW)


def x_account(external_id: str = "u1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.X, external_id),
        owner_member_id=222,
        platform=Platform.X,
        handle="krucialstudios",
        canonical_url="https://x.com/krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_x_connector_uses_since_id_and_ignores_replies_and_reposts() -> None:
    page = await x_connector(X_TIMELINE_FIXTURE).fetch_page(x_account(), cursor="100")
    assert [event.get("external_id") for event in page.items] == ["103"]
    assert page.next_cursor == "103"


@pytest.mark.asyncio
async def test_fetch_page_ignores_quote_tweets() -> None:
    page = await x_connector(X_TIMELINE_WITH_QUOTE_TWEET_FIXTURE).fetch_page(
        x_account(), cursor="200"
    )
    assert [event.get("external_id") for event in page.items] == ["203"]
    assert page.next_cursor == "203"


@pytest.mark.asyncio
async def test_fetch_page_sends_since_id_param_from_cursor() -> None:
    session = FakeSession([FakeResponse(200, X_TIMELINE_FIXTURE)])
    await x_connector_from_session(session).fetch_page(x_account(), cursor="100")

    url, kwargs = session.requests[0]
    assert url == USER_TWEETS_URL.format(id="u1")
    params = cast("dict[str, object]", kwargs["params"])
    assert params["since_id"] == "100"
    assert params["exclude"] == "replies,retweets"


@pytest.mark.asyncio
async def test_fetch_page_omits_since_id_when_no_cursor() -> None:
    session = FakeSession([FakeResponse(200, {"meta": {"result_count": 0}})])
    await x_connector_from_session(session).fetch_page(x_account(), cursor=None)

    _, kwargs = session.requests[0]
    params = cast("dict[str, object]", kwargs["params"])
    assert "since_id" not in params


@pytest.mark.asyncio
async def test_fetch_page_keeps_prior_cursor_when_no_new_tweets() -> None:
    session = FakeSession([FakeResponse(200, {"meta": {"result_count": 0}})])
    page = await x_connector_from_session(session).fetch_page(x_account(), cursor="100")

    assert page.items == ()
    assert page.next_cursor == "100"


@pytest.mark.asyncio
async def test_fetch_page_maps_original_post_fields() -> None:
    session = FakeSession([FakeResponse(200, X_TIMELINE_FIXTURE)])
    page = await x_connector_from_session(session).fetch_page(x_account(), cursor="100")

    item = page.items[0]
    assert item["kind"] == ContentKind.POST.value
    assert item["state"] == ContentState.PUBLISHED.value
    assert item["canonical_url"] == "https://x.com/krucialstudios/status/103"
    assert item["published_at"] == "2026-08-05T10:00:00.000Z"


@pytest.mark.asyncio
async def test_resolve_account_uses_users_by_username() -> None:
    session = FakeSession(
        [FakeResponse(200, {"data": {"id": "u1", "username": "krucialstudios", "name": "KS"}})]
    )

    result = await x_connector_from_session(session).resolve_account(
        RecognizedAccountUrl(
            platform=Platform.X,
            handle="krucialstudios",
            canonical_url="https://x.com/krucialstudios",
        )
    )

    assert result.external_id == "u1"
    assert result.display_name == "KS"
    url, _ = session.requests[0]
    assert url == USERS_BY_USERNAME_URL.format(username="krucialstudios")


@pytest.mark.asyncio
async def test_resolve_account_raises_not_found_for_unknown_username() -> None:
    session = FakeSession([FakeResponse(200, {"errors": [{"title": "Not Found Error"}]})])

    with pytest.raises(XConnectorError) as excinfo:
        await x_connector_from_session(session).resolve_account(
            RecognizedAccountUrl(
                platform=Platform.X, handle="nobody", canonical_url="https://x.com/nobody"
            )
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.NOT_FOUND


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_on_401() -> None:
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector({}, status=401).fetch_page(x_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_fetch_page_raises_rate_limited_on_429() -> None:
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector({}, status=429).fetch_page(x_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED
    assert excinfo.value.retry_after_seconds is None


@pytest.mark.asyncio
async def test_fetch_page_reports_retry_after_from_rate_limit_reset_header() -> None:
    reset_epoch = int(NOW.timestamp()) + 900
    session = FakeSession([FakeResponse(429, {}, headers={"x-rate-limit-reset": str(reset_epoch)})])
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector_from_session(session).fetch_page(x_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED
    assert excinfo.value.retry_after_seconds is not None
    assert 890 <= excinfo.value.retry_after_seconds <= 900


@pytest.mark.asyncio
async def test_fetch_page_falls_back_to_retry_after_header_when_reset_is_absent() -> None:
    session = FakeSession([FakeResponse(429, {}, headers={"Retry-After": "120"})])
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector_from_session(session).fetch_page(x_account(), cursor=None)
    assert excinfo.value.retry_after_seconds == 120.0


@pytest.mark.asyncio
async def test_fetch_page_raises_invalid_response_on_malformed_json() -> None:
    session = FakeSession([InvalidJsonResponse(200, None)])
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector_from_session(session).fetch_page(x_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_page_raises_timeout_on_transport_timeout() -> None:
    session = FakeSession([TimeoutError()])
    with pytest.raises(XConnectorError) as excinfo:
        await x_connector_from_session(session).fetch_page(x_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_health_reports_ready_until_a_failure_is_observed() -> None:
    session = FakeSession([FakeResponse(429, {})])
    client = x_connector_from_session(session)

    healthy = await client.health()
    assert healthy.capability is Capability.SOCIAL
    assert healthy.state is CapabilityState.READY

    with pytest.raises(XConnectorError):
        await client.fetch_page(x_account(), cursor=None)

    degraded = await client.health()
    assert degraded.state is CapabilityState.DEGRADED
