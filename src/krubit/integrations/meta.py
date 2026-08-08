"""Meta Graph API connectors for Instagram, Facebook, and Threads, plus the
signed/expiring/single-use OAuth state that guards every member-facing Meta
authorization Krubit performs.

Unlike Twitch/YouTube/X/Bluesky (public reads or a single static app credential),
every connector here reads content that only exists behind a *member's own* OAuth
grant: Instagram and Threads always via the authorizing member's own account,
Facebook either via a Page the member manages or (heavily gated) the member's own
personal profile. `MetaOAuthStates` is the CSRF defense for that authorization flow:
`issue` mints a signed token binding one authorization attempt to exactly one
(guild, member, platform) triple with a short expiry; `consume` verifies the
signature, expiry, and guild/member binding, and can never succeed twice for the
same token. Every rejection reason collapses to the same generic message so the
mechanism itself gives an attacker no oracle to probe.

Each connector mirrors the HTTP-client shape of `krubit.integrations.x.XConnector`
(an injected `aiohttp`-like session, `ConnectorFailure`-based error taxonomy) but is
adapted to the Graph API's error envelope (`{"error": {"code": ..., ...}}`) and to
each platform's content shape:

- `InstagramConnector` reads `/{ig-user-id}/media` and classifies each item by its
  `media_product_type` (`REELS` -> Reel, `LIVE` -> live media, anything else ->
  ordinary post).
- `FacebookPageConnector` reads a Page's `/posts`, `/videos`, and `/live_videos`
  edges and classifies videos by an `is_reels_video` flag and live broadcasts by
  their `status` field (`LIVE`, `SCHEDULED_*`, `LIVE_STOPPED`/`VOD`,
  `SCHEDULED_CANCELED`) — the same kind of "no official flag, so classify from the
  closest real signal" approximation `YouTubeConnector` already makes for Shorts.
  Each of the three edges is bounded and re-polled on every page rather than
  cursor-chained (`next_cursor` is always `None`): re-observing an already-ingested
  item is a safe no-op for `ContentSignalService`, so this trades a small amount of
  redundant polling for not having to thread three independent Graph cursors through
  one connector's single opaque cursor string.
- `FacebookProfileConnector` never calls the Graph API at all unless it was
  constructed with `app_review_granted=True`: personal-profile content is
  `CapabilityState.APPROVAL_REQUIRED` by construction, matching the platform
  capability matrix's static declaration for `Platform.FACEBOOK`, and `fetch_page`
  raises an authorization failure rather than attempting a call no token could
  satisfy anyway.
- `ThreadsConnector` reads `/{threads-user-id}/threads` and excludes anything that
  is a quote post (`is_quote_post`) or a reply (a truthy `root_post`), surfacing only
  the member's own original posts.

Every connector resolves its own account via `/me` (Instagram/Facebook) or
`/me` on the Threads Graph host, never an arbitrary handle lookup: the whole point of
OAuth-gated Meta access is that Krubit only ever reads the account that authorized
it, not an arbitrary public profile.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast
from uuid import uuid4

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
from krubit.security.credential_vault import CredentialVault, CredentialVaultError
from krubit.web.callbacks import (
    CallbackRoute,
    OAuthRedirect,
    SignedWebhook,
    build_oauth_redirect_route,
    build_signed_webhook_route,
)

GRAPH_BASE = "https://graph.facebook.com/v19.0"
THREADS_BASE = "https://graph.threads.net/v1.0"

_MAX_PAGE_SIZE = 50
_MAX_TEXT_LENGTH = 300
_DEFAULT_STATE_TTL = timedelta(minutes=10)
_STATE_USED_OR_EXPIRED = "OAuth state was used or expired"

# Graph API error `code` values that mean the token itself is unusable (expired,
# revoked, malformed) or that the token's scopes do not cover the requested read —
# both are treated as the same `ConnectorFailureKind.AUTHORIZATION` outcome a
# refreshed/re-granted authorization is required to clear.
_GRAPH_AUTH_ERROR_CODES = frozenset({190, 200, 10, 299})
_GRAPH_RATE_LIMIT_ERROR_CODES = frozenset({4, 17, 32, 613})

_LIVE_VIDEO_LIVE_STATUSES = frozenset({"LIVE"})
_LIVE_VIDEO_SCHEDULED_STATUSES = frozenset({"SCHEDULED_LIVE", "SCHEDULED_UNPUBLISHED"})
_LIVE_VIDEO_ENDED_STATUSES = frozenset({"LIVE_STOPPED", "VOD"})
_LIVE_VIDEO_CANCELLED_STATUSES = frozenset({"SCHEDULED_CANCELED"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------
# OAuth state: signed, expiring, single-use, bound to (guild_id, member_id, platform)
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StatePayload:
    guild_id: int
    member_id: int
    platform: Platform
    expires_at: datetime
    nonce: str


class MetaOAuthStates:
    """Signed, expiring, single-use OAuth state tokens bound to guild/member/platform.

    `issue` mints an HMAC-SHA256-signed token embedding the (guild, member, platform)
    triple, an expiry, and a random nonce. `consume` verifies the signature, checks
    the token has not expired, checks the caller-supplied `guild_id`/`member_id`
    match what was issued, and checks the nonce has not already been consumed —
    marking it consumed on success so a second `consume` call for the same token
    always fails, even before expiry. Every failure path (bad signature, expiry,
    guild/member mismatch, or reuse) raises the same generic
    `ValueError("OAuth state was used or expired")`: distinguishing *why* a state was
    rejected would let an attacker probe the CSRF-prevention mechanism itself, so
    this is deliberately one uninformative outcome.

    Consumed nonces are tracked in memory only; a process restart between issue and
    consume drops that tracking, which only ever *widens* what a stale state can no
    longer do (an expired-by-restart token still fails on the expiry or signature
    check for any state issued before restart with a different in-process key), never
    narrows it.
    """

    def __init__(
        self,
        signing_key: str,
        *,
        now: Callable[[], datetime] = _utc_now,
        ttl: timedelta = _DEFAULT_STATE_TTL,
    ) -> None:
        if not signing_key.strip():
            raise ValueError("signing_key must not be blank")
        self._signing_key = signing_key.encode("utf-8")
        self._now = now
        self._ttl = ttl
        self._consumed_nonces: set[str] = set()

    def issue(self, *, guild_id: int, member_id: int, platform: Platform) -> str:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if member_id <= 0:
            raise ValueError("member_id must be positive")
        if type(platform) is not Platform:
            raise ValueError("platform must be a Platform")
        nonce = uuid4().hex
        expires_at = self._now() + self._ttl
        payload = "|".join(
            (
                str(guild_id),
                str(member_id),
                platform.value,
                str(int(expires_at.timestamp())),
                nonce,
            )
        )
        signature = hmac.new(self._signing_key, payload.encode("utf-8"), sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
        return f"{token}.{signature}"

    def consume(self, state: str, *, guild_id: int, member_id: int) -> Platform:
        parsed = self._parse_and_verify(state)
        if (
            parsed is None
            or parsed.nonce in self._consumed_nonces
            or self._now() >= parsed.expires_at
            or parsed.guild_id != guild_id
            or parsed.member_id != member_id
        ):
            raise ValueError(_STATE_USED_OR_EXPIRED)
        self._consumed_nonces.add(parsed.nonce)
        return parsed.platform

    def _parse_and_verify(self, state: str) -> _StatePayload | None:
        token, separator, signature = state.partition(".")
        if not separator or not token or not signature:
            return None
        try:
            payload = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        expected_signature = hmac.new(
            self._signing_key, payload.encode("utf-8"), sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, signature):
            return None
        parts = payload.split("|")
        if len(parts) != 5:
            return None
        raw_guild_id, raw_member_id, raw_platform, raw_expires_at, nonce = parts
        try:
            guild_id = int(raw_guild_id)
            member_id = int(raw_member_id)
            expires_at = datetime.fromtimestamp(int(raw_expires_at), tz=UTC)
            platform = Platform(raw_platform)
        except ValueError:
            return None
        if not nonce:
            return None
        return _StatePayload(
            guild_id=guild_id,
            member_id=member_id,
            platform=platform,
            expires_at=expires_at,
            nonce=nonce,
        )


# --------------------------------------------------------------------------------
# OAuth grant sealing: a connector never stores or logs an unsealed token
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaOAuthGrant:
    """An unsealed OAuth grant, held only transiently between exchange and sealing.

    `access_token` and `refresh_token` are marked `repr=False`, matching
    `krubit.config.Settings`'s existing convention for every secret-bearing field:
    `repr(grant)`/`str(grant)` (and anything that stringifies this object — a debug
    log, an uncaught-exception frame dump) must never render a plaintext token.
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("access_token must not be blank")


