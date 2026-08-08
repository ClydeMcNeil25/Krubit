from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from krubit.integrations.tiktok import (
    TIKTOK_TOKEN_URL,
    USER_INFO_URL,
    TikTokConnector,
    TikTokConnectorError,
    TikTokOAuthGrant,
    TikTokOAuthStates,
    exchange_authorization_code,
    open_oauth_grant,
    seal_oauth_grant,
)
from krubit.security.credential_vault import CredentialVault

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_TEST_SIGNING_KEY = "test-only-tiktok-oauth-state-signing-key"
_VAULT_KEY = "test-only-credential-encryption-key-material"

TIKTOK_VIDEO_LIST = {
    "data": {
        "videos": [
            {
                "id": "video-1",
                "create_time": 1786017600,
                "share_url": "https://www.tiktok.com/@krucialstudios/video/video-1",
                "video_description": "New upload!",
            },
            {
                "id": "video-2",
                "create_time": 1786014000,
                "share_url": "https://www.tiktok.com/@krucialstudios/video/video-2",
                "video_description": "Another one",
            },
        ],
        "cursor": 1786014000,
        "has_more": False,
    },
    "error": {"code": "ok", "message": "", "log_id": "log-1"},
}

TIKTOK_TOKEN_ERROR_FIXTURE = {
    "error": {"code": "access_token_invalid", "message": "The access token is invalid or expired."}
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

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def tiktok_connector(payload: object, *, status: int = 200) -> TikTokConnector:
    session = FakeSession([FakeResponse(status, payload)])
    return TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)


def tiktok_account(external_id: str = "open-id-1") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.TIKTOK, external_id),
        owner_member_id=222,
        platform=Platform.TIKTOK,
        handle="krucialstudios",
        canonical_url="https://www.tiktok.com/@krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


# --------------------------------------------------------------------------------
# Video reads and capability gating
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tiktok_reads_authorized_videos_but_does_not_claim_live_detection() -> None:
    connector = tiktok_connector(TIKTOK_VIDEO_LIST)
    page = await connector.fetch_page(tiktok_account(), cursor=None)
    assert [item["kind"] for item in page.items] == [
        ContentKind.VIDEO.value,
        ContentKind.VIDEO.value,
    ]
    live = connector.descriptor.capability(Capability.LIVE)
    assert live.state is CapabilityState.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_tiktok_video_items_are_always_published_and_never_carry_a_live_state() -> None:
    page = await tiktok_connector(TIKTOK_VIDEO_LIST).fetch_page(tiktok_account(), cursor=None)
    assert all(item["state"] == ContentState.PUBLISHED.value for item in page.items)
    assert all(item["kind"] != ContentKind.LIVE.value for item in page.items)


@pytest.mark.asyncio
async def test_tiktok_maps_description_and_share_url() -> None:
    page = await tiktok_connector(TIKTOK_VIDEO_LIST).fetch_page(tiktok_account(), cursor=None)
    first = page.items[0]
    assert first["external_id"] == "video-1"
    assert first["canonical_url"] == "https://www.tiktok.com/@krucialstudios/video/video-1"
    assert first["text"] == "New upload!"
    assert first["published_at"] == datetime.fromtimestamp(1786017600, tz=UTC).isoformat()


@pytest.mark.asyncio
async def test_tiktok_next_cursor_only_set_when_has_more() -> None:
    page = await tiktok_connector(TIKTOK_VIDEO_LIST).fetch_page(tiktok_account(), cursor=None)
    assert page.next_cursor is None

    more_fixture = {
        "data": {
            "videos": TIKTOK_VIDEO_LIST["data"]["videos"],  # type: ignore[index]
            "cursor": 1786014000,
            "has_more": True,
        },
        "error": {"code": "ok", "message": "", "log_id": "log-2"},
    }
    page_with_more = await tiktok_connector(more_fixture).fetch_page(tiktok_account(), cursor=None)
    assert page_with_more.next_cursor == "1786014000"


