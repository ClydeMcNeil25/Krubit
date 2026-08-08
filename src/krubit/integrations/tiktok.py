"""TikTok Display API connector, plus the signed/expiring/single-use OAuth state
that guards the member-facing TikTok Login Kit authorization flow.

Like `krubit.integrations.meta`, TikTok content only exists behind a *member's own*
Login Kit OAuth grant: there is no static app-only credential that can read a
creator's videos. `TikTokOAuthStates` mirrors `meta.MetaOAuthStates` — a signed,
expiring, single-use token binding one authorization attempt to exactly one
(guild, member) pair, with a short expiry and every rejection reason collapsed to
the same generic message so the mechanism itself gives no oracle to probe. It is
narrower than Meta's version in one respect: TikTok's catalog entry is a single
platform, so the signed payload embeds a fixed domain tag instead of a `Platform`
value, purely to stop a state minted for a different flow from being replayed here.

`TikTokConnector` reads `POST /v2/video/list/` for the authorized creator's own
recent public videos via `resolve_account`'s `open_id`. It deliberately has no code
path that infers a LIVE state from anything — not profile HTML, not an embed, not a
heuristic on video fields. `Capability.LIVE` stays exactly what `catalog.py`
declares (`CapabilityState.APPROVAL_REQUIRED`, "pending reliable TikTok detection
access") because nothing in this module ever claims otherwise; every item this
connector emits is `ContentKind.VIDEO` / `ContentState.PUBLISHED`.

`exchange_authorization_code` is the concrete, real-HTTP-call implementation for
`POST /v2/oauth/token/` (TikTok's OAuth2-shaped token endpoint — a flat JSON
response/error envelope, unlike the `{"data": ..., "error": {"code": ...}}` wrapper
every other TikTok Content API v2 endpoint uses), following the same lesson Task 9's
review applied to Meta: `build_tiktok_oauth_redirect_route`'s `exchange_code`
parameter is injectable for testability, but production wiring binds it to this
function, not to a callback with no real implementation.
"""

from __future__ import annotations

import base64
import binascii
import hmac
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
from krubit.web.callbacks import CallbackRoute, OAuthRedirect, build_oauth_redirect_route

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
USER_INFO_URL = f"{TIKTOK_API_BASE}/user/info/"
VIDEO_LIST_URL = f"{TIKTOK_API_BASE}/video/list/"
TIKTOK_TOKEN_URL = f"{TIKTOK_API_BASE}/oauth/token/"

_USER_INFO_FIELDS = "open_id,display_name"
_VIDEO_FIELDS = "id,create_time,share_url,video_description"
_MAX_PAGE_SIZE = 20  # TikTok's own `max_count` ceiling for /v2/video/list/.
_MAX_TEXT_LENGTH = 300
_DEFAULT_STATE_TTL = timedelta(minutes=10)
_STATE_USED_OR_EXPIRED = "OAuth state was used or expired"
_STATE_DOMAIN = "tiktok"

# TikTok Content API v2 `error.code` values that mean the access token itself is
# unusable or lacks the required scope — both are treated as the same
# `ConnectorFailureKind.AUTHORIZATION` outcome a re-granted authorization clears.
_TIKTOK_AUTH_ERROR_CODES = frozenset(
    {"access_token_invalid", "scope_not_authorized", "scope_permission_missing"}
)
_TIKTOK_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_exceeded"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------
# OAuth state: signed, expiring, single-use, bound to (guild_id, member_id)
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StatePayload:
    guild_id: int
    member_id: int
    expires_at: datetime
    nonce: str


