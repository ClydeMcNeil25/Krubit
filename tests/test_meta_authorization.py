from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krubit.domain.creator_signals import Capability, Platform, RecognizedAccountUrl
from krubit.integrations.meta import (
    MetaOAuthGrant,
    MetaOAuthStates,
    build_meta_deauthorization_route,
    build_meta_oauth_redirect_route,
    open_oauth_grant,
    seal_oauth_grant,
    verify_meta_signed_request,
)
from krubit.security.credential_vault import CredentialVault
from krubit.services.creator_registry import CreatorAuthorityError, CreatorRegistry
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_TEST_SIGNING_KEY = "test-only-meta-oauth-state-signing-key"

oauth_states = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)


def test_meta_oauth_state_is_guild_member_platform_bound_and_single_use() -> None:
    state = oauth_states.issue(guild_id=111, member_id=222, platform=Platform.INSTAGRAM)
    assert oauth_states.consume(state, guild_id=111, member_id=222) is Platform.INSTAGRAM
    with pytest.raises(ValueError, match="used or expired"):
        oauth_states.consume(state, guild_id=111, member_id=222)


def test_meta_oauth_state_rejects_a_different_guild_or_member() -> None:
    states = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = states.issue(guild_id=111, member_id=222, platform=Platform.THREADS)

    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=999, member_id=222)
    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=111, member_id=999)

    # The state is still valid and unconsumed for the guild/member it was issued to.
    assert states.consume(state, guild_id=111, member_id=222) is Platform.THREADS


def test_meta_oauth_state_expires() -> None:
    clock = {"now": NOW}
    states = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: clock["now"], ttl=timedelta(minutes=10))
    state = states.issue(guild_id=111, member_id=222, platform=Platform.FACEBOOK_PAGE)

    clock["now"] = NOW + timedelta(minutes=11)
    with pytest.raises(ValueError, match="used or expired"):
        states.consume(state, guild_id=111, member_id=222)


def test_meta_oauth_state_rejects_a_tampered_token() -> None:
    states = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = states.issue(guild_id=111, member_id=222, platform=Platform.FACEBOOK)
    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")

    with pytest.raises(ValueError, match="used or expired"):
        states.consume(tampered, guild_id=111, member_id=222)


def test_meta_oauth_state_from_a_different_signing_key_is_rejected() -> None:
    issuer = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    state = issuer.issue(guild_id=111, member_id=222, platform=Platform.INSTAGRAM)

    other = MetaOAuthStates("a-completely-different-signing-key", now=lambda: NOW)
    with pytest.raises(ValueError, match="used or expired"):
        other.consume(state, guild_id=111, member_id=222)


# --------------------------------------------------------------------------------
# OAuth grant sealing
# --------------------------------------------------------------------------------

_VAULT_KEY = "test-only-credential-encryption-key-material"


def test_oauth_grant_round_trips_through_the_credential_vault_without_plaintext() -> None:
    vault = CredentialVault.from_env_key(_VAULT_KEY)
    grant = MetaOAuthGrant(
        access_token="ig-secret-access-token",
        refresh_token="ig-secret-refresh-token",
        expires_at=NOW + timedelta(days=60),
    )

    sealed = seal_oauth_grant(vault, grant)

    assert "ig-secret-access-token" not in sealed
    assert "ig-secret-refresh-token" not in sealed
    reopened = open_oauth_grant(vault, sealed)
    assert reopened == grant


def test_oauth_grant_without_refresh_token_or_expiry_round_trips() -> None:
    vault = CredentialVault.from_env_key(_VAULT_KEY)
    grant = MetaOAuthGrant(access_token="threads-secret-access-token")

    reopened = open_oauth_grant(vault, seal_oauth_grant(vault, grant))
    assert reopened == grant


# --------------------------------------------------------------------------------
# CreatorRegistry: OAuth authorization completion
# --------------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def registry(store: SQLiteStore) -> CreatorRegistry:
    return CreatorRegistry(store)