@pytest.mark.asyncio
async def test_tiktok_resolve_account_uses_user_info_endpoint() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": {"user": {"open_id": "open-id-1", "display_name": "Krucial Studios"}},
                    "error": {"code": "ok", "message": "", "log_id": "log-3"},
                },
            )
        ]
    )
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    result = await connector.resolve_account(
        RecognizedAccountUrl(
            platform=Platform.TIKTOK,
            handle="krucialstudios",
            canonical_url="https://www.tiktok.com/@krucialstudios",
        )
    )
    assert result.external_id == "open-id-1"
    assert result.display_name == "Krucial Studios"
    url, kwargs = session.requests[0]
    assert url.startswith(USER_INFO_URL)
    headers = cast("dict[str, str]", kwargs["headers"])
    assert headers["Authorization"] == "Bearer tiktok-access-token"


@pytest.mark.asyncio
async def test_tiktok_raises_authorization_on_invalid_access_token() -> None:
    with pytest.raises(TikTokConnectorError) as excinfo:
        await tiktok_connector(TIKTOK_TOKEN_ERROR_FIXTURE, status=401).fetch_page(
            tiktok_account(), cursor=None
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_tiktok_raises_rate_limited_on_throttling() -> None:
    payload = {"error": {"code": "rate_limit_exceeded", "message": "Too many requests"}}
    with pytest.raises(TikTokConnectorError) as excinfo:
        await tiktok_connector(payload, status=429).fetch_page(tiktok_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_tiktok_raises_invalid_response_on_malformed_json() -> None:
    session = FakeSession([InvalidJsonResponse(200, None)])
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    with pytest.raises(TikTokConnectorError) as excinfo:
        await connector.fetch_page(tiktok_account(), cursor=None)
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_tiktok_health_moves_to_authorization_required_after_token_failure() -> None:
    connector = tiktok_connector(TIKTOK_TOKEN_ERROR_FIXTURE, status=401)

    healthy = await connector.health()
    assert healthy.state is CapabilityState.READY

    with pytest.raises(TikTokConnectorError):
        await connector.fetch_page(tiktok_account(), cursor=None)

    degraded = await connector.health()
    assert degraded.capability is Capability.SOCIAL
    assert degraded.state is CapabilityState.AUTHORIZATION_REQUIRED


# --------------------------------------------------------------------------------
# OAuth state: signed, expiring, single-use, bound to (guild_id, member_id)
# --------------------------------------------------------------------------------


def test_tiktok_oauth_state_is_guild_member_bound_and_single_use() -> None:
    states = TikTokOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = states.issue(guild_id=111, member_id=222)
    states.consume(state, guild_id=111, member_id=222)
    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=111, member_id=222)


def test_tiktok_oauth_state_rejects_a_different_guild_or_member() -> None:
    states = TikTokOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = states.issue(guild_id=111, member_id=222)

    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=999, member_id=222)
    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=111, member_id=999)

    states.consume(state, guild_id=111, member_id=222)


def test_tiktok_oauth_state_expires() -> None:
    clock = {"now": NOW}
    states = TikTokOAuthStates(
        _TEST_SIGNING_KEY, now=lambda: clock["now"], ttl=timedelta(minutes=10)
    )
    state = states.issue(guild_id=111, member_id=222)

    clock["now"] = NOW + timedelta(minutes=11)
    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=111, member_id=222)


def test_tiktok_oauth_state_rejects_a_tampered_token() -> None:
    states = TikTokOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = states.issue(guild_id=111, member_id=222)
    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")

    with pytest.raises(ValueError, match="used or expired"):
        states.consume(tampered, guild_id=111, member_id=222)


# --------------------------------------------------------------------------------
# OAuth grant sealing and secret redaction
# --------------------------------------------------------------------------------


