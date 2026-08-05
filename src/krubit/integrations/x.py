"""X (Twitter) API v2 connector: static bearer-token auth and `since_id` polling.

Mirrors the shape of `krubit.integrations.youtube.YouTubeConnector` for HTTP client
structure (an injected `aiohttp`-like session, `ConnectorFailure`-based error
taxonomy) but is adapted to X's simpler model: there is no OAuth refresh flow (a
single static app-only bearer token authenticates every request), and pagination is
a durable `since_id` watermark rather than an opaque page token.

`resolve_account` turns a handle into X's stable numeric user id via
`GET /2/users/by/username/{username}`. `fetch_page` polls
`GET /2/users/{id}/tweets` with `since_id=cursor` (omitted on the first poll) and
`exclude=replies,retweets` so the API itself does most of the filtering. The
`exclude` parameter only recognizes `replies` and `retweets` — it does not cover
quote-tweets ("retweet with comment"), which the API still returns as ordinary
tweets. The response is therefore still walked defensively and any tweet carrying a
`referenced_tweets` entry of type `replied_to`, `retweeted`, or `quoted` is dropped,
so a reply, repost, or quote-tweet can never reach the content ledger even if the
server-side `exclude` ever changes behavior — quote-tweets are exactly the "ordinary
share" pattern the platform vocabulary excludes by default alongside replies and
reposts. `next_cursor` is the API's own `meta.newest_id` for the fetched
(unfiltered) page; if the API reports no new tweets at all, the caller's own cursor
is preserved so the watermark never regresses.
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

USERS_BY_USERNAME_URL = "https://api.x.com/2/users/by/username/{username}"
USER_TWEETS_URL = "https://api.x.com/2/users/{id}/tweets"

_MAX_PAGE_SIZE = 100
_MAX_TEXT_LENGTH = 300
_REPLY_OR_REPOST_TYPES = frozenset({"replied_to", "retweeted", "quoted"})

_HEALTH_STATE_BY_FAILURE: Mapping[ConnectorFailureKind, CapabilityState] = {
    ConnectorFailureKind.AUTHORIZATION: CapabilityState.AUTHORIZATION_REQUIRED,
    ConnectorFailureKind.RATE_LIMITED: CapabilityState.DEGRADED,
    ConnectorFailureKind.QUOTA_EXCEEDED: CapabilityState.QUOTA_LIMITED,
    ConnectorFailureKind.UNAVAILABLE: CapabilityState.DEGRADED,
    ConnectorFailureKind.INVALID_RESPONSE: CapabilityState.DEGRADED,
    ConnectorFailureKind.TIMEOUT: CapabilityState.DEGRADED,
    ConnectorFailureKind.NOT_FOUND: CapabilityState.DEGRADED,
}


class XConnectorError(RuntimeError):
    """Raised by `XConnector` when a request cannot be completed.

    Carries the classified `ConnectorFailure` a caller renders safely; this
    exception's own message is the fixed `kind` name, never provider response text.
    `retry_after_seconds`, when known, is the real `x-rate-limit-reset` (or
    `Retry-After`) resume delay a 429 response reported — see
    `_retry_after_seconds_from_headers` — so a scheduler never re-polls before X's own
    rate limit window has actually elapsed.
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


def _is_reply_repost_or_quote(tweet: Mapping[str, object]) -> bool:
    referenced = _list(tweet.get("referenced_tweets"))
    if not referenced:
        return False
    for entry in referenced:
        mapped = _mapping(entry)
        if mapped is not None and mapped.get("type") in _REPLY_OR_REPOST_TYPES:
            return True
    return False


