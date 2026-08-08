from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.wiring import build_callback_routes

pytestmark = pytest.mark.asyncio

_SECRET = "app-secret"


def _sign(payload: dict[str, object]) -> str:
    payload_json = json.dumps(payload)
    encoded_payload = (
        base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    )
    signature = hmac.new(
        _SECRET.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def _settings(**overrides: object):
    from krubit.config import Settings
    base = dict(
        application_id=1, bot_token="t", database_path=Path("unused.db"),
        creator_signals_enabled=True,
        callback_public_base_url="https://example.test", callback_port=8080,
        credential_encryption_key=None,
        tiktok_client_key=None, tiktok_client_secret=None,
        meta_app_id="app-1", meta_app_secret=_SECRET,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _store(tmp_path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_deauthorize_routes_register_without_vault(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    paths = {(r.path, r.method) for r in routes}
    assert ("/callbacks/meta/deauthorize", "POST") in paths
    assert ("/callbacks/meta/data-deletion", "POST") in paths
    await store.close()


async def test_deauthorize_removes_matching_rows(tmp_path):
    store = await _store(tmp_path)
    # The handler verifies issued_at against the real wall clock, so the signed
    # payload must be timestamped relative to now, not a fixed past date.
    now = datetime.now(UTC)
    from krubit.domain.creator_signals import CreatorAccount, Platform
    # find_connector_authorizations_by_authorization_subject joins connector_authorizations
    # to creator_accounts on (guild_id, account_id) and filters by creator_accounts.platform,
    # so a matching creator account is required for the deauthorize handler to find this row.
    await store.save_creator_account(
        CreatorAccount(
            guild_id=1, account_id="acct-1", owner_member_id=2,
            platform=Platform.FACEBOOK_PAGE, handle="creator_handle",
            canonical_url="https://facebook.com/creator_handle",
            external_id="page-1", paused=True, created_at=now, updated_at=now,
        )
    )
    await store.save_connector_authorization(
        guild_id=1, account_id="acct-1", capability="content", secret_ref="v1:x",
        provider_resource_id="page-1", authorization_subject_id="user-1",
        status="active", expires_at=None, now=now,
    )
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign(
            {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-1"}
        )
        response = await client.post(
            "/callbacks/meta/deauthorize", data={"signed_request": signed}
        )
        assert response.status == 200
    assert await store.get_connector_authorization(1, "acct-1", "content") is None
    await store.close()


async def test_deauthorize_rejects_bad_signature(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.post(
            "/callbacks/meta/deauthorize", data={"signed_request": "garbage.garbage"}
        )
        assert response.status == 403
    await store.close()


async def test_data_deletion_returns_documented_contract(tmp_path):
    store = await _store(tmp_path)
    now = datetime.now(UTC)  # verified against the real wall clock; see note above
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign(
            {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-2"}
        )
        response = await client.post(
            "/callbacks/meta/data-deletion", data={"signed_request": signed}
        )
        assert response.status == 200
        body = await response.json()
        assert "confirmation_code" in body
        expected_suffix = f"/callbacks/meta/data-deletion/status?id={body['confirmation_code']}"
        assert body["url"].endswith(expected_suffix)

        status_response = await client.get(
            "/callbacks/meta/data-deletion/status", params={"id": body["confirmation_code"]}
        )
        assert status_response.status == 200
        status_body = await status_response.json()
        assert status_body["status"] == "complete"
    await store.close()


async def test_data_deletion_status_unknown_code_is_404(tmp_path):
    store = await _store(tmp_path)
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        response = await client.get(
            "/callbacks/meta/data-deletion/status", params={"id": "unknown"}
        )
        assert response.status == 404
    await store.close()


async def test_repeat_data_deletion_request_reuses_confirmation_code(tmp_path):
    store = await _store(tmp_path)
    now = datetime.now(UTC)  # verified against the real wall clock; see note above
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign(
            {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "user-3"}
        )
        first = await client.post(
            "/callbacks/meta/data-deletion", data={"signed_request": signed}
        )
        second = await client.post(
            "/callbacks/meta/data-deletion", data={"signed_request": signed}
        )
        first_body = await first.json()
        second_body = await second.json()
        assert first_body["confirmation_code"] == second_body["confirmation_code"]
    await store.close()


async def test_data_deletion_on_already_deleted_subject_deletes_zero_rows_without_error(
    tmp_path,
):
    store = await _store(tmp_path)
    now = datetime.now(UTC)  # verified against the real wall clock; see note above
    routes = build_callback_routes(_settings(), store, None, object())
    server = CallbackServer(public_base_url="https://example.test", port=0, routes=routes)
    app = server.build_app()
    async with TestServer(app) as test_server, TestClient(test_server) as client:
        signed = _sign(
            {
                "algorithm": "HMAC-SHA256",
                "issued_at": int(now.timestamp()),
                "user_id": "never-existed",
            }
        )
        response = await client.post(
            "/callbacks/meta/data-deletion", data={"signed_request": signed}
        )
        assert response.status == 200
    await store.close()
