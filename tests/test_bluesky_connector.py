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
from krubit.integrations.bluesky import (
    GET_AUTHOR_FEED_URL,
    RESOLVE_HANDLE_URL,
    BlueskyConnector,
    BlueskyConnectorError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

BLUESKY_AUTHOR_FEED = {
    "feed": [
        {
            "post": {
                "uri": "at://did/post/original",
                "cid": "cid-original",
                "author": {"did": "did:plc:abc", "handle": "creator.bsky.social"},
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": "Hello world",
                    "createdAt": "2026-08-05T10:00:00.000Z",
                },
                "indexedAt": "2026-08-05T10:00:00.000Z",
            }
        },
        {
            "post": {
                "uri": "at://did/post/reply",
                "cid": "cid-reply",
                "author": {"did": "did:plc:abc", "handle": "creator.bsky.social"},
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": "a reply",
                    "createdAt": "2026-08-05T09:30:00.000Z",
                    "reply": {
                        "root": {"uri": "at://did/post/root", "cid": "cid-root"},
                        "parent": {"uri": "at://did/post/parent", "cid": "cid-parent"},
                    },
                },
                "indexedAt": "2026-08-05T09:30:00.000Z",
            }
        },
        {
            "post": {
                "uri": "at://did/post/reposted-elsewhere",
                "cid": "cid-repost",
                "author": {"did": "did:plc:other", "handle": "someone-else.bsky.social"},
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": "not mine",
                    "createdAt": "2026-08-05T09:00:00.000Z",
                },
                "indexedAt": "2026-08-05T09:00:00.000Z",
            },
            "reason": {
                "$type": "app.bsky.feed.defs#reasonRepost",
                "by": {"did": "did:plc:abc", "handle": "creator.bsky.social"},
                "indexedAt": "2026-08-05T09:00:00.000Z",
            },
        },
    ],
    "cursor": "opaque-next-page-cursor",
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


def bluesky_connector(payload: object, *, status: int = 200) -> BlueskyConnector:
    session = FakeSession([FakeResponse(status, payload)])
    return BlueskyConnector(session, now=lambda: NOW)


def bluesky_connector_from_session(session: FakeSession) -> BlueskyConnector:
    return BlueskyConnector(session, now=lambda: NOW)


def bsky_account(external_id: str = "did:plc:abc") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.BLUESKY, external_id),
        owner_member_id=222,
        platform=Platform.BLUESKY,
        handle="creator.bsky.social",
        canonical_url="https://bsky.app/profile/creator.bsky.social",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_bluesky_connector_ignores_reposts_and_replies_by_default() -> None:
    page = await bluesky_connector(BLUESKY_AUTHOR_FEED).fetch_page(bsky_account(), cursor=None)
    assert [event.get("external_id") for event in page.items] == ["at://did/post/original"]


@pytest.mark.asyncio
async def test_fetch_page_uses_the_newest_post_uri_as_the_next_watermark() -> None:
    page = await bluesky_connector(BLUESKY_AUTHOR_FEED).fetch_page(bsky_account(), cursor=None)
    assert page.next_cursor == "at://did/post/original"


@pytest.mark.asyncio
async def test_fetch_page_maps_original_post_fields() -> None:
    page = await bluesky_connector(BLUESKY_AUTHOR_FEED).fetch_page(bsky_account(), cursor=None)

    item = page.items[0]
    assert item["kind"] == ContentKind.POST.value
    assert item["state"] == ContentState.PUBLISHED.value
    assert item["canonical_url"] == "https://bsky.app/profile/creator.bsky.social/post/original"
    assert item["published_at"] == "2026-08-05T10:00:00.000Z"


@pytest.mark.asyncio
async def test_fetch_page_stops_at_the_prior_watermark_uri() -> None:
    session = FakeSession([FakeResponse(200, BLUESKY_AUTHOR_FEED)])
    page = await bluesky_connector_from_session(session).fetch_page(
        bsky_account(), cursor="at://did/post/original"
    )

    assert page.items == ()
    assert page.next_cursor == "at://did/post/original"


@pytest.mark.asyncio
async def test_fetch_page_requests_the_author_feed_for_the_resolved_did() -> None:
    session = FakeSession([FakeResponse(200, BLUESKY_AUTHOR_FEED)])
    await bluesky_connector_from_session(session).fetch_page(bsky_account(), cursor=None)

    url, kwargs = session.requests[0]
    assert url == GET_AUTHOR_FEED_URL
    params = cast("dict[str, object]", kwargs["params"])
    assert params["actor"] == "did:plc:abc"


@pytest.mark.asyncio
async def test_resolve_account_resolves_handle_to_did() -> None:
    session = FakeSession([FakeResponse(200, {"did": "did:plc:abc"})])

    result = await bluesky_connector_from_session(session).resolve_account(
        RecognizedAccountUrl(
            platform=Platform.BLUESKY,
            handle="creator.bsky.social",
            canonical_url="https://bsky.app/profile/creator.bsky.social",
        )
    )

    assert result.external_id == "did:plc:abc"
    url, kwargs = session.requests[0]
    assert url == RESOLVE_HANDLE_URL
    params = cast("dict[str, object]", kwargs["params"])
    assert params["handle"] == "creator.bsky.social"


@pytest.mark.asyncio
async def test_resolve_account_raises_not_found_for_unknown_handle() -> None:
    session = FakeSession(
        [FakeResponse(400, {"error": "InvalidRequest", "message": "Unable to resolve handle"})]
    )

    with pytest.raises(BlueskyConnectorError) as excinfo:
        await bluesky_connector_from_session(session).resolve_account(
            RecognizedAccountUrl(
                platform=Platform.BLUESKY,
                handle="nobody.bsky.social",
                canonical_url="https://bsky.app/profile/nobody.bsky.social",
            )
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.NOT_FOUND


@pytest.mark.asyncio
async def test_fetch_page_raises_rate_limited_on_429() -> None:
    with pytest.raises(BlueskyConnectorError) as excinfo:
        await bluesky_connector({}, status=429).fetch_page(bsky_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED
    assert excinfo.value.retry_after_seconds is None


@pytest.mark.asyncio
async def test_fetch_page_reports_retry_after_header_on_429() -> None:
    session = FakeSession([FakeResponse(429, {}, headers={"Retry-After": "60"})])
    with pytest.raises(BlueskyConnectorError) as excinfo:
        await bluesky_connector_from_session(session).fetch_page(bsky_account(), cursor=None)
    assert excinfo.value.retry_after_seconds == 60.0


@pytest.mark.asyncio
async def test_fetch_page_raises_invalid_response_on_malformed_json() -> None:
    session = FakeSession([InvalidJsonResponse(200, None)])
    with pytest.raises(BlueskyConnectorError) as excinfo:
        await bluesky_connector_from_session(session).fetch_page(bsky_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_page_raises_timeout_on_transport_timeout() -> None:
    session = FakeSession([TimeoutError()])
    with pytest.raises(BlueskyConnectorError) as excinfo:
        await bluesky_connector_from_session(session).fetch_page(bsky_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_health_reports_ready_until_a_failure_is_observed() -> None:
    session = FakeSession([FakeResponse(503, {})])
    client = bluesky_connector_from_session(session)

    healthy = await client.health()
    assert healthy.capability is Capability.SOCIAL
    assert healthy.state is CapabilityState.READY

    with pytest.raises(BlueskyConnectorError):
        await client.fetch_page(bsky_account(), cursor=None)

    degraded = await client.health()
    assert degraded.state is CapabilityState.DEGRADED