def _retry_after_seconds_from_headers(headers: object, *, now: datetime) -> float | None:
    """Extract a numeric resume delay from X's 429 rate-limit response headers.

    X sets `x-rate-limit-reset` to a Unix epoch second on a 429; a plain `Retry-After`
    (seconds) is checked as a fallback for any gateway/proxy in front of the API that
    might set it instead. Returns `None` if neither header is present or parseable
    (or resolves to a non-positive delay), letting the scheduler's own exponential
    backoff apply instead.
    """
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    reset = getter("x-rate-limit-reset") or getter("X-Rate-Limit-Reset")
    if reset is not None:
        try:
            delay = float(str(reset)) - now.timestamp()
        except (TypeError, ValueError):
            delay = None
        if delay is not None and delay > 0:
            return delay
    retry_after = getter("Retry-After") or getter("retry-after")
    if retry_after is not None:
        try:
            delay = float(str(retry_after))
        except (TypeError, ValueError):
            return None
        if delay > 0:
            return delay
    return None


def _canonical_status_url(handle: str, tweet_id: str) -> str:
    return f"https://x.com/{handle}/status/{tweet_id}"


class XConnector:
    """Static bearer-token X API v2 connector.

    Satisfies `krubit.integrations.base.Connector` structurally. `resolve_account`
    and `fetch_page` raise `XConnectorError` on failure (there is no result-union
    return type in the shared protocol); `health` never raises and instead reports
    the connector's last observed `ConnectorFailure`, if any, as an honest
    `CapabilityState`.
    """

    descriptor = CATALOG[Platform.X]

    def __init__(
        self,
        session: object,
        bearer_token: str,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._bearer_token = bearer_token
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(
            USERS_BY_USERNAME_URL.format(username=recognized.handle),
            {"user.fields": "id,username,name"},
        )
        data = _mapping(payload.get("data"))
        if data is None:
            raise self._fail(ConnectorFailure.not_found("user not found"))
        user_id = data.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise self._fail(ConnectorFailure.invalid_response())
        name = data.get("name")
        return ConnectorAccount(
            platform=Platform.X,
            external_id=user_id,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=name if isinstance(name, str) and name.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        params: dict[str, object] = {
            "max_results": _MAX_PAGE_SIZE,
            "exclude": "replies,retweets",
            "tweet.fields": "created_at,referenced_tweets",
        }
        if cursor is not None:
            params["since_id"] = cursor
        payload = await self._get(USER_TWEETS_URL.format(id=account.external_id), params)

        tweets = _list(payload.get("data")) or []
        items: list[Mapping[str, JSONValue]] = []
        for entry in tweets:
            tweet = _mapping(entry)
            if tweet is None or _is_reply_repost_or_quote(tweet):
                continue
            mapped = self._map_tweet(tweet, account.handle)
            if mapped is not None:
                items.append(mapped)

        meta = _mapping(payload.get("meta")) or {}
        newest_id = meta.get("newest_id")
        next_cursor = newest_id if isinstance(newest_id, str) and newest_id else cursor
        return ConnectorPage(items=tuple(items), next_cursor=next_cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_tweet(
        self, tweet: Mapping[str, object], handle: str
    ) -> Mapping[str, JSONValue] | None:
        tweet_id = tweet.get("id")
        if not isinstance(tweet_id, str) or not tweet_id:
            return None
        item: dict[str, JSONValue] = {
            "external_id": tweet_id,
            "kind": ContentKind.POST.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_status_url(handle, tweet_id),
        }
        text = tweet.get("text")
        if isinstance(text, str) and text.strip():
            item["text"] = text[:_MAX_TEXT_LENGTH]
        created_at = tweet.get("created_at")
        if isinstance(created_at, str) and created_at:
            item["published_at"] = created_at
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        request_params = {key: str(value) for key, value in params.items() if value is not None}
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        try:
            async with self._session.get(url, params=request_params, headers=headers) as response:
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
        if status in {401, 403}:
            raise self._fail(ConnectorFailure.authorization())
        if status == 404:
            raise self._fail(ConnectorFailure.not_found())
        if status == 429:
            raise self._fail(
                ConnectorFailure.rate_limited(),
                retry_after_seconds=_retry_after_seconds_from_headers(
                    response_headers, now=self._now()
                ),
            )
        raise self._fail(ConnectorFailure.invalid_response())

    def _fail(
        self, failure: ConnectorFailure, *, retry_after_seconds: float | None = None
    ) -> XConnectorError:
        self._last_failure = failure
        return XConnectorError(failure, retry_after_seconds=retry_after_seconds)