@pytest.mark.asyncio
async def test_record_oauth_authorization_unpauses_account_and_records_a_redacted_receipt(
    store: SQLiteStore, registry: CreatorRegistry
) -> None:
    recognized = RecognizedAccountUrl(
        platform=Platform.INSTAGRAM,
        handle="krucialstudios",
        canonical_url="https://www.instagram.com/krucialstudios",
    )
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=recognized,
        resolved_external_id="ig-1",
        now=NOW,
    )
    assert account.paused is True

    authorized = await registry.record_oauth_authorization(
        guild_id=111,
        actor_member_id=222,
        account_id=account.account_id,
        actor_is_admin=False,
        actor_has_creator_role=True,
        platform=Platform.INSTAGRAM,
        capability=Capability.SOCIAL,
        expires_at=NOW + timedelta(days=60),
        now=NOW,
    )
    assert authorized.paused is False

    receipts = await store.list_creator_registry_receipts(111, account.account_id)
    authorize_receipt = next(r for r in receipts if r.action == "authorize_connector")
    assert authorize_receipt.detail["capability"] == Capability.SOCIAL.value
    assert "access_token" not in authorize_receipt.detail
    assert "secret" not in authorize_receipt.detail


@pytest.mark.asyncio
async def test_record_oauth_authorization_rejects_a_platform_mismatch(
    registry: CreatorRegistry,
) -> None:
    recognized = RecognizedAccountUrl(
        platform=Platform.INSTAGRAM,
        handle="krucialstudios",
        canonical_url="https://www.instagram.com/krucialstudios",
    )
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=recognized,
        resolved_external_id="ig-1",
        now=NOW,
    )

    with pytest.raises(ValueError, match="not"):
        await registry.record_oauth_authorization(
            guild_id=111,
            actor_member_id=222,
            account_id=account.account_id,
            actor_is_admin=False,
            actor_has_creator_role=True,
            platform=Platform.THREADS,
            capability=Capability.SOCIAL,
            expires_at=None,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_record_oauth_authorization_rejects_a_non_owner_without_admin_authority(
    registry: CreatorRegistry,
) -> None:
    recognized = RecognizedAccountUrl(
        platform=Platform.INSTAGRAM,
        handle="krucialstudios",
        canonical_url="https://www.instagram.com/krucialstudios",
    )
    account = await registry.add_account(
        guild_id=111,
        actor_member_id=222,
        owner_member_id=222,
        actor_is_admin=False,
        actor_has_creator_role=True,
        recognized=recognized,
        resolved_external_id="ig-1",
        now=NOW,
    )

    with pytest.raises(CreatorAuthorityError, match="administrator authority"):
        await registry.record_oauth_authorization(
            guild_id=111,
            actor_member_id=333,
            account_id=account.account_id,
            actor_is_admin=False,
            actor_has_creator_role=True,
            platform=Platform.INSTAGRAM,
            capability=Capability.SOCIAL,
            expires_at=None,
            now=NOW,
        )


# --------------------------------------------------------------------------------
# Web ingress: OAuth redirect route and signed deauthorization webhook
# --------------------------------------------------------------------------------


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_verify_meta_signed_request_accepts_only_a_matching_signature() -> None:
    body = b'{"object":"user","entry":[]}'
    assert verify_meta_signed_request(body, _sign(body, "app-secret"), "app-secret") is True
    assert verify_meta_signed_request(body, _sign(body, "wrong-secret"), "app-secret") is False
    assert verify_meta_signed_request(body, None, "app-secret") is False
    assert verify_meta_signed_request(body, "sha1=deadbeef", "app-secret") is False


@pytest.fixture
async def deauthorization_client() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    received: list[bytes] = []

    async def handle_deauthorization(body: bytes) -> None:
        received.append(body)

    route = build_meta_deauthorization_route(
        path="/callbacks/meta/deauthorize",
        app_secret="app-secret",
        handle_deauthorization=handle_deauthorization,
    )
    server = CallbackServer(
        public_base_url="https://callbacks.example.com", port=8443, routes=(route,)
    )
    app = server.build_app()
    app["received"] = received
    client = TestClient[web.Request, web.Application](TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_deauthorization_webhook_rejects_an_unsigned_or_wrong_signature_body(
    deauthorization_client: TestClient[web.Request, web.Application],
) -> None:
    body = b'{"object":"user","entry":[]}'
    response = await deauthorization_client.post(
        "/callbacks/meta/deauthorize",
        data=body,
        headers={"X-Hub-Signature-256": _sign(body, "wrong-secret")},
    )
    assert response.status == 403
    assert deauthorization_client.app["received"] == []


async def test_deauthorization_webhook_accepts_a_correctly_signed_body(
    deauthorization_client: TestClient[web.Request, web.Application],
) -> None:
    body = b'{"object":"user","entry":[{"id":"ig-1"}]}'
    response = await deauthorization_client.post(
        "/callbacks/meta/deauthorize",
        data=body,
        headers={"X-Hub-Signature-256": _sign(body, "app-secret")},
    )
    assert response.status == 204
    assert deauthorization_client.app["received"] == [body]


@pytest.fixture
async def redirect_client() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    states = MetaOAuthStates(_TEST_SIGNING_KEY, now=lambda: NOW)
    authorized: list[tuple[int, int, Platform, MetaOAuthGrant]] = []

    async def exchange_code(code: str, platform: Platform) -> MetaOAuthGrant:
        return MetaOAuthGrant(access_token=f"token-for-{code}")

    async def on_authorized(
        guild_id: int, member_id: int, platform: Platform, grant: MetaOAuthGrant
    ) -> None:
        authorized.append((guild_id, member_id, platform, grant))

    route = build_meta_oauth_redirect_route(
        path="/callbacks/meta/oauth",
        oauth_states=states,
        exchange_code=exchange_code,
        on_authorized=on_authorized,
    )
    server = CallbackServer(
        public_base_url="https://callbacks.example.com", port=8443, routes=(route,)
    )
    app = server.build_app()
    app["states"] = states
    app["authorized"] = authorized
    client = TestClient[web.Request, web.Application](TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_oauth_redirect_route_exchanges_code_only_for_a_valid_state(
    redirect_client: TestClient[web.Request, web.Application],
) -> None:
    states: MetaOAuthStates = redirect_client.app["states"]
    state = states.issue(guild_id=111, member_id=222, platform=Platform.INSTAGRAM)

    response = await redirect_client.get(
        "/callbacks/meta/oauth",
        params={"guild_id": "111", "member_id": "222", "state": state, "code": "abc123"},
    )
    assert response.status == 200

    authorized = redirect_client.app["authorized"]
    assert len(authorized) == 1
    guild_id, member_id, platform, grant = authorized[0]
    assert (guild_id, member_id, platform) == (111, 222, Platform.INSTAGRAM)
    assert grant.access_token == "token-for-abc123"


async def test_oauth_redirect_route_rejects_a_reused_state(
    redirect_client: TestClient[web.Request, web.Application],
) -> None:
    states: MetaOAuthStates = redirect_client.app["states"]
    state = states.issue(guild_id=111, member_id=222, platform=Platform.THREADS)

    params = {"guild_id": "111", "member_id": "222", "state": state, "code": "abc123"}
    first = await redirect_client.get("/callbacks/meta/oauth", params=params)
    assert first.status == 200

    second = await redirect_client.get("/callbacks/meta/oauth", params=params)
    assert second.status == 400
    assert len(redirect_client.app["authorized"]) == 1


async def test_oauth_redirect_route_rejects_a_state_bound_to_a_different_guild(
    redirect_client: TestClient[web.Request, web.Application],
) -> None:
    states: MetaOAuthStates = redirect_client.app["states"]
    state = states.issue(guild_id=111, member_id=222, platform=Platform.FACEBOOK_PAGE)

    response = await redirect_client.get(
        "/callbacks/meta/oauth",
        params={"guild_id": "999", "member_id": "222", "state": state, "code": "abc123"},
    )
    assert response.status == 400
    assert redirect_client.app["authorized"] == []
