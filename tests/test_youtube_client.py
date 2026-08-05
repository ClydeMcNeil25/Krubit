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
from krubit.integrations.youtube import (
    CHANNELS_URL,
    PLAYLIST_ITEMS_URL,
    VIDEOS_URL,
    YouTubeConnector,
    YouTubeConnectorError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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


def connector(session: FakeSession) -> YouTubeConnector:
    return YouTubeConnector(session, "api-key", now=lambda: NOW)


def account(external_id: str = "UC-one") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
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


def channel_payload(*, channel_id: str = "UC-one", uploads: str = "UU-one") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "items": [
                {
                    "id": channel_id,
                    "snippet": {"title": "Krucial Studios"},
                    "contentDetails": {"relatedPlaylists": {"uploads": uploads}},
                }
            ]
        },
    )


def playlist_items_payload(
    video_ids: list[str], *, next_page_token: str | None = None
) -> FakeResponse:
    body: dict[str, object] = {
        "items": [{"contentDetails": {"videoId": video_id}} for video_id in video_ids]
    }
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    return FakeResponse(200, body)


def video_item(
    video_id: str,
    *,
    title: str = "A Video",
    duration: str = "PT10M0S",
    broadcast: str = "none",
    privacy: str = "public",
    upload_status: str = "processed",
    live_details: dict[str, object] | None = None,
    published_at: str = "2026-08-05T10:00:00Z",
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": video_id,
        "snippet": {
            "title": title,
            "liveBroadcastContent": broadcast,
            "publishedAt": published_at,
        },
        "contentDetails": {"duration": duration},
        "status": {"privacyStatus": privacy, "uploadStatus": upload_status},
    }
    if live_details is not None:
        item["liveStreamingDetails"] = live_details
    return item


def videos_payload(items: list[dict[str, object]]) -> FakeResponse:
    return FakeResponse(200, {"items": items})


@pytest.mark.asyncio
async def test_resolve_account_uses_channels_list_for_handle() -> None:
    session = FakeSession([channel_payload()])

    result = await connector(session).resolve_account(
        RecognizedAccountUrl(
            platform=Platform.YOUTUBE,
            handle="krucialstudios",
            canonical_url="https://www.youtube.com/@krucialstudios",
        )
    )

    assert result.external_id == "UC-one"
    assert result.display_name == "Krucial Studios"
    url, kwargs = session.requests[0]
    assert url == CHANNELS_URL
    params = cast("dict[str, object]", kwargs["params"])
    assert params["forHandle"] == "@krucialstudios"


@pytest.mark.asyncio
async def test_resolve_account_raises_not_found_for_an_unknown_handle() -> None:
    session = FakeSession([FakeResponse(200, {"items": []})])

    with pytest.raises(YouTubeConnectorError) as excinfo:
        await connector(session).resolve_account(
            RecognizedAccountUrl(
                platform=Platform.YOUTUBE,
                handle="nobody",
                canonical_url="https://www.youtube.com/@nobody",
            )
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.NOT_FOUND


@pytest.mark.asyncio
async def test_fetch_page_polls_playlist_items_then_enriches_with_videos_list() -> None:
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload(["v1"], next_page_token="page-2"),
            videos_payload([video_item("v1", title="Upload One")]),
        ]
    )

    page = await connector(session).fetch_page(account(), cursor=None)

    assert page.next_cursor == "page-2"
    assert len(page.items) == 1
    item = page.items[0]
    assert item["external_id"] == "v1"
    assert item["kind"] == ContentKind.VIDEO.value
    assert item["state"] == ContentState.PUBLISHED.value
    assert item["canonical_url"] == "https://www.youtube.com/watch?v=v1"
    assert item["title"] == "Upload One"

    requested_urls = [request[0] for request in session.requests]
    assert requested_urls == [CHANNELS_URL, PLAYLIST_ITEMS_URL, VIDEOS_URL]


@pytest.mark.asyncio
async def test_fetch_page_caches_the_uploads_playlist_across_calls() -> None:
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload([]),
            playlist_items_payload([]),
        ]
    )
    client = connector(session)

    await client.fetch_page(account(), cursor=None)
    await client.fetch_page(account(), cursor="page-2")

    requested_urls = [request[0] for request in session.requests]
    assert requested_urls.count(CHANNELS_URL) == 1


@pytest.mark.asyncio
async def test_fetch_page_classifies_shorts_by_duration() -> None:
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload(["short-1"]),
            videos_payload([video_item("short-1", duration="PT45S")]),
        ]
    )

    page = await connector(session).fetch_page(account(), cursor=None)

    assert page.items[0]["kind"] == ContentKind.SHORT.value


