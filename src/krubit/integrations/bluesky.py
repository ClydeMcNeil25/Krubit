"""Bluesky AT Protocol connector: public reads only, no member OAuth required.

Mirrors the shape of `krubit.integrations.youtube.YouTubeConnector` for HTTP client
structure (an injected `aiohttp`-like session, `ConnectorFailure`-based error
taxonomy) but is adapted to Bluesky's public-read AppView API served from
`public.api.bsky.app`, which needs no member authorization at all:

- `com.atproto.identity.resolveHandle` resolves a handle (for example
  `creator.bsky.social`) to its stable DID.
- `app.bsky.feed.getAuthorFeed` reads that DID's public post feed, newest first.
  The feed endpoint has no `since`-style cursor for incremental polling (its
  `cursor` walks toward older pages instead), so `fetch_page` always reads from the
  top and locally stops as soon as it reaches the previously recorded watermark: the
  URI of the newest post seen on the prior poll. `next_cursor` becomes the URI of the
  newest post in the freshly fetched page (or the prior cursor, unchanged, if nothing
  new was found), so the watermark never regresses.

Replies and reposts are excluded by default: a reply is any post whose own record
carries a `reply` field; a repost is any feed entry carrying a top-level `reason` of
type `app.bsky.feed.defs#reasonRepost` (Bluesky represents "this author reposted
someone else's post" as a wrapper around the original post, not as a new post of the
author's own).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

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

RESOLVE_HANDLE_URL = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
GET_AUTHOR_FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

_MAX_PAGE_SIZE = 50
_MAX_TEXT_LENGTH = 300
_REPOST_REASON_TYPE = "app.bsky.feed.defs#reasonRepost"

_HEALTH_STATE_BY_FAILURE: Mapping[ConnectorFailureKind, CapabilityState] = {
    ConnectorFailureKind.AUTHORIZATION: CapabilityState.AUTHORIZATION_REQUIRED,
    ConnectorFailureKind.RATE_LIMITED: CapabilityState.DEGRADED,
    ConnectorFailureKind.QUOTA_EXCEEDED: CapabilityState.QUOTA_LIMITED,
    ConnectorFailureKind.UNAVAILABLE: CapabilityState.DEGRADED,
    ConnectorFailureKind.INVALID_RESPONSE: CapabilityState.DEGRADED,
    ConnectorFailureKind.TIMEOUT: CapabilityState.DEGRADED,
    ConnectorFailureKind.NOT_FOUND: CapabilityState.DEGRADED,
}


class BlueskyConnectorError(RuntimeError):
    """Raised by `BlueskyConnector` when a request cannot be completed.

    Carries the classified `ConnectorFailure` a caller renders safely; this
    exception's own message is the fixed `kind` name, never provider response text.
    """

    def __init__(self, failure: ConnectorFailure) -> None:
        super().__init__(failure.kind.value)
        self.failure = failure


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


def _is_repost(entry: Mapping[str, object]) -> bool:
    reason = _mapping(entry.get("reason"))
    return reason is not None and reason.get("$type") == _REPOST_REASON_TYPE


def _is_reply(post: Mapping[str, object]) -> bool:
    record = _mapping(post.get("record"))
    return record is not None and record.get("reply") is not None


def _rkey(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _canonical_post_url(handle: str, uri: str) -> str:
    return f"https://bsky.app/profile/{handle}/post/{_rkey(uri)}"


class BlueskyConnector:
    """Public-read Bluesky AT Protocol connector; no member OAuth required.

    Satisfies `krubit.integrations.base.Connector` structurally. `resolve_account`
    and `fetch_page` raise `BlueskyConnectorError` on failure (there is no
    result-union return type in the shared protocol); `health` never raises and
    instead reports the connector's last observed `ConnectorFailure`, if any, as an
    honest `CapabilityState`.
    """

    descriptor = CATALOG[Platform.BLUESKY]

    def __init__(
        self,
        session: object,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(RESOLVE_HANDLE_URL, {"handle": recognized.handle})
        did = payload.get("did")
        if not isinstance(did, str) or not did:
            raise self._fail(ConnectorFailure.invalid_response())
        return ConnectorAccount(
            platform=Platform.BLUESKY,
            external_id=did,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        payload = await self._get(
            GET_AUTHOR_FEED_URL, {"actor": account.external_id, "limit": _MAX_PAGE_SIZE}
        )
        feed = _list(payload.get("feed")) or []

        items: list[Mapping[str, JSONValue]] = []
        newest_uri: str | None = None
        for raw_entry in feed:
            entry = _mapping(raw_entry)
            post = _mapping(entry.get("post")) if entry is not None else None
            if entry is None or post is None:
                continue
            uri = post.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            if newest_uri is None:
                newest_uri = uri
            if cursor is not None and uri == cursor:
                # Reached the previously recorded watermark; every remaining entry
                # in this (newest-first) page was already surfaced on a prior poll.
                break
            if _is_repost(entry) or _is_reply(post):
                continue
            mapped = self._map_post(post, account.handle)
            if mapped is not None:
                items.append(mapped)

        next_cursor = newest_uri if newest_uri is not None else cursor
        return ConnectorPage(items=tuple(items), next_cursor=next_cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_post(
        self, post: Mapping[str, object], handle: str
    ) -> Mapping[str, JSONValue] | None:
        uri = post.get("uri")
        if not isinstance(uri, str) or not uri:
            return None
        item: dict[str, JSONValue] = {
            "external_id": uri,
            "kind": ContentKind.POST.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_post_url(handle, uri),
        }
        record = _mapping(post.get("record")) or {}
        text = record.get("text")
        if isinstance(text, str) and text.strip():
            item["text"] = text[:_MAX_TEXT_LENGTH]
        created_at = record.get("createdAt")
        if isinstance(created_at, str) and created_at:
            item["published_at"] = created_at
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        request_params = {key: str(value) for key, value in params.items() if value is not None}
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
        except TimeoutError as exc:
            raise self._fail(ConnectorFailure.timeout()) from exc
        except aiohttp.ClientError as exc:
            raise self._fail(ConnectorFailure.unavailable()) from exc

        mapped_payload = _mapping(payload)
        if status == 200 and mapped_payload is not None:
            self._last_failure = None
            return mapped_payload
        error = mapped_payload.get("error") if mapped_payload is not None else None
        if status == 400 and error == "InvalidRequest":
            raise self._fail(ConnectorFailure.not_found())
        if status in {401, 403}:
            raise self._fail(ConnectorFailure.authorization())
        if status == 404:
            raise self._fail(ConnectorFailure.not_found())
        if status == 429:
            raise self._fail(ConnectorFailure.rate_limited())
        raise self._fail(ConnectorFailure.invalid_response())

    def _fail(self, failure: ConnectorFailure) -> BlueskyConnectorError:
        self._last_failure = failure
        return BlueskyConnectorError(failure)