class TikTokOAuthStates:
    """Signed, expiring, single-use OAuth state tokens bound to guild/member.

    Mirrors `krubit.integrations.meta.MetaOAuthStates`: `issue` mints an
    HMAC-SHA256-signed token embedding the (guild, member) pair, an expiry, and a
    random nonce. `consume` verifies the signature, checks the token has not
    expired, checks the caller-supplied `guild_id`/`member_id` match what was
    issued, and checks the nonce has not already been consumed — marking it
    consumed on success so a second `consume` call for the same token always fails,
    even before expiry. Every failure path raises the same generic
    `ValueError("OAuth state was used or expired")`, matching Meta's rationale:
    distinguishing *why* a state was rejected would let an attacker probe the
    CSRF-prevention mechanism itself.
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

    def issue(self, *, guild_id: int, member_id: int) -> str:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if member_id <= 0:
            raise ValueError("member_id must be positive")
        nonce = uuid4().hex
        expires_at = self._now() + self._ttl
        payload = "|".join(
            (
                str(guild_id),
                str(member_id),
                _STATE_DOMAIN,
                str(int(expires_at.timestamp())),
                nonce,
            )
        )
        signature = hmac.new(self._signing_key, payload.encode("utf-8"), sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
        return f"{token}.{signature}"

    def consume(self, state: str, *, guild_id: int, member_id: int) -> None:
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
        raw_guild_id, raw_member_id, domain, raw_expires_at, nonce = parts
        if domain != _STATE_DOMAIN:
            return None
        try:
            guild_id = int(raw_guild_id)
            member_id = int(raw_member_id)
            expires_at = datetime.fromtimestamp(int(raw_expires_at), tz=UTC)
        except ValueError:
            return None
        if not nonce:
            return None
        return _StatePayload(
            guild_id=guild_id, member_id=member_id, expires_at=expires_at, nonce=nonce
        )


# --------------------------------------------------------------------------------
# OAuth grant sealing: a connector never stores or logs an unsealed token
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TikTokOAuthGrant:
    """An unsealed TikTok Login Kit OAuth grant, held only transiently between
    exchange and sealing.

    `access_token` and `refresh_token` are marked `repr=False` from the start,
    matching every other secret-bearing field in this codebase
    (`krubit.config.Settings`, `krubit.integrations.meta.MetaOAuthGrant`):
    `repr(grant)`/`str(grant)` must never render a plaintext token.
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("access_token must not be blank")


@dataclass(frozen=True, slots=True)
class TikTokIdentity:
    """The authorizing TikTok account's independently-confirmed identity.

    Unlike `resolve_account`'s `handle` (which is merely echoed back from its
    caller, never confirmed against TikTok's own data), `username` here comes
    straight from TikTok's userinfo response — the only field this codebase can
    use to genuinely verify which account authorized a token.
    """

    open_id: str
    username: str | None


def seal_oauth_grant(vault: CredentialVault, grant: TikTokOAuthGrant) -> str:
    """Seal an OAuth grant for storage; the caller never persists `grant` itself."""
    payload: dict[str, JSONValue] = {"access_token": grant.access_token}
    if grant.refresh_token is not None:
        payload["refresh_token"] = grant.refresh_token
    if grant.expires_at is not None:
        payload["expires_at"] = grant.expires_at.isoformat()
    return vault.seal_json(payload)


