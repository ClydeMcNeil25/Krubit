from __future__ import annotations

import hashlib
import hmac

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.integrations.youtube import (
    YouTubePushError,
    YouTubePushEvent,
    build_youtube_push_routes,
    parse_youtube_push,
    verify_push_challenge,
    verify_push_signature,
    youtube_feed_topic,
)
from krubit.web.callbacks import CallbackServer

YOUTUBE_ATOM_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:video-123</id>
    <yt:videoId>video-123</yt:videoId>
    <yt:channelId>channel-456</yt:channelId>
    <title>Krucial Studios live now</title>
    <published>2026-08-05T12:00:00+00:00</published>
  </entry>
</feed>
"""

YOUTUBE_DELETED_ATOM_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:at="http://purl.org/atompub/tombstones/1.0"
      xmlns="http://www.w3.org/2005/Atom">
  <at:deleted-entry ref="yt:video:video-123" when="2026-08-05T12:05:00+00:00">
    <link href="https://www.youtube.com/watch?v=video-123"/>
    <author>
      <name>Krucial Studios</name>
      <uri>https://www.youtube.com/channel/channel-456</uri>
    </author>
  </at:deleted-entry>
</feed>
"""


def test_push_entry_becomes_video_identity_but_requires_api_enrichment() -> None:
    event = parse_youtube_push(YOUTUBE_ATOM_FIXTURE)
    assert event.video_id == "video-123"
    assert event.channel_id == "channel-456"
    assert event.deleted is False


def test_push_deleted_entry_is_recognized_without_a_title_or_state() -> None:
    event = parse_youtube_push(YOUTUBE_DELETED_ATOM_FIXTURE)
    assert event == YouTubePushEvent(video_id="video-123", channel_id="channel-456", deleted=True)


@pytest.mark.parametrize(
    "atom",
    [
        b"not xml at all",
        b"<feed></feed>",
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry></entry></feed>',
        b"<!DOCTYPE feed [<!ENTITY x 'y'>]><feed/>",
    ],
)
def test_parse_youtube_push_rejects_malformed_or_incomplete_payloads(atom: bytes) -> None:
    with pytest.raises(YouTubePushError):
        parse_youtube_push(atom)


def test_verify_push_challenge_echoes_only_a_matching_subscribe_topic() -> None:
    topic = youtube_feed_topic("channel-456")
    query = {"hub.mode": "subscribe", "hub.topic": topic, "hub.challenge": "abc123"}

    assert verify_push_challenge(query, expected_topic=topic) == "abc123"


@pytest.mark.parametrize(
    "query",
    [
        {"hub.mode": "publish", "hub.topic": "t", "hub.challenge": "abc"},
        {"hub.mode": "subscribe", "hub.topic": "wrong-topic", "hub.challenge": "abc"},
        {"hub.mode": "subscribe", "hub.topic": "t"},
        {},
    ],
)
def test_verify_push_challenge_rejects_unrecognized_requests(query: dict[str, str]) -> None:
    assert verify_push_challenge(query, expected_topic="t") is None


def test_verify_push_signature_accepts_a_correctly_signed_body() -> None:
    body = b"payload-bytes"
    secret = "shared-secret"
    signature = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()

    assert verify_push_signature(body, signature, secret) is True


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha1=deadbeef", "md5=" + hashlib.md5(b"payload-bytes").hexdigest(), "garbage"],
)
def test_verify_push_signature_rejects_missing_or_invalid_signatures(signature: str | None) -> None:
    assert verify_push_signature(b"payload-bytes", signature, "shared-secret") is False


@pytest.mark.asyncio
async def test_push_route_challenge_get_round_trips_through_the_callback_server() -> None:
    topic = youtube_feed_topic("channel-456")
    routes = build_youtube_push_routes(
        path="/callbacks/youtube",
        channel_id="channel-456",
        callback_secret="shared-secret",
        handle_event=_noop,
    )
    server = CallbackServer(
        public_base_url="https://example.com", port=8080, routes=routes
    )
    app = server.build_app()

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/callbacks/youtube",
            params={"hub.mode": "subscribe", "hub.topic": topic, "hub.challenge": "verify-me"},
        )
        assert response.status == 200
        assert await response.text() == "verify-me"

        rejected = await client.get(
            "/callbacks/youtube",
            params={"hub.mode": "subscribe", "hub.topic": "wrong", "hub.challenge": "verify-me"},
        )
        assert rejected.status == 404


@pytest.mark.asyncio
async def test_push_route_post_rejects_an_unverified_signature_before_ingestion() -> None:
    received: list[YouTubePushEvent] = []

    async def record(event: YouTubePushEvent) -> None:
        received.append(event)

    routes = build_youtube_push_routes(
        path="/callbacks/youtube",
        channel_id="channel-456",
        callback_secret="shared-secret",
        handle_event=record,
    )
    server = CallbackServer(public_base_url="https://example.com", port=8080, routes=routes)
    app = server.build_app()

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/callbacks/youtube",
            data=YOUTUBE_ATOM_FIXTURE,
            headers={"X-Hub-Signature": "sha1=deadbeef"},
        )
        assert response.status == 403
        assert received == []


@pytest.mark.asyncio
async def test_push_route_post_ingests_only_after_a_verified_signature() -> None:
    received: list[YouTubePushEvent] = []

    async def record(event: YouTubePushEvent) -> None:
        received.append(event)

    secret = "shared-secret"
    routes = build_youtube_push_routes(
        path="/callbacks/youtube",
        channel_id="channel-456",
        callback_secret=secret,
        handle_event=record,
    )
    server = CallbackServer(public_base_url="https://example.com", port=8080, routes=routes)
    app = server.build_app()
    signature = "sha1=" + hmac.new(secret.encode(), YOUTUBE_ATOM_FIXTURE, hashlib.sha1).hexdigest()

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/callbacks/youtube",
            data=YOUTUBE_ATOM_FIXTURE,
            headers={"X-Hub-Signature": signature},
        )
        assert response.status == 204
        assert len(received) == 1
        assert received[0].video_id == "video-123"


@pytest.mark.asyncio
async def test_push_route_post_never_calls_ingestion_for_malformed_verified_body() -> None:
    received: list[YouTubePushEvent] = []

    async def record(event: YouTubePushEvent) -> None:
        received.append(event)

    secret = "shared-secret"
    routes = build_youtube_push_routes(
        path="/callbacks/youtube",
        channel_id="channel-456",
        callback_secret=secret,
        handle_event=record,
    )
    server = CallbackServer(public_base_url="https://example.com", port=8080, routes=routes)
    app = server.build_app()
    body = b"not-xml"
    signature = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/callbacks/youtube",
            data=body,
            headers={"X-Hub-Signature": signature},
        )
        assert response.status == 500
        assert received == []


async def _noop(event: YouTubePushEvent) -> None:
    return None