def test_tiktok_oauth_grant_round_trips_through_the_credential_vault_without_plaintext() -> None:
    vault = CredentialVault.from_env_key(_VAULT_KEY)
    grant = TikTokOAuthGrant(
        access_token="tiktok-secret-access-token",
        refresh_token="tiktok-secret-refresh-token",
        expires_at=NOW + timedelta(days=1),
    )

    sealed = seal_oauth_grant(vault, grant)

    assert "tiktok-secret-access-token" not in sealed
    assert "tiktok-secret-refresh-token" not in sealed
    reopened = open_oauth_grant(vault, sealed)
    assert reopened == grant


def test_tiktok_oauth_grant_repr_and_str_never_leak_the_tokens() -> None:
    grant = TikTokOAuthGrant(
        access_token="tiktok-secret-access-token",
        refresh_token="tiktok-secret-refresh-token",
        expires_at=NOW,
    )
    assert "tiktok-secret-access-token" not in repr(grant)
    assert "tiktok-secret-access-token" not in str(grant)
    assert "tiktok-secret-refresh-token" not in repr(grant)
    assert "tiktok-secret-refresh-token" not in str(grant)


# --------------------------------------------------------------------------------
# Authorization-code exchange: the concrete production HTTP call
# --------------------------------------------------------------------------------


class _FakeTokenResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _FakeTokenResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        return self.payload


