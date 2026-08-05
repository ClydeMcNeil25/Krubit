"""YouTube Data API v3 connector: quota-conscious polling, push-Atom parsing, and
push-subscription challenge/signature verification.

Mirrors the shape of `krubit.integrations.twitch.TwitchHelixClient` for HTTP client
structure (an injected `aiohttp`-like session, `ConnectorFailure`-based error
taxonomy) but is adapted to YouTube's REST API and PubSubHubbub/WebSub push model:

- `channels.list` resolves a channel handle to its channel id and "uploads" playlist.
- `playlistItems.list` polls that uploads playlist cheaply (1 quota unit) instead of
  the 100-unit `search.list` — `YouTubeConnector` never calls `search.list`.
- `videos.list` enriches a batch of video ids with lifecycle/live metadata.
- YouTube's push notifications deliver a terse Atom entry identifying a changed or
  deleted video. `parse_youtube_push` extracts only that bounded identity;
  `YouTubeConnector.ingest_push` always re-enriches a non-deleted push through
  `videos.list` before it can reach the content ledger — the push payload's own claims
  about title or state are never trusted directly.

Content lifecycle mapping (YouTube -> shared `ContentState`):
  - `upcoming` (scheduled broadcast, not yet started)              -> SCHEDULED
  - `live` (currently broadcasting)                                -> LIVE
  - a broadcast whose `liveStreamingDetails.actualEndTime` is set
    (was live, now finished)                                       -> ENDED
  - a plain, publicly viewable, non-broadcast video/Short           -> PUBLISHED
  - a push tombstone (`at:deleted-entry`)                           -> RETRACTED
  - a video `videos.list` can no longer return, or one whose status
    is `private`/`rejected`/`failed` (not an explicit tombstone)    -> FAILED

Shorts are approximated from `contentDetails.duration`: the public API exposes no
official "is Short" flag, so a non-live video at or under 60 seconds is classified as
`ContentKind.SHORT`.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import aiohttp

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    ContentKind,
    ContentState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import (
    ConnectorAccount,
    ConnectorFailure,
    ConnectorFailureKind,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.integrations.catalog import CATALOG
from krubit.web.callbacks import CallbackRoute, PushSubscription, build_push_route

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

_MAX_PLAYLIST_PAGE_SIZE = 50
_MAX_VIDEOS_PER_CALL = 50
_SHORT_MAX_SECONDS = 60
_MAX_TITLE_LENGTH = 300

_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_TOMBSTONE_NS = "http://purl.org/atompub/tombstones/1.0"

_VIDEO_URL_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DELETED_REF_PATTERN = re.compile(r"yt:video:(?P<video_id>[A-Za-z0-9_-]{1,64})")
_DURATION_PATTERN = re.compile(
    r"P(?:\d+D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def normalize_youtube_video_url(url: str) -> str | None:
    """Return the video id for a canonical `watch?v=` or `live/` YouTube URL, else None.

    Used both by push/API enrichment (to build canonical URLs) and by Discord presence
    detection (`krubit.discord.live_runtime.extract_streaming_observation`) to validate
    a member's streaming-activity URL without duplicating URL parsing.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in _VIDEO_URL_HOSTS:
        return None
    if parsed.path == "/watch":
        values = parse_qs(parsed.query).get("v")
        video_id = values[0] if values else None
    elif parsed.path.startswith("/live/"):
        video_id = parsed.path.removeprefix("/live/").rstrip("/")
    else:
        video_id = None
    if video_id is None or _VIDEO_ID_PATTERN.fullmatch(video_id) is None:
        return None
    return video_id


def _canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class YouTubePushError(ValueError):
    """Raised when a push payload is not a well-formed YouTube Atom notification."""


@dataclass(frozen=True, slots=True)
class YouTubePushEvent:
    """The bounded identity a push Atom entry carries before API enrichment.

    Deliberately carries no title, state, or timestamp: those are never trusted from
    the push payload itself. `YouTubeConnector.ingest_push` always re-fetches through
    `videos.list` for a non-deleted event.
    """

    video_id: str
    channel_id: str
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.video_id.strip():
            raise ValueError("video_id must not be blank")
        if not self.channel_id.strip():
            raise ValueError("channel_id must not be blank")