def open_oauth_grant(vault: CredentialVault, sealed: str) -> TikTokOAuthGrant:
    """Unseal a token produced by `seal_oauth_grant`, just before a connector uses it."""
    payload = vault.open_json(sealed)
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CredentialVaultError("sealed OAuth grant is missing access_token")
    refresh_token = payload.get("refresh_token")
    raw_expires_at = payload.get("expires_at")
    expires_at = datetime.fromisoformat(raw_expires_at) if isinstance(raw_expires_at, str) else None
    return TikTokOAuthGrant(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------------
# Shared Display API request/error-mapping plumbing
# --------------------------------------------------------------------------------


class TikTokConnectorError(RuntimeError):
    """Raised by `TikTokConnector` (and the token exchange) when a request cannot
    be completed.

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


def _tiktok_error_code(payload: Mapping[str, object] | None) -> str | None:
    if payload is None:
        return None
    error = _mapping(payload.get("error"))
    if error is None:
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _canonical_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    return default


async def _tiktok_post(
    session: _Session,
    url: str,
    body: Mapping[str, object],
    access_token: str,
    *,
    fail: Callable[[ConnectorFailure], TikTokConnectorError],
) -> Mapping[str, object]:
    """POST to one `{"data": ..., "error": {"code": ...}}`-shaped Content API v2
    endpoint (`/user/info/`, `/video/list/`), returning the unwrapped `data` object
    on success.
    """
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        async with session.post(url, json=dict(body), headers=headers) as response:
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
    code = _tiktok_error_code(mapped_payload)
    if status == 200 and code == "ok" and mapped_payload is not None:
        data = _mapping(mapped_payload.get("data"))
        if data is None:
            raise fail(ConnectorFailure.invalid_response())
        return data
    if status == 429 or code in _TIKTOK_RATE_LIMIT_ERROR_CODES:
        raise fail(ConnectorFailure.rate_limited())
    if status in {401, 403} or code in _TIKTOK_AUTH_ERROR_CODES:
        raise fail(ConnectorFailure.authorization())
    if status == 404:
        raise fail(ConnectorFailure.not_found())
    raise fail(ConnectorFailure.invalid_response())


# --------------------------------------------------------------------------------
# Authorization-code exchange: the concrete production implementation of
# `build_tiktok_oauth_redirect_route`'s injectable `exchange_code` callback
# --------------------------------------------------------------------------------


async def exchange_authorization_code(
    session: object,
    *,
    code: str,
    client_key: str,
    client_secret: str,
    redirect_uri: str,
    now: Callable[[], datetime] = _utc_now,
) -> TikTokOAuthGrant:
    """Exchange a TikTok Login Kit authorization code for an access token.

    This is the concrete, real-HTTP-call implementation `build_tiktok_oauth_redirect_route`'s
    `exchange_code` parameter is meant to be wired to in production, for
    `POST {TIKTOK_TOKEN_URL}` with the standard `client_key`/`client_secret`/`code`/
    `grant_type`/`redirect_uri` form body. Unlike every other TikTok endpoint this
    module calls, the token endpoint's response is a flat OAuth2-shaped JSON object
    (`access_token`/`expires_in`/... on success, `error`/`error_description` on
    failure) rather than the `{"data": ..., "error": {"code": ...}}` envelope
    `_tiktok_post` handles, so it is parsed separately here.

    A caller still supplies its own `exchange_code` callable for testability
    (matching this codebase's dependency-injection conventions elsewhere); that
    callable is expected to be a thin partial application of this function bound to
    a real session and the operator's app credentials.

    Any non-2xx or malformed response is classified into the same `ConnectorFailure`
    taxonomy `_tiktok_post` produces: 429 (or `error == "rate_limit_exceeded"`) ->
    `RATE_LIMITED`; any other error response (an invalid/expired code, an invalid
    client secret, or any other rejection at the token endpoint) -> `AUTHORIZATION`;
    a malformed 200 response missing `access_token` -> `INVALID_RESPONSE`.
    """
    session_obj = cast(_Session, session)
    request_data = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    def fail(failure: ConnectorFailure) -> TikTokConnectorError:
        return TikTokConnectorError(failure)

    try:
        async with session_obj.post(TIKTOK_TOKEN_URL, data=request_data) as response:
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
    has_error = mapped_payload is not None and "error" in mapped_payload
    if status == 200 and mapped_payload is not None and not has_error:
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
        return TikTokOAuthGrant(
            access_token=access_token,
            refresh_token=(
                refresh_token if isinstance(refresh_token, str) and refresh_token else None
            ),
            expires_at=expires_at,
        )

    error_value = mapped_payload.get("error") if mapped_payload is not None else None
    if status == 429 or error_value == "rate_limit_exceeded":
        raise fail(ConnectorFailure.rate_limited())
    if status in {400, 401, 403} or has_error:
        raise fail(ConnectorFailure.authorization())
    raise fail(ConnectorFailure.invalid_response())


# --------------------------------------------------------------------------------
# TikTok Display API connector
# --------------------------------------------------------------------------------


class TikTokConnector:
    """TikTok Display API connector, reading the authorizing creator's own videos.

    Satisfies `krubit.integrations.base.Connector` structurally. `resolve_account`
    and `fetch_page` raise `TikTokConnectorError` on failure; `health` never raises
    and instead reports the connector's last observed `ConnectorFailure`, if any, as
    an honest `CapabilityState`. There is no LIVE-related code path anywhere in this
    class: `Capability.LIVE` stays exactly `catalog.py`'s static declaration
    (`APPROVAL_REQUIRED`) because nothing here ever reports otherwise.
    """

    descriptor = CATALOG[Platform.TIKTOK]

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
        data = await self._post(f"{USER_INFO_URL}?fields={_USER_INFO_FIELDS}", {})
        user = _mapping(data.get("user"))
        if user is None:
            raise self._fail(ConnectorFailure.invalid_response())
        open_id = user.get("open_id")
        if not isinstance(open_id, str) or not open_id:
            raise self._fail(ConnectorFailure.invalid_response())
        display_name = user.get("display_name")
        return ConnectorAccount(
            platform=Platform.TIKTOK,
            external_id=open_id,
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
            display_name=(
                display_name if isinstance(display_name, str) and display_name.strip() else None
            ),
        )

    async def fetch_authorized_identity(self) -> TikTokIdentity:
        data = await self._post(f"{USER_INFO_URL}?fields=open_id,username", {})
        user = _mapping(data.get("user"))
        if user is None:
            raise self._fail(ConnectorFailure.invalid_response())
        open_id = user.get("open_id")
        if not isinstance(open_id, str) or not open_id:
            raise self._fail(ConnectorFailure.invalid_response())
        username = user.get("username")
        return TikTokIdentity(
            open_id=open_id,
            username=username if isinstance(username, str) and username.strip() else None,
        )

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        body: dict[str, object] = {"max_count": _MAX_PAGE_SIZE}
        if cursor is not None:
            body["cursor"] = int(cursor)
        data = await self._post(f"{VIDEO_LIST_URL}?fields={_VIDEO_FIELDS}", body)

        items: list[Mapping[str, JSONValue]] = []
        for entry in _list(data.get("videos")) or []:
            video = _mapping(entry)
            if video is None:
                continue
            mapped = self._map_video(video, account.handle)
            if mapped is not None:
                items.append(mapped)

        next_cursor: str | None = None
        if data.get("has_more") is True:
            raw_cursor = data.get("cursor")
            if isinstance(raw_cursor, int):
                next_cursor = str(raw_cursor)
        return ConnectorPage(items=tuple(items), next_cursor=next_cursor)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        if self._last_failure is None:
            return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)
        state = _HEALTH_STATE_BY_FAILURE.get(self._last_failure.kind, CapabilityState.DEGRADED)
        return ConnectorHealth(
            capability=Capability.SOCIAL, state=state, detail=self._last_failure.safe_detail
        )

    def _map_video(
        self, video: Mapping[str, object], handle: str
    ) -> Mapping[str, JSONValue] | None:
        video_id = video.get("id")
        if not isinstance(video_id, str) or not video_id:
            return None
        item: dict[str, JSONValue] = {
            "external_id": video_id,
            "kind": ContentKind.VIDEO.value,
            "state": ContentState.PUBLISHED.value,
            "canonical_url": _canonical_or_default(
                video.get("share_url"), f"https://www.tiktok.com/@{handle}/video/{video_id}"
            ),
        }
        description = video.get("video_description")
        if isinstance(description, str) and description.strip():
            item["text"] = description[:_MAX_TEXT_LENGTH]
        create_time = video.get("create_time")
        if isinstance(create_time, int):
            item["published_at"] = datetime.fromtimestamp(create_time, tz=UTC).isoformat()
        return item

    async def _post(self, url: str, body: Mapping[str, object]) -> Mapping[str, object]:
        return await _tiktok_post(self._session, url, body, self._access_token, fail=self._fail)

    def _fail(self, failure: ConnectorFailure) -> TikTokConnectorError:
        self._last_failure = failure
        return TikTokConnectorError(failure)


# --------------------------------------------------------------------------------
# Web ingress: authorization redirect
# --------------------------------------------------------------------------------


def build_tiktok_oauth_redirect_route(
    *,
    path: str,
    oauth_states: TikTokOAuthStates,
    exchange_code: Callable[[str], Awaitable[TikTokOAuthGrant]],
    on_authorized: Callable[[int, int, TikTokOAuthGrant], Awaitable[None]],
) -> CallbackRoute:
    """Build the single GET route TikTok redirects a member's browser back to.

    `guild_id`/`member_id` arrive as query parameters preserved from the
    authorize-URL's `redirect_uri`; `code`/`state` are appended by TikTok itself.
    The state is validated and single-use-consumed via `oauth_states.consume`
    *before* `exchange_code` is ever called, so a replayed or forged redirect can
    never trigger a token exchange — matching
    `krubit.integrations.meta.build_meta_oauth_redirect_route`'s ordering.

    `exchange_code` is injectable for testability, but production wiring should bind
    it to `exchange_authorization_code` (this module's concrete, real-HTTP-call
    implementation) with a real session and the operator's app credentials, e.g.
    `functools.partial(exchange_authorization_code, session, client_key=..., ...)`.
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

        oauth_states.consume(state, guild_id=guild_id, member_id=member_id)
        grant = await exchange_code(code)
        await on_authorized(guild_id, member_id, grant)
        return "Authorization complete. You may close this window."

    redirect = OAuthRedirect(handle_redirect=handle_redirect)
    return build_oauth_redirect_route(path=path, redirect=redirect)
