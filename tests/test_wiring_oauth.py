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