def parse_youtube_push(atom: bytes) -> YouTubePushEvent:
    """Parse a YouTube PubSubHubbub Atom notification into its bounded identity.

    Raises `YouTubePushError` for anything that is not a well-formed, recognized
    notification: unparsable XML, a DOCTYPE/entity declaration (rejected outright as a
    basic external-entity guard against a stdlib XML parser), a normal entry missing
    `yt:videoId`/`yt:channelId`, or a tombstone whose `ref`/author identity does not
    match YouTube's documented shape.
    """
    if b"<!DOCTYPE" in atom or b"<!ENTITY" in atom:
        raise YouTubePushError("push payload must not declare a DOCTYPE or entity")
    try:
        root = ElementTree.fromstring(atom)
    except ElementTree.ParseError as exc:
        raise YouTubePushError("push payload is not well-formed XML") from exc

    deleted_entry = root.find(f"{{{_TOMBSTONE_NS}}}deleted-entry")
    if deleted_entry is not None:
        return _parse_deleted_entry(deleted_entry)

    entry = root.find(f"{{{_ATOM_NS}}}entry")
    if entry is None:
        raise YouTubePushError("push payload has no entry")
    video_id = _text(entry, f"{{{_YT_NS}}}videoId")
    channel_id = _text(entry, f"{{{_YT_NS}}}channelId")
    if video_id is None or channel_id is None:
        raise YouTubePushError("push entry is missing videoId or channelId")
    return YouTubePushEvent(video_id=video_id, channel_id=channel_id, deleted=False)


def _parse_deleted_entry(deleted_entry: ElementTree.Element) -> YouTubePushEvent:
    ref = deleted_entry.get("ref", "")
    match = _DELETED_REF_PATTERN.fullmatch(ref)
    if match is None:
        raise YouTubePushError("deleted-entry ref is not a recognized video identity")
    author = deleted_entry.find(f"{{{_ATOM_NS}}}author")
    channel_id = None
    if author is not None:
        uri = _text(author, f"{{{_ATOM_NS}}}uri")
        if uri is not None:
            channel_id = uri.rstrip("/").rsplit("/", 1)[-1]
    if not channel_id:
        raise YouTubePushError("deleted-entry is missing a channel identity")
    return YouTubePushEvent(video_id=match.group("video_id"), channel_id=channel_id, deleted=True)


def _text(element: ElementTree.Element, tag: str) -> str | None:
    found = element.find(tag)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def verify_push_challenge(query: Mapping[str, str], *, expected_topic: str) -> str | None:
    """Answer a WebSub subscribe/unsubscribe verification GET, or reject it.

    Returns `hub.challenge` only when `hub.mode` is `subscribe`/`unsubscribe` and
    `hub.topic` matches the one Krubit actually subscribed for; otherwise `None`, so the
    caller renders a rejection rather than echo a challenge for an unrecognized topic.
    """
    mode = query.get("hub.mode")
    topic = query.get("hub.topic")
    challenge = query.get("hub.challenge")
    if mode not in {"subscribe", "unsubscribe"} or not challenge or not topic:
        return None
    if topic != expected_topic:
        return None
    return challenge


def verify_push_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify a push POST's `X-Hub-Signature` HMAC-SHA1 against the shared secret."""
    if not signature_header or not secret:
        return False
    algorithm, _, digest = signature_header.partition("=")
    if algorithm != "sha1" or not digest:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, sha1).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


def youtube_feed_topic(channel_id: str) -> str:
    """The canonical PubSubHubbub topic URL for one channel's upload feed."""
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


def build_youtube_push_routes(
    *,
    path: str,
    channel_id: str,
    callback_secret: str,
    handle_event: Callable[[YouTubePushEvent], Awaitable[None]],
) -> tuple[CallbackRoute, CallbackRoute]:
    """Build the GET/POST push routes for one subscribed YouTube channel feed.

    `channel_id` pins the expected `hub.topic` to exactly this channel's feed, so a
    challenge for a different topic is rejected rather than echoed. `callback_secret`
    is the shared secret supplied at subscribe time; a POST whose `X-Hub-Signature` does
    not verify against it is rejected with 403 before `atom` is ever parsed.
    """
    topic = youtube_feed_topic(channel_id)
    subscription = PushSubscription(
        verify_challenge=lambda query: verify_push_challenge(query, expected_topic=topic),
        verify_signature=lambda body, header: verify_push_signature(body, header, callback_secret),
    )

    async def handle_notification(body: bytes) -> None:
        event = parse_youtube_push(body)
        await handle_event(event)

    return build_push_route(
        path=path, subscription=subscription, handle_notification=handle_notification
    )


