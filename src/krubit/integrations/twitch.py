"""Twitch Helix lookups backed by an in-memory app-token lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import aiohttp

from krubit.domain.live_signals import TwitchLookup, TwitchLookupKind, TwitchStream

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

_REFRESH_WINDOW = timedelta(seconds=60)
_VALIDATE_INTERVAL = timedelta(hours=1)


class TwitchClient(Protocol):
    """Look up a Twitch channel's current live-stream state."""

    async def get_stream(self, login: str) -> TwitchLookup: ...


class _Response(Protocol):
    status: int
    headers: Mapping[str, str]

    async def json(self) -> object: ...


class _RequestContext(Protocol):
    async def __aenter__(self) -> _Response: ...

    async def __aexit__(self, *args: object) -> None: ...


class _Session(Protocol):
    def request(
        self, method: str, url: str, **kwargs: object
    ) -> _RequestContext: ...


@dataclass(frozen=True, slots=True)
class _HttpResult:
    status: int | None
    payload: object | None
    headers: Mapping[str, str]
    failure: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TwitchHelixClient:
    """Fetch Helix stream data using client-credentials app authentication."""

    def __init__(
        self,
        session: object,
        client_id: str,
        client_secret: str,
        *,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = cast(_Session, session)
        self._client_id = client_id
        self._client_secret = client_secret
        self._now = now
        self._sleep = sleep
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._validated_at: datetime | None = None

    async def get_stream(self, login: str) -> TwitchLookup:
        """Return the current stream state without exposing provider details."""
        preparation_failure = await self._prepare_token()
        if preparation_failure is not None:
            return self._unavailable(preparation_failure)

        result = await self._get_stream_response(login)
        if result.status == 401:
            self._clear_token()
            preparation_failure = await self._prepare_token()
            if preparation_failure is not None:
                return self._unavailable(preparation_failure)
            result = await self._get_stream_response(login)
            if result.status == 401:
                return self._unavailable("token_rejected")

        return self._map_stream_result(result)

    async def _prepare_token(self) -> str | None:
        now = self._now()
        if self._token_needs_refresh(now):
            failure = await self._acquire_token(now)
            if failure is not None:
                return failure
        if self._token_needs_validation(now):
            failure = await self._validate_token(now)
            if failure is not None:
                return failure
        return None

    def _token_needs_refresh(self, now: datetime) -> bool:
        return (
            self._access_token is None
            or self._expires_at is None
            or now >= self._expires_at - _REFRESH_WINDOW
        )

    def _token_needs_validation(self, now: datetime) -> bool:
        return self._validated_at is not None and now - self._validated_at >= _VALIDATE_INTERVAL

    async def _acquire_token(self, now: datetime) -> str | None:
        result = await self._request_json(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
        )
        if result.failure is not None:
            return result.failure
        if result.status == 401:
            return "token_rejected"
        if result.status == 429:
            return "rate_limited"
        payload = _string_mapping(result.payload)
        if result.status != 200 or payload is None:
            return "invalid_response"

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int | float)
            or expires_in <= _REFRESH_WINDOW.total_seconds()
        ):
            return "invalid_response"

        self._access_token = token
        self._expires_at = now + timedelta(seconds=expires_in)
        self._validated_at = now
        return None

    async def _validate_token(self, now: datetime) -> str | None:
        token = self._access_token
        if token is None:
            return "token_rejected"
        result = await self._request_json(
            "GET", VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}
        )
        if result.failure is not None:
            return result.failure
        if result.status == 401:
            self._clear_token()
            return await self._acquire_token(now)
        if result.status == 429:
            return "rate_limited"
        if result.status != 200:
            return "invalid_response"
        self._validated_at = now
        return None

    async def _get_stream_response(self, login: str) -> _HttpResult:
        token = self._access_token
        if token is None:
            return _HttpResult(None, None, {}, "token_rejected")

        result = await self._request_json(
            "GET",
            STREAMS_URL,
            params={"user_login": login},
            headers={"Client-Id": self._client_id, "Authorization": f"Bearer {token}"},
        )
        if result.status != 429:
            return result

        reset_value = result.headers.get("Ratelimit-Reset")
        try:
            delay = max(0.0, float(int(reset_value or "")) - self._now().timestamp())
        except ValueError:
            return _HttpResult(None, None, {}, "rate_limited")
        try:
            await self._sleep(delay)
        except TimeoutError:
            return _HttpResult(None, None, {}, "timeout")
        return await self._request_json(
            "GET",
            STREAMS_URL,
            params={"user_login": login},
            headers={"Client-Id": self._client_id, "Authorization": f"Bearer {token}"},
        )

    async def _request_json(self, method: str, url: str, **kwargs: object) -> _HttpResult:
        try:
            async with self._session.request(method, url, **kwargs) as response:
                try:
                    payload = await response.json()
                except (TypeError, ValueError):
                    return _HttpResult(response.status, None, response.headers, "invalid_response")
                return _HttpResult(response.status, payload, response.headers)
        except TimeoutError:
            return _HttpResult(None, None, {}, "timeout")
        except aiohttp.ClientError:
            return _HttpResult(None, None, {}, "unavailable")
        except Exception:
            return _HttpResult(None, None, {}, "unavailable")

    def _map_stream_result(self, result: _HttpResult) -> TwitchLookup:
        if result.failure is not None:
            return self._unavailable(result.failure)
        if result.status == 401:
            return self._unavailable("token_rejected")
        if result.status == 429:
            return self._unavailable("rate_limited")
        payload = _string_mapping(result.payload)
        if result.status != 200 or payload is None:
            return self._unavailable("invalid_response")

        data = _object_list(payload.get("data"))
        if data is None:
            return self._unavailable("invalid_response")
        if not data:
            return TwitchLookup(TwitchLookupKind.OFFLINE)
        stream_payload = _string_mapping(data[0]) if len(data) == 1 else None
        if stream_payload is None:
            return self._unavailable("invalid_response")

        try:
            stream = TwitchStream(
                stream_id=self._required_text(stream_payload, "id"),
                user_login=self._required_text(stream_payload, "user_login"),
                user_name=self._required_text(stream_payload, "user_name"),
                title=self._required_text(stream_payload, "title"),
                game_name=self._required_text(stream_payload, "game_name"),
                started_at=self._started_at(stream_payload),
                thumbnail_url=self._thumbnail_url(stream_payload),
            )
        except (TypeError, ValueError):
            return self._unavailable("invalid_response")
        return TwitchLookup(TwitchLookupKind.LIVE, stream=stream)

    @staticmethod
    def _required_text(payload: Mapping[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        return value

    @classmethod
    def _started_at(cls, payload: Mapping[str, object]) -> datetime:
        value = cls._required_text(payload, "started_at")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("started_at must include a timezone")
        return parsed

    @classmethod
    def _thumbnail_url(cls, payload: Mapping[str, object]) -> str:
        return cls._required_text(payload, "thumbnail_url").replace("{width}", "640").replace(
            "{height}", "360"
        )

    def _clear_token(self) -> None:
        self._access_token = None
        self._expires_at = None
        self._validated_at = None

    @staticmethod
    def _unavailable(reason: str) -> TwitchLookup:
        return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason=reason)


def _string_mapping(value: object | None) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw_mapping = cast(Mapping[object, object], value)
    mapped: dict[str, object] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            return None
        mapped[key] = item
    return mapped


def _object_list(value: object | None) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return list(cast(list[object], value))
