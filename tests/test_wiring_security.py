"""Cross-cutting security properties that need the fully assembled route set
(every route registered together via `build_callback_routes`) rather than any
single component tested in isolation. Coverage that already lives in
`tests/test_wiring_oauth.py`, `tests/test_wiring_deauthorization.py`,
`tests/test_callback_ingress.py`, and `tests/test_meta_signed_request.py` is
intentionally not repeated here.
"""

from __future__ import annotations

import logging
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
        callback_public_base_url="https://example.test", callback_port=8080,
        credential_encryption_key="a" * 32,
        tiktok_client_key="ck", tiktok_client_secret="cs",
        meta_app_id="app-1", meta_app_secret="app-secret",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path: Path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_failing_exchange_never_leaks_token_in_response_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)

    # The route handler under test calls `datetime.now(UTC)` internally (it has
    # no injectable clock), so the attempt must be issued relative to the real
    # wall clock rather than a hardcoded past timestamp -- otherwise the token
    # is already expired by the time the HTTP round-trip below consumes it.
    # (Matches the convention established in tests/test_wiring_oauth.py.)
    now = datetime.now(UTC)
    from krubit.domain.creator_signals import CreatorAccount, Platform

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
        capability="account", redirect_uri="https://example.test/callbacks/tiktok/authorize",
        now=now, ttl=timedelta(minutes=10),
    )

    async def failing_exchange(*args: object, **kwargs: object) -> object:
        raise RuntimeError("token exchange failed for secret-token-abc123")

    monkeypatch.setattr(
        "krubit.integrations.tiktok.exchange_authorization_code", failing_exchange
    )

    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        with caplog.at_level(logging.ERROR):
            response = await client.get(
                "/callbacks/tiktok/authorize", params={"code": "authcode", "state": token}
            )
        body = await response.text()
        assert "secret-token-abc123" not in body
        assert all("secret-token-abc123" not in r.message for r in caplog.records)
    await store.close()


async def test_second_callback_server_start_binds_nothing_extra(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    await server.start()
    runner = server._runner
    await server.start()
    assert server._runner is runner
    await server.close()
    await store.close()


async def test_partial_bind_failure_leaves_runner_unset(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    vault = CredentialVault.from_env_key("a" * 32)
    routes = build_callback_routes(_settings(), store, vault, object())
    occupied = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    await occupied.start()
    # aiohttp's AppRunner.addresses returns a list of the underlying sockets'
    # getsockname() tuples; for a TCP site bound to 127.0.0.1 that is
    # ("127.0.0.1", port). Verified against the aiohttp==3.14.3 pinned in
    # pyproject.toml/uv.lock, where BaseRunner.addresses is exactly this shape.
    bound_port = occupied._runner.addresses[0][1]  # type: ignore[union-attr]

    colliding = CallbackServer(
        public_base_url="https://example.test", port=bound_port, routes=routes
    )
    with pytest.raises(OSError):
        await colliding.start()
    assert colliding._runner is None

    await occupied.close()
    await store.close()