@pytest.mark.asyncio
async def test_fetch_page_maps_upcoming_live_and_completed_broadcasts() -> None:
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload(["upcoming", "live", "ended"]),
            videos_payload(
                [
                    video_item(
                        "upcoming",
                        broadcast="upcoming",
                        live_details={"scheduledStartTime": "2026-08-05T14:00:00Z"},
                    ),
                    video_item(
                        "live",
                        broadcast="live",
                        live_details={"actualStartTime": "2026-08-05T12:00:00Z"},
                    ),
                    video_item(
                        "ended",
                        broadcast="none",
                        live_details={
                            "actualStartTime": "2026-08-05T10:00:00Z",
                            "actualEndTime": "2026-08-05T11:00:00Z",
                        },
                    ),
                ]
            ),
        ]
    )

    page = await connector(session).fetch_page(account(), cursor=None)

    mapped = {item["external_id"]: item for item in page.items}
    assert mapped["upcoming"]["kind"] == ContentKind.LIVE.value
    assert mapped["upcoming"]["state"] == ContentState.SCHEDULED.value
    assert mapped["live"]["kind"] == ContentKind.LIVE.value
    assert mapped["live"]["state"] == ContentState.LIVE.value
    assert mapped["ended"]["kind"] == ContentKind.LIVE.value
    assert mapped["ended"]["state"] == ContentState.ENDED.value


@pytest.mark.asyncio
async def test_fetch_page_maps_unavailable_private_video_to_failed() -> None:
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload(["private-1"]),
            videos_payload([video_item("private-1", privacy="private")]),
        ]
    )

    page = await connector(session).fetch_page(account(), cursor=None)

    assert page.items[0]["state"] == ContentState.FAILED.value


@pytest.mark.asyncio
async def test_fetch_page_batches_videos_list_calls_at_fifty() -> None:
    video_ids = [f"v{n}" for n in range(75)]
    session = FakeSession(
        [
            channel_payload(),
            playlist_items_payload(video_ids),
            videos_payload([video_item(video_id) for video_id in video_ids[:50]]),
            videos_payload([video_item(video_id) for video_id in video_ids[50:]]),
        ]
    )

    page = await connector(session).fetch_page(account(), cursor=None)

    assert len(page.items) == 75
    requested_urls = [request[0] for request in session.requests]
    assert requested_urls.count(VIDEOS_URL) == 2


@pytest.mark.asyncio
async def test_fetch_page_raises_quota_exceeded_on_a_403_quota_error() -> None:
    session = FakeSession(
        [
            channel_payload(),
            FakeResponse(
                403,
                {"error": {"errors": [{"reason": "quotaExceeded"}]}},
            ),
        ]
    )

    with pytest.raises(YouTubeConnectorError) as excinfo:
        await connector(session).fetch_page(account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_fetch_page_raises_timeout_on_a_transport_timeout() -> None:
    session = FakeSession([channel_payload(), TimeoutError()])

    with pytest.raises(YouTubeConnectorError) as excinfo:
        await connector(session).fetch_page(account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_fetch_page_raises_invalid_response_for_malformed_json() -> None:
    session = FakeSession([channel_payload(), InvalidJsonResponse(200, None)])

    with pytest.raises(YouTubeConnectorError) as excinfo:
        await connector(session).fetch_page(account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_health_reports_ready_until_a_failure_is_observed() -> None:
    session = FakeSession(
        [FakeResponse(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})]
    )
    client = connector(session)

    healthy = await client.health()
    assert healthy.capability is Capability.SOCIAL
    assert healthy.state is CapabilityState.READY

    with pytest.raises(YouTubeConnectorError):
        await client.resolve_account(
            RecognizedAccountUrl(
                platform=Platform.YOUTUBE,
                handle="krucialstudios",
                canonical_url="https://www.youtube.com/@krucialstudios",
            )
        )

    degraded = await client.health()
    assert degraded.state is CapabilityState.QUOTA_LIMITED


@pytest.mark.asyncio
async def test_ingest_push_enriches_a_non_deleted_event_through_videos_list() -> None:
    from krubit.integrations.youtube import YouTubePushEvent

    session = FakeSession([videos_payload([video_item("v1", title="Fresh Upload")])])

    item = await connector(session).ingest_push(
        YouTubePushEvent(video_id="v1", channel_id="UC-one", deleted=False)
    )

    assert item["external_id"] == "v1"
    assert item["title"] == "Fresh Upload"


@pytest.mark.asyncio
async def test_ingest_push_maps_a_deleted_tombstone_to_retracted_without_a_lookup() -> None:
    from krubit.integrations.youtube import YouTubePushEvent

    session = FakeSession([])

    item = await connector(session).ingest_push(
        YouTubePushEvent(video_id="v1", channel_id="UC-one", deleted=True)
    )

    assert item["state"] == ContentState.RETRACTED.value
    assert session.requests == []


@pytest.mark.asyncio
async def test_ingest_push_maps_a_missing_video_to_unavailable_failed() -> None:
    from krubit.integrations.youtube import YouTubePushEvent

    session = FakeSession([videos_payload([])])

    item = await connector(session).ingest_push(
        YouTubePushEvent(video_id="v1", channel_id="UC-one", deleted=False)
    )

    assert item["state"] == ContentState.FAILED.value