class _FakeTokenSession:
    def __init__(self, responses: list[_FakeTokenResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> _FakeTokenResponse:
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


TIKTOK_TOKEN_RESPONSE_FIXTURE = {
    "access_token": "tiktok-real-access-token",
    "expires_in": 86_400,
    "open_id": "open-id-1",
    "refresh_expires_in": 31_536_000,
    "refresh_token": "tiktok-real-refresh-token",
    "scope": "video.list,user.info.basic",
    "token_type": "Bearer",
}


@pytest.mark.asyncio
async def test_exchange_authorization_code_maps_a_successful_token_response() -> None:
    session = _FakeTokenSession([_FakeTokenResponse(200, TIKTOK_TOKEN_RESPONSE_FIXTURE)])

    grant = await exchange_authorization_code(
        session,
        code="auth-code-abc",
        client_key="client-key",
        client_secret="client-secret",
        redirect_uri="https://bot.example.com/callbacks/tiktok/oauth",
        now=lambda: NOW,
    )

    assert grant.access_token == "tiktok-real-access-token"
    assert grant.refresh_token == "tiktok-real-refresh-token"
    assert grant.expires_at == NOW + timedelta(seconds=86_400)

    url, kwargs = session.requests[0]
    assert url == TIKTOK_TOKEN_URL
    data = kwargs["data"]
    assert data == {
        "client_key": "client-key",
        "client_secret": "client-secret",
        "code": "auth-code-abc",
        "grant_type": "authorization_code",
        "redirect_uri": "https://bot.example.com/callbacks/tiktok/oauth",
    }


@pytest.mark.asyncio
async def test_exchange_authorization_code_raises_authorization_on_invalid_grant() -> None:
    payload = {
        "error": "invalid_grant",
        "error_description": "Authorization code is invalid or expired.",
    }
    session = _FakeTokenSession([_FakeTokenResponse(400, payload)])

    with pytest.raises(TikTokConnectorError) as excinfo:
        await exchange_authorization_code(
            session,
            code="already-used-code",
            client_key="client-key",
            client_secret="client-secret",
            redirect_uri="https://bot.example.com/callbacks/tiktok/oauth",
            now=lambda: NOW,
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.AUTHORIZATION


@pytest.mark.asyncio
async def test_exchange_authorization_code_raises_rate_limited_on_throttling() -> None:
    payload = {"error": "rate_limit_exceeded", "error_description": "Too many requests"}
    session = _FakeTokenSession([_FakeTokenResponse(429, payload)])

    with pytest.raises(TikTokConnectorError) as excinfo:
        await exchange_authorization_code(
            session,
            code="auth-code-abc",
            client_key="client-key",
            client_secret="client-secret",
            redirect_uri="https://bot.example.com/callbacks/tiktok/oauth",
            now=lambda: NOW,
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_exchange_authorization_code_raises_invalid_response_without_access_token() -> None:
    session = _FakeTokenSession([_FakeTokenResponse(200, {"token_type": "Bearer"})])

    with pytest.raises(TikTokConnectorError) as excinfo:
        await exchange_authorization_code(
            session,
            code="auth-code-abc",
            client_key="client-key",
            client_secret="client-secret",
            redirect_uri="https://bot.example.com/callbacks/tiktok/oauth",
            now=lambda: NOW,
        )
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


# --------------------------------------------------------------------------------
# Web ingress: OAuth redirect route
# --------------------------------------------------------------------------------


async def test_oauth_redirect_route_exchanges_code_only_for_a_valid_state() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from krubit.integrations.tiktok import build_tiktok_oauth_redirect_route
    from krubit.web.callbacks import CallbackServer

    states = TikTokOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    authorized: list[tuple[int, int, TikTokOAuthGrant]] = []

    async def exchange_code(code: str) -> TikTokOAuthGrant:
        return TikTokOAuthGrant(access_token=f"token-for-{code}")

    async def on_authorized(guild_id: int, member_id: int, grant: TikTokOAuthGrant) -> None:
        authorized.append((guild_id, member_id, grant))

    route = build_tiktok_oauth_redirect_route(
        path="/callbacks/tiktok/oauth",
        oauth_states=states,
        exchange_code=exchange_code,
        on_authorized=on_authorized,
    )
    server = CallbackServer(
        public_base_url="https://callbacks.example.com", port=8443, routes=(route,)
    )
    app = server.build_app()
    client = TestClient[web.Request, web.Application](TestServer(app))
    await client.start_server()
    try:
        state = states.issue(guild_id=111, member_id=222)
        response = await client.get(
            "/callbacks/tiktok/oauth",
            params={"guild_id": "111", "member_id": "222", "state": state, "code": "abc123"},
        )
        assert response.status == 200
        assert len(authorized) == 1
        guild_id, member_id, grant = authorized[0]
        assert (guild_id, member_id) == (111, 222)
        assert grant.access_token == "token-for-abc123"

        replay = await client.get(
            "/callbacks/tiktok/oauth",
            params={"guild_id": "111", "member_id": "222", "state": state, "code": "abc123"},
        )
        assert replay.status == 400
        assert len(authorized) == 1
    finally:
        await client.close()


# --------------------------------------------------------------------------------
# TikTok identity resolution: independently-confirmed username
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_authorized_identity_returns_open_id_and_username() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": {"user": {"open_id": "open-123", "username": "creator_handle"}},
                    "error": {"code": "ok", "message": "", "log_id": "log-identity-1"},
                },
            )
        ]
    )
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    identity = await connector.fetch_authorized_identity()
    assert identity.open_id == "open-123"
    assert identity.username == "creator_handle"


@pytest.mark.asyncio
async def test_fetch_authorized_identity_raises_on_missing_open_id() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": {"user": {"username": "creator_handle"}},
                    "error": {"code": "ok", "message": "", "log_id": "log-identity-2"},
                },
            )
        ]
    )
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    with pytest.raises(TikTokConnectorError) as excinfo:
        await connector.fetch_authorized_identity()
    assert excinfo.value.failure.kind is ConnectorFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_authorized_identity_handles_missing_username() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": {"user": {"open_id": "open-123"}},
                    "error": {"code": "ok", "message": "", "log_id": "log-identity-3"},
                },
            )
        ]
    )
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    identity = await connector.fetch_authorized_identity()
    assert identity.open_id == "open-123"
    assert identity.username is None


@pytest.mark.asyncio
async def test_fetch_authorized_identity_nullifies_blank_username() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": {"user": {"open_id": "open-123", "username": "   "}},
                    "error": {"code": "ok", "message": "", "log_id": "log-identity-4"},
                },
            )
        ]
    )
    connector = TikTokConnector(session, "tiktok-access-token", now=lambda: NOW)
    identity = await connector.fetch_authorized_identity()
    assert identity.open_id == "open-123"
    assert identity.username is None