def seal_oauth_grant(vault: CredentialVault, grant: MetaOAuthGrant) -> str:
    """Seal an OAuth grant for storage; the caller never persists `grant` itself."""
    payload: dict[str, JSONValue] = {"access_token": grant.access_token}
    if grant.refresh_token is not None:
        payload["refresh_token"] = grant.refresh_token
    if grant.expires_at is not None:
        payload["expires_at"] = grant.expires_at.isoformat()
    return vault.seal_json(payload)


def open_oauth_grant(vault: CredentialVault, sealed: str) -> MetaOAuthGrant:
    """Unseal a token produced by `seal_oauth_grant`, just before a connector uses it."""
    payload = vault.open_json(sealed)
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CredentialVaultError("sealed OAuth grant is missing access_token")
    refresh_token = payload.get("refresh_token")
    raw_expires_at = payload.get("expires_at")
    expires_at = datetime.fromisoformat(raw_expires_at) if isinstance(raw_expires_at, str) else None
    return MetaOAuthGrant(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------------
# Shared Graph API request/error-mapping plumbing
# --------------------------------------------------------------------------------


class MetaConnectorError(RuntimeError):
    """Raised by any Meta connector when a request cannot be completed.

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

    def post(self, url: str, **kwargs: object) -> _RequestContext: ...


_HEALTH_STATE_BY_FAILURE: Mapping[ConnectorFailureKind, CapabilityState] = {
    ConnectorFailureKind.AUTHORIZATION: CapabilityState.AUTHORIZATION_REQUIRED,
    ConnectorFailureKind.RATE_LIMITED: CapabilityState.DEGRADED,
    ConnectorFailureKind.QUOTA_EXCEEDED: CapabilityState.QUOTA_LIMITED,
    ConnectorFailureKind.UNAVAILABLE: CapabilityState.DEGRADED,
    ConnectorFailureKind.INVALID_RESPONSE: CapabilityState.DEGRADED,
    ConnectorFailureKind.TIMEOUT: CapabilityState.DEGRADED,
    ConnectorFailureKind.NOT_FOUND: CapabilityState.DEGRADED,
}


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


def _graph_error_code(payload: Mapping[str, object] | None) -> int | None:
    if payload is None:
        return None
    error = _mapping(payload.get("error"))
    if error is None:
        return None
    code = error.get("code")
    return code if isinstance(code, int) else None


async def _graph_get(
    session: _Session,
    url: str,
    params: Mapping[str, object],
    access_token: str,
    *,
    fail: Callable[[ConnectorFailure], MetaConnectorError],
) -> Mapping[str, object]:
    request_params = {key: str(value) for key, value in params.items() if value is not None}
    request_params["access_token"] = access_token
    try:
        async with session.get(url, params=request_params) as response:
            try:
                payload = await response.json()
            except (
                aiohttp.ClientPayloadError,
                aiohttp.ContentTypeError,
                TypeError,
                ValueError,
            ) as exc:
                raise fail(ConnectorFailure.invalid_response()) from exc
            status = response.status
    except TimeoutError as exc:
        raise fail(ConnectorFailure.timeout()) from exc
    except aiohttp.ClientError as exc:
        raise fail(ConnectorFailure.unavailable()) from exc

    mapped_payload = _mapping(payload)
    if status == 200 and mapped_payload is not None and "error" not in mapped_payload:
        return mapped_payload
    code = _graph_error_code(mapped_payload)
    if status == 404:
        raise fail(ConnectorFailure.not_found())
    if status == 429 or code in _GRAPH_RATE_LIMIT_ERROR_CODES:
        raise fail(ConnectorFailure.rate_limited())
    if status in {401, 403} or code in _GRAPH_AUTH_ERROR_CODES:
        raise fail(ConnectorFailure.authorization())
    raise fail(ConnectorFailure.invalid_response())


def _canonical_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    return default


# --------------------------------------------------------------------------------
# Authorization-code exchange: the concrete production implementation of
# `build_meta_oauth_redirect_route`'s injectable `exchange_code` callback
# --------------------------------------------------------------------------------

_THREADS_TOKEN_EXCHANGE_URL = "https://graph.threads.net/oauth/access_token"

_TOKEN_EXCHANGE_URL_BY_PLATFORM: Mapping[Platform, str] = {
    Platform.INSTAGRAM: f"{GRAPH_BASE}/oauth/access_token",
    Platform.FACEBOOK_PAGE: f"{GRAPH_BASE}/oauth/access_token",
    Platform.FACEBOOK: f"{GRAPH_BASE}/oauth/access_token",
    Platform.THREADS: _THREADS_TOKEN_EXCHANGE_URL,
}


async def exchange_authorization_code(
    session: object,
    *,
    platform: Platform,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    now: Callable[[], datetime] = _utc_now,
) -> MetaOAuthGrant:
    """Exchange a Meta authorization code for an access token.

    This is the concrete, real-HTTP-call implementation `build_meta_oauth_redirect_route`'s
    `exchange_code` parameter is meant to be wired to in production — the same
    injectable-`_Session` pattern `_graph_get` uses for every other Graph API call in
    this module, but for `POST {GRAPH_BASE}/oauth/access_token` (or the Threads host's
    equivalent endpoint) with the standard `client_id`/`client_secret`/`redirect_uri`/
    `code` body. A caller still supplies its own `exchange_code` callable for
    testability (matching this codebase's dependency-injection conventions elsewhere);
    that callable is expected to be a thin partial application of this function bound
    to a real session and the operator's app credentials.

    Any non-2xx or malformed response is classified into the same `ConnectorFailure`
    taxonomy `_graph_get` produces: 429 (or a Graph rate-limit error code) ->
    `RATE_LIMITED`; any other non-success status (an invalid/expired code, an invalid
    client secret, or any other rejection at the token endpoint all present this way)
    -> `AUTHORIZATION`; a malformed 200 response missing `access_token` ->
    `INVALID_RESPONSE`.
    """
    url = _TOKEN_EXCHANGE_URL_BY_PLATFORM[platform]
    session_obj = cast(_Session, session)
    request_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }

    def fail(failure: ConnectorFailure) -> MetaConnectorError:
        return MetaConnectorError(failure)

    try:
        async with session_obj.post(url, data=request_data) as response:
            try:
                payload = await response.json()
            except (
                aiohttp.ClientPayloadError,
                aiohttp.ContentTypeError,
                TypeError,
                ValueError,
            ) as exc:
                raise fail(ConnectorFailure.invalid_response()) from exc
            status = response.status
    except TimeoutError as exc:
        raise fail(ConnectorFailure.timeout()) from exc
    except aiohttp.ClientError as exc:
        raise fail(ConnectorFailure.unavailable()) from exc

    mapped_payload = _mapping(payload)
    if status == 200 and mapped_payload is not None and "error" not in mapped_payload:
        access_token = mapped_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise fail(ConnectorFailure.invalid_response())
        refresh_token = mapped_payload.get("refresh_token")
        expires_in = mapped_payload.get("expires_in")
        expires_at = (
            now() + timedelta(seconds=expires_in)
            if isinstance(expires_in, int) and expires_in > 0
            else None
        )
        return MetaOAuthGrant(
            access_token=access_token,
            refresh_token=(
                refresh_token if isinstance(refresh_token, str) and refresh_token else None
            ),
            expires_at=expires_at,
        )

    code_value = _graph_error_code(mapped_payload)
    if status == 429 or code_value in _GRAPH_RATE_LIMIT_ERROR_CODES:
        raise fail(ConnectorFailure.rate_limited())
    has_error = mapped_payload is not None and "error" in mapped_payload
    if status in {400, 401, 403} or has_error:
        raise fail(ConnectorFailure.authorization())
    raise fail(ConnectorFailure.invalid_response())


async def fetch_authorizing_user_id(session: object, access_token: str) -> str:
    """Resolve the Meta user ID of whoever just granted `access_token`.

    No existing connector's `resolve_account` answers this independent of which
    capability was authorized (Instagram/Threads resolve an IG/Threads-scoped user
    ID, the Facebook connectors a Page or profile ID) — this always calls the plain
    `GRAPH_BASE`/me` endpoint with no extra fields, using the same injectable
    `_graph_get` call shape every other Graph API read in this module uses, so a
    caller can bind `authorization_subject_id` to "the human who authorized this
    token" regardless of which resource-scoped capability was granted.
    """
    session_obj = cast(_Session, session)

    def fail(failure: ConnectorFailure) -> MetaConnectorError:
        return MetaConnectorError(failure)

    payload = await _graph_get(
        session_obj, f"{GRAPH_BASE}/me", {"fields": "id"}, access_token, fail=fail
    )
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise fail(ConnectorFailure.invalid_response())
    return user_id


@dataclass(frozen=True, slots=True)
class MetaIdentity:
    """The authorizing account's independently-confirmed identity, for capabilities
    (Instagram, Threads) where `resolve_account`'s own `handle` is not a trustworthy
    verification signal -- it falls back to echoing the caller-supplied
    `RecognizedAccountUrl.handle` whenever Graph does not return a `username` (e.g.
    a missing scope). `fetch_authorized_identity` on `InstagramConnector`/
    `ThreadsConnector` returns this instead: `username` is `None`, never an echoed
    fallback, when Graph did not return one, so a caller verifying an authorization
    can treat that as a hard rejection rather than silently trusting the value it is
    trying to verify. Mirrors `krubit.integrations.tiktok.TikTokIdentity`.
    """

    external_id: str
    username: str | None


# --------------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------------

_IG_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,is_live_now"


class InstagramConnector:
    """Instagram Graph API connector, reading the authorizing member's own media.

    Satisfies `krubit.integrations.base.Connector` structurally. `resolve_account`
    and `fetch_page` raise `MetaConnectorError` on failure; `health` never raises and
    instead reports the connector's last observed `ConnectorFailure`, if any, as an
    honest `CapabilityState`.
    """

    descriptor = CATALOG[Platform.INSTAGRAM]

    def __init__(
        self,
        session: object,
        access_token: str,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._access_token = access_token
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(f"{GRAPH_BASE}/me", {"fields": "id,username,name"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = payload.get("username")
        name = payload.get("name")
        return ConnectorAccount(
            platform=Platform.INSTAGRAM,
            external_id=external_id,
            handle=username if isinstance(username, str) and username else recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=name if isinstance(name, str) and name.strip() else None,
        )

    async def fetch_authorized_identity(self) -> MetaIdentity:
        """Independently confirm the authorizing account's `username`, never
        falling back to any caller-supplied handle -- see `MetaIdentity`'s
        docstring for why `resolve_account`'s own `handle` cannot be used for
        verification.
        """
        payload = await self._get(f"{GRAPH_BASE}/me", {"fields": "id,username"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = payload.get("username")
        return MetaIdentity(
            external_id=external_id,
            username=username if isinstance(username, str) and username.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        params: dict[str, object] = {"fields": _IG_MEDIA_FIELDS, "limit": _MAX_PAGE_SIZE}
        if cursor is not None:
            params["after"] = cursor
        payload = await self._get(f"{GRAPH_BASE}/{account.external_id}/media", params)

        items: list[Mapping[str, JSONValue]] = []
        for entry in _list(payload.get("data")) or []:
            media = _mapping(entry)
            if media is None:
                continue
            mapped = self._map_media(media)
            if mapped is not None:
                items.append(mapped)

        paging = _mapping(payload.get("paging")) or {}
        cursors = _mapping(paging.get("cursors")) or {}
        after = cursors.get("after")
        next_cursor = after if isinstance(after, str) and after else None
        return ConnectorPage(items=tuple(items), next_cursor=next_cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_media(self, media: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        media_id = media.get("id")
        if not isinstance(media_id, str) or not media_id:
            return None
        product_type = media.get("media_product_type")
        if product_type == "REELS":
            kind, state = ContentKind.REEL, ContentState.PUBLISHED
        elif product_type == "LIVE":
            kind = ContentKind.LIVE
            state = ContentState.LIVE if media.get("is_live_now") is True else ContentState.ENDED
        else:
            kind, state = ContentKind.POST, ContentState.PUBLISHED

        item: dict[str, JSONValue] = {
            "external_id": media_id,
            "kind": kind.value,
            "state": state.value,
            "canonical_url": _canonical_or_default(
                media.get("permalink"), f"https://www.instagram.com/p/{media_id}/"
            ),
        }
        caption = media.get("caption")
        if isinstance(caption, str) and caption.strip():
            item["text"] = caption[:_MAX_TEXT_LENGTH]
        timestamp = media.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            item["published_at"] = timestamp
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await _graph_get(self._session, url, params, self._access_token, fail=self._fail)

    def _fail(self, failure: ConnectorFailure) -> MetaConnectorError:
        self._last_failure = failure
        return MetaConnectorError(failure)


# --------------------------------------------------------------------------------
# Facebook Page
# --------------------------------------------------------------------------------

_FB_POST_FIELDS = "id,message,created_time,permalink_url"
_FB_VIDEO_FIELDS = "id,description,created_time,permalink_url,is_reels_video"
_FB_LIVE_VIDEO_FIELDS = "id,title,permalink_url,creation_time,status"


class FacebookPageConnector:
    """Facebook Graph API connector for a Page the authorizing member manages.

    Satisfies `krubit.integrations.base.Connector` structurally. `fetch_page` merges
    three bounded Graph edges (`/posts`, `/videos`, `/live_videos`) into one page and
    never returns a durable cross-edge cursor — see the module docstring for why that
    is a safe simplification for this connector.
    """

    descriptor = CATALOG[Platform.FACEBOOK_PAGE]

    def __init__(
        self,
        session: object,
        access_token: str,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._access_token = access_token
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(f"{GRAPH_BASE}/me", {"fields": "id,name"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        name = payload.get("name")
        return ConnectorAccount(
            platform=Platform.FACEBOOK_PAGE,
            external_id=external_id,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=name if isinstance(name, str) and name.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        posts_payload = await self._get(
            f"{GRAPH_BASE}/{account.external_id}/posts",
            {"fields": _FB_POST_FIELDS, "limit": _MAX_PAGE_SIZE},
        )
        videos_payload = await self._get(
            f"{GRAPH_BASE}/{account.external_id}/videos",
            {"fields": _FB_VIDEO_FIELDS, "limit": _MAX_PAGE_SIZE},
        )
        live_payload = await self._get(
            f"{GRAPH_BASE}/{account.external_id}/live_videos",
            {"fields": _FB_LIVE_VIDEO_FIELDS, "limit": _MAX_PAGE_SIZE},
        )

        items: list[Mapping[str, JSONValue]] = []
        for entry in _list(posts_payload.get("data")) or []:
            mapped_entry = _mapping(entry)
            if mapped_entry is not None:
                mapped = self._map_post(mapped_entry)
                if mapped is not None:
                    items.append(mapped)
        for entry in _list(videos_payload.get("data")) or []:
            mapped_entry = _mapping(entry)
            if mapped_entry is not None:
                mapped = self._map_video(mapped_entry)
                if mapped is not None:
                    items.append(mapped)
        for entry in _list(live_payload.get("data")) or []:
            mapped_entry = _mapping(entry)
            if mapped_entry is not None:
                mapped = self._map_live_video(mapped_entry)
                if mapped is not None:
                    items.append(mapped)

        return ConnectorPage(items=tuple(items), next_cursor=None)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_post(self, post: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        post_id = post.get("id")
        if not isinstance(post_id, str) or not post_id:
            return None
        item: dict[str, JSONValue] = {
            "external_id": post_id,
            "kind": ContentKind.POST.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_or_default(
                post.get("permalink_url"), f"https://www.facebook.com/{post_id}"
            ),
        }
        message = post.get("message")
        if isinstance(message, str) and message.strip():
            item["text"] = message[:_MAX_TEXT_LENGTH]
        created_time = post.get("created_time")
        if isinstance(created_time, str) and created_time:
            item["published_at"] = created_time
        return item

    def _map_video(self, video: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        video_id = video.get("id")
        if not isinstance(video_id, str) or not video_id:
            return None
        kind = ContentKind.REEL if video.get("is_reels_video") is True else ContentKind.VIDEO
        item: dict[str, JSONValue] = {
            "external_id": video_id,
            "kind": kind.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_or_default(
                video.get("permalink_url"), f"https://www.facebook.com/{video_id}"
            ),
        }
        description = video.get("description")
        if isinstance(description, str) and description.strip():
            item["text"] = description[:_MAX_TEXT_LENGTH]
        created_time = video.get("created_time")
        if isinstance(created_time, str) and created_time:
            item["published_at"] = created_time
        return item

    def _map_live_video(self, live: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        live_id = live.get("id")
        if not isinstance(live_id, str) or not live_id:
            return None
        status = live.get("status")
        if status in _LIVE_VIDEO_LIVE_STATUSES:
            state = ContentState.LIVE
        elif status in _LIVE_VIDEO_SCHEDULED_STATUSES:
            state = ContentState.SCHEDULED
        elif status in _LIVE_VIDEO_ENDED_STATUSES:
            state = ContentState.ENDED
        elif status in _LIVE_VIDEO_CANCELLED_STATUSES:
            state = ContentState.CANCELLED
        else:
            return None
        item: dict[str, JSONValue] = {
            "external_id": live_id,
            "kind": ContentKind.LIVE.value,
            "state": state.value,
            "canonical_url": _canonical_or_default(
                live.get("permalink_url"), f"https://www.facebook.com/{live_id}"
            ),
        }
        title = live.get("title")
        if isinstance(title, str) and title.strip():
            item["title"] = title[:_MAX_TEXT_LENGTH]
        creation_time = live.get("creation_time")
        if isinstance(creation_time, str) and creation_time:
            item["published_at"] = creation_time
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await _graph_get(self._session, url, params, self._access_token, fail=self._fail)

    def _fail(self, failure: ConnectorFailure) -> MetaConnectorError:
        self._last_failure = failure
        return MetaConnectorError(failure)


# --------------------------------------------------------------------------------
# Facebook personal profile
# --------------------------------------------------------------------------------


class FacebookProfileConnector:
    """Facebook Graph API connector for a member's personal profile.

    Personal-profile content is `CapabilityState.APPROVAL_REQUIRED` unless
    constructed with `app_review_granted=True` (the operator has both an explicit
    owner-content grant from the member and Meta app review permitting the read).
    Without it, `fetch_page` never attempts a network call — it raises immediately —
    and `health` always reports `APPROVAL_REQUIRED` regardless of any prior failure.
    """

    descriptor = CATALOG[Platform.FACEBOOK]

    def __init__(
        self,
        session: object,
        access_token: str,
        *,
        app_review_granted: bool = False,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._access_token = access_token
        self._app_review_granted = app_review_granted
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(f"{GRAPH_BASE}/me", {"fields": "id,name"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        name = payload.get("name")
        return ConnectorAccount(
            platform=Platform.FACEBOOK,
            external_id=external_id,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=name if isinstance(name, str) and name.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        if not self._app_review_granted:
            raise self._fail(
                ConnectorFailure.authorization(
                    "personal-profile content requires app review and an owner grant"
                )
            )
        payload = await self._get(
            f"{GRAPH_BASE}/{account.external_id}/posts",
            {"fields": _FB_POST_FIELDS, "limit": _MAX_PAGE_SIZE},
        )
        items: list[Mapping[str, JSONValue]] = []
        for entry in _list(payload.get("data")) or []:
            mapped_entry = _mapping(entry)
            if mapped_entry is not None:
                mapped = self._map_post(mapped_entry)
                if mapped is not None:
                    items.append(mapped)
        return ConnectorPage(items=tuple(items), next_cursor=None)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if not self._app_review_granted:
            return ConnectorHealth(
                capability=Capability.SOCIAL,
                state=CapabilityState.APPROVAL_REQUIRED,
                detail="personal-profile content requires app review and an owner grant",
            )
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_post(self, post: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        post_id = post.get("id")
        if not isinstance(post_id, str) or not post_id:
            return None
        item: dict[str, JSONValue] = {
            "external_id": post_id,
            "kind": ContentKind.POST.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_or_default(
                post.get("permalink_url"), f"https://www.facebook.com/{post_id}"
            ),
        }
        message = post.get("message")
        if isinstance(message, str) and message.strip():
            item["text"] = message[:_MAX_TEXT_LENGTH]
        created_time = post.get("created_time")
        if isinstance(created_time, str) and created_time:
            item["published_at"] = created_time
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await _graph_get(self._session, url, params, self._access_token, fail=self._fail)

    def _fail(self, failure: ConnectorFailure) -> MetaConnectorError:
        self._last_failure = failure
        return MetaConnectorError(failure)


# --------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------

_THREADS_MEDIA_FIELDS = "id,text,permalink,timestamp,is_quote_post,root_post"


class ThreadsConnector:
    """Threads Graph API connector, reading only the authorizing member's own
    original posts — replies and quote posts are always excluded.
    """

    descriptor = CATALOG[Platform.THREADS]

    def __init__(
        self,
        session: object,
        access_token: str,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = cast(_Session, session)
        self._access_token = access_token
        self._now = now
        self._last_failure: ConnectorFailure | None = None

    async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
        payload = await self._get(f"{THREADS_BASE}/me", {"fields": "id,username"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = payload.get("username")
        return ConnectorAccount(
            platform=Platform.THREADS,
            external_id=external_id,
            handle=username if isinstance(username, str) and username else recognized.handle,
            canonical_url=recognized.canonical_url,
        )

    async def fetch_authorized_identity(self) -> MetaIdentity:
        """Independently confirm the authorizing account's `username`, never
        falling back to any caller-supplied handle -- see `MetaIdentity`'s
        docstring for why `resolve_account`'s own `handle` cannot be used for
        verification.
        """
        payload = await self._get(f"{THREADS_BASE}/me", {"fields": "id,username"})
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = payload.get("username")
        return MetaIdentity(
            external_id=external_id,
            username=username if isinstance(username, str) and username.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        params: dict[str, object] = {"fields": _THREADS_MEDIA_FIELDS, "limit": _MAX_PAGE_SIZE}
        if cursor is not None:
            params["after"] = cursor
        payload = await self._get(f"{THREADS_BASE}/{account.external_id}/threads", params)

        items: list[Mapping[str, JSONValue]] = []
        for entry in _list(payload.get("data")) or []:
            media = _mapping(entry)
            if media is None or media.get("is_quote_post") is True:
                continue
            if media.get("root_post") not in (None, ""):
                continue
            mapped = self._map_thread(media)
            if mapped is not None:
                items.append(mapped)

        paging = _mapping(payload.get("paging")) or {}
        cursors = _mapping(paging.get("cursors")) or {}
        after = cursors.get("after")
        next_cursor = after if isinstance(after, str) and after else None
        return ConnectorPage(items=tuple(items), next_cursor=next_cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_thread(self, media: Mapping[str, object]) -> Mapping[str, JSONValue] | None:
        media_id = media.get("id")
        if not isinstance(media_id, str) or not media_id:
            return None
        item: dict[str, JSONValue] = {
            "external_id": media_id,
            "kind": ContentKind.POST.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_or_default(
                media.get("permalink"), f"https://www.threads.net/t/{media_id}"
            ),
        }
        text = media.get("text")
        if isinstance(text, str) and text.strip():
            item["text"] = text[:_MAX_TEXT_LENGTH]
        timestamp = media.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            item["published_at"] = timestamp
        return item

    async def _get(self, url: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await _graph_get(self._session, url, params, self._access_token, fail=self._fail)

    def _fail(self, failure: ConnectorFailure) -> MetaConnectorError:
        self._last_failure = failure
        return MetaConnectorError(failure)


# --------------------------------------------------------------------------------
# Web ingress: authorization redirect and deauthorization/data-deletion webhook
# --------------------------------------------------------------------------------


def build_meta_oauth_redirect_route(
    *,
    path: str,
    oauth_states: MetaOAuthStates,
    exchange_code: Callable[[str, Platform], Awaitable[MetaOAuthGrant]],
    on_authorized: Callable[[int, int, Platform, MetaOAuthGrant], Awaitable[None]],
) -> CallbackRoute:
    """Build the single GET route Meta redirects a member's browser back to.

    `guild_id`/`member_id` arrive as query parameters preserved from the
    authorize-URL's `redirect_uri` (Meta echoes any query string already present on
    the configured redirect URI back unchanged); `code`/`state` are appended by
    Meta itself. The state is validated and single-use-consumed via
    `oauth_states.consume` *before* `exchange_code` is ever called, so a replayed or
    forged redirect can never trigger a token exchange.

    `exchange_code` is injectable for testability, but production wiring should bind
    it to `exchange_authorization_code` (this module's concrete, real-HTTP-call
    implementation) with a real session and the operator's app credentials, e.g.
    `functools.partial(exchange_authorization_code, session, client_id=..., ...)`.
    """

    async def handle_redirect(query: Mapping[str, str]) -> str:
        raw_guild_id = query.get("guild_id")
        raw_member_id = query.get("member_id")
        state = query.get("state")
        code = query.get("code")
        if not raw_guild_id or not raw_member_id or not state or not code:
            raise ValueError("authorization redirect is missing required parameters")
        try:
            guild_id = int(raw_guild_id)
            member_id = int(raw_member_id)
        except ValueError as exc:
            raise ValueError("authorization redirect has invalid identifiers") from exc

        platform = oauth_states.consume(state, guild_id=guild_id, member_id=member_id)
        grant = await exchange_code(code, platform)
        await on_authorized(guild_id, member_id, platform, grant)
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path=path, redirect=redirect)


def _verify_meta_webhook_signature(
    body: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Verify Meta's `X-Hub-Signature-256` HMAC-SHA256 over the raw webhook body."""
    if not signature_header or not app_secret:
        return False
    algorithm, _, digest = signature_header.partition("=")
    if algorithm != "sha256" or not digest:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


def verify_meta_signed_request(
    signed_request: str,
    app_secret: str,
    *,
    now: Callable[[], datetime] = _utc_now,
    max_age: timedelta = timedelta(minutes=5),
) -> Mapping[str, object] | None:
    """Verify and parse Meta's form-posted `signed_request` value.

    Format: `<base64url-signature>.<base64url-json-payload>`, HMAC-SHA256 over the
    encoded payload string, keyed by the app secret. Rejects a missing/extra
    separator, a bad signature, non-JSON payload, or an `issued_at` older than
    `max_age` — a validly-signed but stale request is a replay, not a fresh call.
    """
    parts = signed_request.split(".")
    if len(parts) != 2:
        return None
    encoded_signature, encoded_payload = parts
    try:
        signature = base64.urlsafe_b64decode(encoded_signature + "==")
        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "==")
    except (binascii.Error, ValueError):
        return None
    expected_signature = hmac.new(
        app_secret.encode("utf-8"), encoded_payload.encode("utf-8"), sha256
    ).digest()
    if not hmac.compare_digest(expected_signature, signature):
        return None
    try:
        payload_raw = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload_raw, dict):
        return None
    payload = cast(dict[str, object], payload_raw)
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        return None
    issued_datetime = datetime.fromtimestamp(issued_at, tz=UTC)
    if now() - issued_datetime > max_age:
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    return payload


def build_meta_deauthorization_route(
    *,
    path: str,
    app_secret: str,
    handle_deauthorization: Callable[[bytes], Awaitable[None]],
) -> CallbackRoute:
    """Build the single signed POST route for Meta's deauthorization/data-deletion
    callback. A body whose `X-Hub-Signature-256` does not verify against `app_secret`
    is rejected with 403 and `handle_deauthorization` is never called.
    """
    webhook = SignedWebhook(
        verify_signature=lambda body, header: _verify_meta_webhook_signature(
            body, header, app_secret
        ),
        handle_notification=handle_deauthorization,
    )
    return build_signed_webhook_route(path=path, header_name="X-Hub-Signature-256", webhook=webhook)