def _parse_iso8601_duration(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        return None
    parts = match.groupdict()
    if not any(parts.values()):
        return None
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return hours * 3_600 + minutes * 60 + seconds


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in raw.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _list(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return list(cast(list[object], value))


def _error_reason(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    error = _mapping(payload.get("error"))
    if error is None:
        return None
    errors = _list(error.get("errors"))
    if not errors:
        return None
    first = _mapping(errors[0])
    if first is None:
        return None
    reason = first.get("reason")
    return reason if isinstance(reason, str) else None


def _retracted_item(video_id: str) -> Mapping[str, JSONValue]:
    return {
        "external_id": video_id,
        "kind": ContentKind.VIDEO.value,
        "state": ContentState.RETRACTED.value,
        "canonical_url": _canonical_watch_url(video_id),
    }


def _unavailable_item(video_id: str) -> Mapping[str, JSONValue]:
    return {
        "external_id": video_id,
        "kind": ContentKind.VIDEO.value,
        "state": ContentState.FAILED.value,
        "canonical_url": _canonical_watch_url(video_id),
    }


_HEALTH_STATE_BY_FAILURE: Mapping[ConnectorFailureKind, CapabilityState] = {
    ConnectorFailureKind.AUTHORIZATION: CapabilityState.AUTHORIZATION_REQUIRED,
    ConnectorFailureKind.RATE_LIMITED: CapabilityState.DEGRADED,
    ConnectorFailureKind.QUOTA_EXCEEDED: CapabilityState.QUOTA_LIMITED,
    ConnectorFailureKind.UNAVAILABLE: CapabilityState.DEGRADED,
    ConnectorFailureKind.INVALID_RESPONSE: CapabilityState.DEGRADED,
    ConnectorFailureKind.TIMEOUT: CapabilityState.DEGRADED,
    ConnectorFailureKind.NOT_FOUND: CapabilityState.DEGRADED,
}


class YouTubeConnectorError(RuntimeError):
    """Raised by `YouTubeConnector` when a request cannot be completed.

    Carries the classified `ConnectorFailure` a caller renders safely; this exception's
    own message is the fixed `kind` name, never provider response text.
    `retry_after_seconds`, when known, is the real server-instructed (or, for a daily
    quota, API-documented) resume delay — see `_quota_reset_delay_seconds` and
    `_retry_after_seconds_from_headers` — so a scheduler never re-polls before the
    platform actually allows it.
    """

    def __init__(
        self, failure: ConnectorFailure, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(failure.kind.value)
        self.failure = failure
        self.retry_after_seconds = retry_after_seconds


class _Response(Protocol):
    status: int

    async def json(self) -> object: ...


class _RequestContext(Protocol):
    async def __aenter__(self) -> _Response: ...

    async def __aexit__(self, *args: object) -> None: ...


class _Session(Protocol):
    def get(self, url: str, **kwargs: object) -> _RequestContext: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


_QUOTA_RESET_ZONE = ZoneInfo("America/Los_Angeles")


def _quota_reset_delay_seconds(now: datetime) -> float:
    """Seconds until the next YouTube Data API daily quota reset (midnight Pacific).

    Google documents the YouTube Data API's daily quota as resetting at midnight
    Pacific time: https://developers.google.com/youtube/v3/getting-started#quota.
    Used so a `quotaExceeded`/`dailyLimitExceeded` failure schedules its next poll at
    the real reset instant — commonly 12+ hours away — instead of retrying every few
    hours against a quota that cannot possibly have recovered.
    """
    local = now.astimezone(_QUOTA_RESET_ZONE)
    next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (next_midnight - local).total_seconds())


def _retry_after_seconds_from_headers(headers: object) -> float | None:
    """Extract a numeric resume delay from a 429 response's `Retry-After` header."""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for key in ("Retry-After", "retry-after"):
        value = getter(key)
        if value is None:
            continue
        try:
            delay = float(str(value))
        except (TypeError, ValueError):
            continue
        if delay > 0:
            return delay
    return None


class YouTubeConnector:
    """Quota-conscious YouTube Data API v3 connector.

    Satisfies `krubit.integrations.base.Connector` structurally. `resolve_account` and
    `fetch_page` raise `YouTubeConnectorError` on failure (there is no result-union
    return type in the shared protocol); `health` never raises and instead reports the
    connector's last observed `ConnectorFailure`, if any, as an honest `CapabilityState`.
    """

    descriptor = CATALOG[Platform.YOUTUBE]

    def __init__(
        self,
        session: object,
        api_key: str,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._api_key = api_key
        self._now = now
        self._uploads_playlist_cache: dict[str, str] = {}
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(
            CHANNELS_URL,
            {"part": "snippet,contentDetails", "forHandle": f"@{recognized.handle}"},
        )
        items = _list(payload.get("items")) or []
        channel = _mapping(items[0]) if items else None
        if channel is None:
            raise self._fail(ConnectorFailure.not_found("channel not found"))
        channel_id = channel.get("id")
        snippet = _mapping(channel.get("snippet")) or {}
        content_details = _mapping(channel.get("contentDetails"))
        related = _mapping(content_details.get("relatedPlaylists")) if content_details else None
        uploads = related.get("uploads") if related else None
        if (
            not isinstance(channel_id, str)
            or not channel_id
            or not isinstance(uploads, str)
            or not uploads
        ):
            raise self._fail(ConnectorFailure.invalid_response())
        self._uploads_playlist_cache[channel_id] = uploads
        title = snippet.get("title")
        return ConnectorAccount(
            platform=Platform.YOUTUBE,
            external_id=channel_id,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=title if isinstance(title, str) and title.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        playlist_id = await self._uploads_playlist(account.external_id)
        params: dict[str, object] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": _MAX_PLAYLIST_PAGE_SIZE,
        }
        if cursor is not None:
            params["pageToken"] = cursor
        payload = await self._get(PLAYLIST_ITEMS_URL, params)
        entries = _list(payload.get("items")) or []
        video_ids: list[str] = []
        for entry in entries:
            entry_mapping = _mapping(entry)
            content_details = (
                _mapping(entry_mapping.get("contentDetails")) if entry_mapping else None
            )
            video_id = content_details.get("videoId") if content_details else None
            if isinstance(video_id, str) and video_id:
                video_ids.append(video_id)
        videos = await self._videos_by_id(video_ids)
        items = tuple(self._map_video(video) for video in videos)
        next_cursor = payload.get("nextPageToken")
        return ConnectorPage(
            items=items,
            next_cursor=next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        )

    async def ingest_push(self, event: YouTubePushEvent) -> Mapping[str, JSONValue]:
        """Turn one verified push identity into a content-ledger item envelope.

        Never trusts the push payload's own claim about title or lifecycle state — the
        video is re-fetched through `videos.list` unless the push itself was a
        tombstone (`event.deleted`), in which case no video can be fetched at all.
        """
        if event.deleted:
            return _retracted_item(event.video_id)
        videos = await self._videos_by_id([event.video_id])
        if not videos:
            return _unavailable_item(event.video_id)
        return self._map_video(videos[0])

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    async def _uploads_playlist(self, channel_id: str) -> str:
        cached = self._uploads_playlist_cache.get(channel_id)
        if cached is not None:
            return cached
        payload = await self._get(CHANNELS_URL, {"part": "contentDetails", "id": channel_id})
        items = _list(payload.get("items")) or []
        channel = _mapping(items[0]) if items else None
        content_details = _mapping(channel.get("contentDetails")) if channel else None
        related = _mapping(content_details.get("relatedPlaylists")) if content_details else None
        uploads = related.get("uploads") if related else None
        if not isinstance(uploads, str) or not uploads:
            raise self._fail(ConnectorFailure.not_found("channel not found"))
        self._uploads_playlist_cache[channel_id] = uploads
        return uploads

    async def _videos_by_id(self, video_ids: Sequence[str]) -> list[Mapping[str, object]]:
        if not video_ids:
            return []
        collected: list[Mapping[str, object]] = []
        for start in range(0, len(video_ids), _MAX_VIDEOS_PER_CALL):
            batch = video_ids[start : start + _MAX_VIDEOS_PER_CALL]
            payload = await self._get(
                VIDEOS_URL,
                {
                    "part": "snippet,contentDetails,liveStreamingDetails,status",
                    "id": ",".join(batch),
                },
            )
            for item in _list(payload.get("items")) or []:
                mapped = _mapping(item)
                if mapped is not None:
                    collected.append(mapped)
        return collected

    def _map_video(self, video: Mapping[str, object]) -> Mapping[str, JSONValue]:
        video_id = video.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise self._fail(ConnectorFailure.invalid_response())
        snippet = _mapping(video.get("snippet")) or {}
        status = _mapping(video.get("status")) or {}
        live_details = _mapping(video.get("liveStreamingDetails"))
        content_details = _mapping(video.get("contentDetails")) or {}

        broadcast = snippet.get("liveBroadcastContent")
        privacy = status.get("privacyStatus")
        upload_status = status.get("uploadStatus")

        if broadcast == "upcoming":
            kind, state = ContentKind.LIVE, ContentState.SCHEDULED
        elif broadcast == "live":
            kind, state = ContentKind.LIVE, ContentState.LIVE
        elif live_details is not None and live_details.get("actualEndTime"):
            kind, state = ContentKind.LIVE, ContentState.ENDED
        elif privacy == "private" or upload_status in {"rejected", "failed"}:
            kind = ContentKind.LIVE if live_details is not None else ContentKind.VIDEO
            state = ContentState.FAILED
        else:
            duration_seconds = _parse_iso8601_duration(content_details.get("duration"))
            kind = (
                ContentKind.SHORT
                if duration_seconds is not None and duration_seconds <= _SHORT_MAX_SECONDS
                else ContentKind.VIDEO
            )
            state = ContentState.PUBLISHED

        title = snippet.get("title")
        published_raw = (
            (live_details.get("scheduledStartTime") if live_details else None)
            or (live_details.get("actualStartTime") if live_details else None)
            or snippet.get("publishedAt")
        )
        item: dict[str, JSONValue] = {
            "external_id": video_id,
            "kind": kind.value,
            "state": state.value,
            "canonical_url": _canonical_watch_url(video_id),
        }
        if isinstance(title, str) and title.strip():
            item["title"] = title[:_MAX_TITLE_LENGTH]
        if isinstance(published_raw, str) and published_raw:
            item["published_at"] = published_raw
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        request_params = {key: str(value) for key, value in params.items() if value is not None}
        request_params["key"] = self._api_key
        try:
            async with self._session.get(url, params=request_params) as response:
                try:
                    payload = await response.json()
                except (
                    aiohttp.ClientPayloadError,
                    aiohttp.ContentTypeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise self._fail(ConnectorFailure.invalid_response()) from exc
                status = response.status
                response_headers = getattr(response, "headers", None)
        except TimeoutError as exc:
            raise self._fail(ConnectorFailure.timeout()) from exc
        except aiohttp.ClientError as exc:
            raise self._fail(ConnectorFailure.unavailable()) from exc

        mapped_payload = _mapping(payload)
        if status == 200 and mapped_payload is not None:
            self._last_failure = None
            return mapped_payload
        reason = _error_reason(mapped_payload)
        if status == 403 and reason in {"quotaExceeded", "dailyLimitExceeded"}:
            raise self._fail(
                ConnectorFailure.quota_exceeded(),
                retry_after_seconds=_quota_reset_delay_seconds(self._now()),
            )
        if status in {401, 403}:
            raise self._fail(ConnectorFailure.authorization())
        if status == 404:
            raise self._fail(ConnectorFailure.not_found())
        if status == 429:
            raise self._fail(
                ConnectorFailure.rate_limited(),
                retry_after_seconds=_retry_after_seconds_from_headers(response_headers),
            )
        raise self._fail(ConnectorFailure.invalid_response())

    def _fail(
        self, failure: ConnectorFailure, *, retry_after_seconds: float | None = None
    ) -> YouTubeConnectorError:
        self._last_failure = failure
        return YouTubeConnectorError(failure, retry_after_seconds=retry_after_seconds)
