from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.config import Settings
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object) -> Settings:
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test",
        callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck", tiktok_client_secret="cs",
        meta_app_id=None, meta_app_secret=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_build_callback_routes_returns_nothing_when_signals_disabled(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(
        _settings(creator_signals_enabled=False), store, vault, object()
    )
    assert routes == ()
    await store.close()


async def test_build_callback_routes_returns_nothing_without_callback_config(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(
        _settings(callback_public_base_url=None), store, vault, object()
    )
    assert routes == ()
    await store.close()


async def test_build_callback_routes_registers_tiktok_authorize_when_vault_present(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/tiktok/authorize", "GET") in paths
    await store.close()


async def test_build_callback_routes_omits_tiktok_authorize_without_vault(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/tiktok/authorize", "GET") not in paths
    await store.close()


async def test_tiktok_authorize_route_rejects_reused_state(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    # The route handler under test calls `datetime.now(UTC)` internally (it has no
    # injectable clock), so the attempt must be issued relative to the real wall
    # clock rather than a hardcoded past timestamp -- otherwise the token is
    # already expired by the time the HTTP round-trip below consumes it.
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="tiktok:acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="tiktok:acct-1", platform="tiktok",
        capability="account",
        redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.tiktok import TikTokOAuthGrant
        return TikTokOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr(
        "krubit.integrations.tiktok.exchange_authorization_code", fake_exchange
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.tiktok import TikTokIdentity
        return TikTokIdentity(open_id="open-1", username="creator_handle")

    monkeypatch.setattr(
        "krubit.integrations.tiktok.TikTokConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(
        public_base_url="https://example.test", port=0, routes=routes
    )
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        first = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert first.status == 200
        second = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert second.status == 400

    saved = await store.get_connector_authorization(1, "tiktok:acct-1", "account")
    assert saved is not None
    assert saved.authorization_subject_id == "open-1"
    await store.close()


async def test_meta_authorize_route_instagram_end_to_end(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    # Mirrors test_tiktok_authorize_route_rejects_reused_state: the handler calls
    # datetime.now(UTC) internally, so the attempt must be issued relative to the
    # real wall clock rather than a hardcoded past timestamp.
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="instagram:acct-1", owner_member_id=2,
            platform=Platform.INSTAGRAM, handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="instagram:acct-1", platform="instagram",
        capability="account",
        redirect_uri="https://example.test/callbacks/meta/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr(
        "krubit.integrations.meta.exchange_authorization_code", fake_exchange
    )

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.INSTAGRAM,
            external_id="ig-external-id",
            handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.resolve_account",
        fake_resolve_account,
    )

    async def fake_fetch_authorized_identity(self: object) -> object:
        from krubit.integrations.meta import MetaIdentity
        return MetaIdentity(external_id="ig-external-id", username="creator_handle")

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.fetch_authorized_identity",
        fake_fetch_authorized_identity,
    )

    async def fake_fetch_authorizing_user_id(*args: object, **kwargs: object) -> str:
        return "authorizing-user-id"

    monkeypatch.setattr(
        "krubit.integrations.meta.fetch_authorizing_user_id",
        fake_fetch_authorizing_user_id,
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"),
        store, vault, object(),
    )
    server = CallbackServer(
        public_base_url="https://example.test", port=0, routes=routes
    )
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 200
        body = await response.text()
        assert "Authorization complete" in body

    saved = await store.get_connector_authorization(1, "instagram:acct-1", "account")
    assert saved is not None
    assert saved.provider_resource_id == "ig-external-id"
    assert saved.authorization_subject_id == "authorizing-user-id"
    assert saved.provider_resource_id != saved.authorization_subject_id
    await store.close()


async def test_tiktok_authorize_route_rejects_missing_username(tmp_path, monkeypatch):
    """IMPORTANT FIX #3: `identity.username is None` (no `user.info.profile` scope
    granted) must be a REJECTION, not a silently-skipped check -- otherwise the
    grant is saved unverified, undoing the entire point of `fetch_authorized_
    identity` sourcing an independently-confirmed username.
    """
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="tiktok:acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="tiktok:acct-1", platform="tiktok",
        capability="account",
        redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.tiktok import TikTokOAuthGrant
        return TikTokOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr(
        "krubit.integrations.tiktok.exchange_authorization_code", fake_exchange
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.tiktok import TikTokIdentity
        # No `user.info.profile` scope granted -- username is None.
        return TikTokIdentity(open_id="open-1", username=None)

    monkeypatch.setattr(
        "krubit.integrations.tiktok.TikTokConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert await store.get_connector_authorization(1, "tiktok:acct-1", "account") is None
    await store.close()


async def test_tiktok_authorize_route_uses_redirect_uri_from_consumed_attempt(
    tmp_path, monkeypatch
):
    """IMPORTANT FIX #6: the redirect_uri passed to the token exchange must be the
    value stored on the consumed oauth_attempts row, not one recomputed from
    current settings -- proven here by issuing an attempt with a deliberately
    different redirect_uri than the route would compute.
    """
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="tiktok:acct-1", owner_member_id=2,
            platform=Platform.TIKTOK, handle="creator_handle",
            canonical_url="https://tiktok.com/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    stored_redirect_uri = "https://old-domain.example/callbacks/tiktok/authorize"
    token = await store.issue_oauth_attempt(
        guild_id=1, member_id=2, account_id="tiktok:acct-1", platform="tiktok",
        capability="account",
        redirect_uri=stored_redirect_uri,
        now=now, ttl=timedelta(minutes=10),
    )

    captured_kwargs: dict[str, object] = {}

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.tiktok import TikTokOAuthGrant
        captured_kwargs.update(kwargs)
        return TikTokOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr(
        "krubit.integrations.tiktok.exchange_authorization_code", fake_exchange
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.tiktok import TikTokIdentity
        return TikTokIdentity(open_id="open-1", username="creator_handle")

    monkeypatch.setattr(
        "krubit.integrations.tiktok.TikTokConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    # settings.callback_public_base_url is "https://example.test", so the
    # route-build-time-computed redirect_uri would be
    # "https://example.test/callbacks/tiktok/authorize" -- different from
    # stored_redirect_uri above.
    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 200

    assert captured_kwargs["redirect_uri"] == stored_redirect_uri
    await store.close()


def _issue_meta_attempt(
    store: SQLiteStore,
    *,
    guild_id: int,
    member_id: int,
    account_id: str,
    platform: str,
    now: datetime,
    redirect_uri: str = "https://example.test/callbacks/meta/authorize",
):
    return store.issue_oauth_attempt(
        guild_id=guild_id, member_id=member_id, account_id=account_id, platform=platform,
        capability="account", redirect_uri=redirect_uri, now=now, ttl=timedelta(minutes=10),
    )


async def test_meta_authorize_route_instagram_rejects_username_mismatch(tmp_path, monkeypatch):
    """CRITICAL FIX #2: an Instagram authorization whose Graph-confirmed username
    does not match the account's registered handle must be rejected, and nothing
    written to connector_authorizations."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="instagram:acct-1", owner_member_id=2,
            platform=Platform.INSTAGRAM, handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="instagram:acct-1",
        platform="instagram", now=now,
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.INSTAGRAM,
            external_id="ig-external-id",
            handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.resolve_account", fake_resolve_account
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.meta import MetaIdentity
        # A different Instagram account authorized than the one registered.
        return MetaIdentity(external_id="ig-external-id", username="someone_else")

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert (
        await store.get_connector_authorization(1, "instagram:acct-1", "account") is None
    )
    await store.close()


async def test_meta_authorize_route_instagram_rejects_missing_username(tmp_path, monkeypatch):
    """CRITICAL FIX #2: a missing `username` (e.g. no scope granted) must be a hard
    rejection, never a silent fall-through to accepting the authorization."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="instagram:acct-1", owner_member_id=2,
            platform=Platform.INSTAGRAM, handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="instagram:acct-1",
        platform="instagram", now=now,
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.INSTAGRAM,
            external_id="ig-external-id",
            handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.resolve_account", fake_resolve_account
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.meta import MetaIdentity
        return MetaIdentity(external_id="ig-external-id", username=None)

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert (
        await store.get_connector_authorization(1, "instagram:acct-1", "account") is None
    )
    await store.close()


async def test_meta_authorize_route_threads_rejects_username_mismatch(tmp_path, monkeypatch):
    """CRITICAL FIX #2: same fail-closed requirement as Instagram, for Threads."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="threads:acct-1", owner_member_id=2,
            platform=Platform.THREADS, handle="creator_handle",
            canonical_url="https://threads.net/@creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="threads:acct-1",
        platform="threads", now=now,
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.THREADS,
            external_id="threads-external-id",
            handle="creator_handle",
            canonical_url="https://threads.net/@creator_handle",
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.ThreadsConnector.resolve_account", fake_resolve_account
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.meta import MetaIdentity
        return MetaIdentity(external_id="threads-external-id", username="someone_else")

    monkeypatch.setattr(
        "krubit.integrations.meta.ThreadsConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert await store.get_connector_authorization(1, "threads:acct-1", "account") is None
    await store.close()


async def test_meta_authorize_route_facebook_page_rejects_external_id_mismatch(
    tmp_path, monkeypatch
):
    """CRITICAL FIX #2: FacebookPageConnector.resolve_account unconditionally
    echoes the input handle, so the handle can never distinguish a mismatch for
    this capability. The genuinely Graph-confirmed `external_id` (the Page's own
    numeric id) must be compared against the account's registered external_id
    instead, and a mismatch there must be rejected."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="facebook_page:acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="test_page",
            canonical_url="https://facebook.com/test_page",
            external_id="registered-page-id", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="facebook_page:acct-1",
        platform="facebook_page", now=now,
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        # A DIFFERENT Page id than what was registered -- the handle it echoes
        # back still matches, proving the handle alone is a vacuous check.
        return ConnectorAccount(
            platform=Platform.FACEBOOK_PAGE,
            external_id="a-completely-different-page-id",
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.FacebookPageConnector.resolve_account", fake_resolve_account
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert (
        await store.get_connector_authorization(1, "facebook_page:acct-1", "account") is None
    )
    await store.close()


async def test_meta_authorize_route_facebook_profile_rejects_external_id_mismatch(
    tmp_path, monkeypatch
):
    """CRITICAL FIX #2: same reasoning as the Facebook Page case, for a personal
    profile -- the only Graph-confirmable field is the numeric id, so that is what
    gets compared against the account's registered external_id."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="facebook:acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK, handle="test_profile",
            canonical_url="https://facebook.com/test_profile",
            external_id="registered-profile-id", paused=True, created_at=now, updated_at=now,
        )
    )
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="facebook:acct-1",
        platform="facebook", now=now,
    )

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.FACEBOOK,
            external_id="a-completely-different-profile-id",
            handle=recognized.handle,
            canonical_url=recognized.canonical_url,
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.FacebookProfileConnector.resolve_account",
        fake_resolve_account,
    )

    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 400

    assert await store.get_connector_authorization(1, "facebook:acct-1", "account") is None
    await store.close()


async def test_meta_authorize_route_uses_redirect_uri_from_consumed_attempt(
    tmp_path, monkeypatch
):
    """IMPORTANT FIX #6: same requirement as TikTok's equivalent test, for Meta."""
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    from krubit.domain.creator_signals import CreatorAccount, Platform
    now = datetime.now(UTC)
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="instagram:acct-1", owner_member_id=2,
            platform=Platform.INSTAGRAM, handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
            external_id="creator_handle", paused=True, created_at=now, updated_at=now,
        )
    )
    stored_redirect_uri = "https://old-domain.example/callbacks/meta/authorize"
    token = await _issue_meta_attempt(
        store, guild_id=1, member_id=2, account_id="instagram:acct-1",
        platform="instagram", now=now, redirect_uri=stored_redirect_uri,
    )

    captured_kwargs: dict[str, object] = {}

    async def fake_exchange(*args: object, **kwargs: object) -> object:
        from krubit.integrations.meta import MetaOAuthGrant
        captured_kwargs.update(kwargs)
        return MetaOAuthGrant(access_token="tok", refresh_token=None, expires_at=None)

    monkeypatch.setattr("krubit.integrations.meta.exchange_authorization_code", fake_exchange)

    async def fake_resolve_account(self: object, recognized: object) -> object:
        from krubit.integrations.base import ConnectorAccount
        return ConnectorAccount(
            platform=Platform.INSTAGRAM,
            external_id="ig-external-id",
            handle="creator_handle",
            canonical_url="https://instagram.com/creator_handle",
        )

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.resolve_account", fake_resolve_account
    )

    async def fake_fetch_identity(self: object) -> object:
        from krubit.integrations.meta import MetaIdentity
        return MetaIdentity(external_id="ig-external-id", username="creator_handle")

    monkeypatch.setattr(
        "krubit.integrations.meta.InstagramConnector.fetch_authorized_identity",
        fake_fetch_identity,
    )

    async def fake_fetch_authorizing_user_id(*args: object, **kwargs: object) -> str:
        return "authorizing-user-id"

    monkeypatch.setattr(
        "krubit.integrations.meta.fetch_authorizing_user_id", fake_fetch_authorizing_user_id
    )

    # settings.callback_public_base_url is "https://example.test", so the
    # route-build-time-computed redirect_uri would be
    # "https://example.test/callbacks/meta/authorize" -- different from
    # stored_redirect_uri above.
    routes = build_callback_routes(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), store, vault, object()
    )
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/authorize", params={"code": "authcode", "state": token}
        )
        assert response.status == 200

    assert captured_kwargs["redirect_uri"] == stored_redirect_uri
    await store.close()


async def test_authorize_route_never_redirects_regardless_of_query(tmp_path):
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/tiktok/authorize",
            params={"redirect_uri": "https://evil.test", "next": "https://evil.test"},
            allow_redirects=False,
        )
        assert response.status < 300 or response.status >= 400
        assert "Location" not in response.headers
    await store.close()
