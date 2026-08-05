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
from krubit.integrations.meta import (
    FacebookPageConnector,
    FacebookProfileConnector,
    InstagramConnector,
    MetaConnectorError,
    ThreadsConnector,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

INSTAGRAM_MEDIA_FIXTURE = {
    "data": [
        {
            "id": "reel-1",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/reel-1/",
            "timestamp": "2026-08-05T10:00:00+0000",
            "caption": "New reel!",
        },
        {
            "id": "post-2",
            "media_type": "IMAGE",
            "media_product_type": "FEED",
            "permalink": "https://www.instagram.com/p/post-2/",
            "timestamp": "2026-08-05T09:00:00+0000",
        },
        {
            "id": "live-3",
            "media_type": "VIDEO",
            "media_product_type": "LIVE",
            "permalink": "https://www.instagram.com/p/live-3/",
            "timestamp": "2026-08-05T08:00:00+0000",
            "is_live_now": True,
        },
    ],
    "paging": {"cursors": {"after": "cursor-3"}},
}

FACEBOOK_PAGE_POSTS_FIXTURE = {
    "data": [
        {
            "id": "post-1",
            "message": "Big announcement",
            "created_time": "2026-08-05T10:00:00+0000",
            "permalink_url": "https://www.facebook.com/page/posts/post-1",
        }
    ]
}
FACEBOOK_PAGE_VIDEOS_FIXTURE = {
    "data": [
        {
            "id": "video-1",
            "description": "Regular upload",
            "created_time": "2026-08-05T09:00:00+0000",
            "permalink_url": "https://www.facebook.com/page/videos/video-1",
            "is_reels_video": False,
        },
        {
            "id": "reel-1",
            "description": "Quick reel",
            "created_time": "2026-08-05T08:00:00+0000",
            "permalink_url": "https://www.facebook.com/page/videos/reel-1",
            "is_reels_video": True,
        },
    ]
}
FACEBOOK_PAGE_LIVE_FIXTURE = {
    "data": [
        {
            "id": "live-1",
            "title": "Weekly stream",
            "status": "LIVE",
            "creation_time": "2026-08-05T11:00:00+0000",
            "permalink_url": "https://www.facebook.com/page/videos/live-1",
        },
        {
            "id": "live-2",
            "title": "Cancelled AMA",
            "status": "SCHEDULED_CANCELED",
            "creation_time": "2026-08-05T07:00:00+0000",
        },
    ]
}

EXPIRED_TOKEN_ERROR_FIXTURE = {
    "error": {"message": "Error validating access token", "type": "OAuthException", "code": 190}
}

THREADS_FIXTURE = {
    "data": [
        {
            "id": "thread-1",
            "text": "An original thought",
            "permalink": "https://www.threads.net/t/thread-1",
            "timestamp": "2026-08-05T10:00:00+0000",
            "is_quote_post": False,
            "root_post": None,
        },
        {
            "id": "thread-2",
            "text": "Quoting someone else",
            "permalink": "https://www.threads.net/t/thread-2",
            "timestamp": "2026-08-05T09:30:00+0000",
            "is_quote_post": True,
        },
        {
            "id": "thread-3",
            "text": "A reply to another thread",
            "permalink": "https://www.threads.net/t/thread-3",
            "timestamp": "2026-08-05T09:00:00+0000",
            "is_quote_post": False,
            "root_post": "thread-0",
        },
    ],
    "paging": {"cursors": {"after": "cursor-t3"}},
}


class FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

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


def instagram_connector(payload: object, *, status: int = 200) -> InstagramConnector:
    session = FakeSession([FakeResponse(status, payload)])
    return InstagramConnector(session, "ig-access-token", now=lambda: NOW)


def ig_account(external_id: str = "ig-1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.INSTAGRAM, external_id),
        owner_member_id=222,
        platform=Platform.INSTAGRAM,
        handle="krucialstudios",
        canonical_url="https://www.instagram.com/krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def facebook_page_account(external_id: str = "page-1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.FACEBOOK_PAGE, external_id),
        owner_member_id=222,
        platform=Platform.FACEBOOK_PAGE,
        handle="krucialstudios",
        canonical_url="https://www.facebook.com/page-1",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def facebook_profile_account(external_id: str = "profile-1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.FACEBOOK, external_id),
        owner_member_id=222,
        platform=Platform.FACEBOOK,
        handle="krucialcreator",
        canonical_url="https://www.facebook.com/krucialcreator",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


def threads_account(external_id: str = "threads-1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.THREADS, external_id),
        owner_member_id=222,
        platform=Platform.THREADS,
        handle="krucialstudios",
        canonical_url="https://www.threads.net/@krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


# --------------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instagram_separates_reel_post_and_active_live_media() -> None:
    page = await instagram_connector(INSTAGRAM_MEDIA_FIXTURE).fetch_page(ig_account(), cursor=None)
    assert [item["kind"] for item in page.items] == [
        ContentKind.REEL.value,
        ContentKind.POST.value,
        ContentKind.LIVE.value,
    ]


@pytest.mark.asyncio
async def test_instagram_live_media_state_reflects_is_live_now() -> None:
    page = await instagram_connector(INSTAGRAM_MEDIA_FIXTURE).fetch_page(ig_account(), cursor=None)
    live_item = page.items[2]
    assert live_item["state"] == ContentState.LIVE.value


@pytest.mark.asyncio
async def test_instagram_ended_live_media_when_not_currently_live() -> None:
    fixture = {
        "data": [
            {
                "id": "live-4",
                "media_type": "VIDEO",
                "media_product_type": "LIVE",
                "permalink": "https://www.instagram.com/p/live-4/",
                "is_live_now": False,
            }
        ]
    }
    page = await instagram_connector(fixture).fetch_page(ig_account(), cursor=None)
    assert page.items[0]["state"] == ContentState.ENDED.value


@pytest.mark.asyncio
async def test_instagram_next_cursor_from_paging_cursors_after() -> None:
    page = await instagram_connector(INSTAGRAM_MEDIA_FIXTURE).fetch_page(ig_account(), cursor=None)
    assert page.next_cursor == "cursor-3"


@pytest.mark.asyncio
async def test_instagram_resolve_account_uses_me_endpoint() -> None:
    session = FakeSession(
        [FakeResponse(200, {"id": "ig-1", "username": "krucialstudios", "name": "Krucial Studios"})]
    )
    connector = InstagramConnector(session, "ig-access-token", now=lambda: NOW)
    result = await connector.resolve_account(
        RecognizedAccountUrl(
            platform=Platform.INSTAGRAM,
            handle="krucialstudios",
            canonical_url="https://www.instagram.com/krucialstudios",
        )
    )
    assert result.external_id == "ig-1"
    assert result.display_name == "Krucial Studios"
    url, kwargs = session.requests[0]
    assert url == "https://graph.facebook.com/v19.0/me"
    params = cast("dict[str, object]", kwargs["params"])
    assert params["access_token"] == "ig-access-token"


@pytest.mark.asyncio
async def test_instagram_raises_authorization_on_expired_token() -> None:
    payload = EXPIRED_TOKEN_ERROR_FIXTURE
    with pytest.raises(MetaConnectorError) as excinfo:
        await instagram_connector(payload, status=400).fetch_page(ig_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_instagram_raises_authorization_on_insufficient_scope() -> None:
    payload = {
        "error": {
            "message": "(#200) Requires instagram_basic permission",
            "type": "OAuthException",
            "code": 200,
        }
    }
    with pytest.raises(MetaConnectorError) as excinfo:
        await instagram_connector(payload, status=403).fetch_page(ig_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_instagram_raises_rate_limited_on_throttling() -> None:
    payload = {"error": {"message": "Too many calls", "type": "OAuthException", "code": 4}}
    with pytest.raises(MetaConnectorError) as excinfo:
        await instagram_connector(payload, status=400).fetch_page(ig_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_instagram_raises_invalid_response_on_malformed_json() -> None:
    session = FakeSession([InvalidJsonResponse(200, None)])
    connector = InstagramConnector(session, "ig-access-token", now=lambda: NOW)
    with pytest.raises(MetaConnectorError) as excinfo:
        await connector.fetch_page(ig_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_instagram_health_moves_to_authorization_required_after_token_expiry() -> None:
    payload = EXPIRED_TOKEN_ERROR_FIXTURE
    connector = instagram_connector(payload, status=400)

    healthy = await connector.health()
    assert healthy.state is CapabilityState.READY

    with pytest.raises(MetaConnectorError):
        await connector.fetch_page(ig_account(), cursor=None)

    degraded = await connector.health()
    assert degraded.capability is Capability.SOCIAL
    assert degraded.state is CapabilityState.AUTHORIZATION_REQUIRED


# --------------------------------------------------------------------------------
# Facebook Page
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facebook_page_distinguishes_post_video_reel_and_live_broadcast() -> None:
    session = FakeSession(
        [
            FakeResponse(200, FACEBOOK_PAGE_POSTS_FIXTURE),
            FakeResponse(200, FACEBOOK_PAGE_VIDEOS_FIXTURE),
            FakeResponse(200, FACEBOOK_PAGE_LIVE_FIXTURE),
        ]
    )
    connector = FacebookPageConnector(session, "page-access-token", now=lambda: NOW)
    page = await connector.fetch_page(facebook_page_account(), cursor=None)

    by_id = {item["external_id"]: item for item in page.items}
    assert by_id["post-1"]["kind"] == ContentKind.POST.value
    assert by_id["video-1"]["kind"] == ContentKind.VIDEO.value
    assert by_id["reel-1"]["kind"] == ContentKind.REEL.value
    assert by_id["live-1"]["kind"] == ContentKind.LIVE.value
    assert by_id["live-1"]["state"] == ContentState.LIVE.value
    assert by_id["live-2"]["state"] == ContentState.CANCELLED.value


@pytest.mark.asyncio
async def test_facebook_page_health_degrades_on_authorization_failure_only() -> None:
    payload = EXPIRED_TOKEN_ERROR_FIXTURE
    session = FakeSession([FakeResponse(400, payload)])
    connector = FacebookPageConnector(session, "page-access-token", now=lambda: NOW)
    with pytest.raises(MetaConnectorError) as excinfo:
        await connector.fetch_page(facebook_page_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION
    health = await connector.health()
    assert health.state is CapabilityState.AUTHORIZATION_REQUIRED


# --------------------------------------------------------------------------------
# Facebook personal profile
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facebook_profile_requires_approval_without_app_review_grant() -> None:
    connector = FacebookProfileConnector(FakeSession([]), "profile-access-token", now=lambda: NOW)

    health = await connector.health()
    assert health.state is CapabilityState.APPROVAL_REQUIRED

    with pytest.raises(MetaConnectorError) as excinfo:
        await connector.fetch_page(facebook_profile_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_facebook_profile_reads_owner_posts_when_app_review_granted() -> None:
    session = FakeSession([FakeResponse(200, FACEBOOK_PAGE_POSTS_FIXTURE)])
    connector = FacebookProfileConnector(
        session, "profile-access-token", app_review_granted=True, now=lambda: NOW
    )

    page = await connector.fetch_page(facebook_profile_account(), cursor=None)
    assert [item["kind"] for item in page.items] == [ContentKind.POST.value]

    health = await connector.health()
    assert health.state is CapabilityState.READY


# --------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threads_surfaces_only_original_posts() -> None:
    session = FakeSession([FakeResponse(200, THREADS_FIXTURE)])
    connector = ThreadsConnector(session, "threads-access-token", now=lambda: NOW)

    page = await connector.fetch_page(threads_account(), cursor=None)

    assert [item["external_id"] for item in page.items] == ["thread-1"]
    assert page.items[0]["kind"] == ContentKind.POST.value
    assert page.next_cursor == "cursor-t3"
