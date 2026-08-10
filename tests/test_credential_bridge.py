"""Unit tests for krubit.integrations.credential_bridge.
CredentialResolvingConnector -- resolves a per-account OAuth token from
storage on every call, rather than the fixed-at-construction token every
other connector in this codebase uses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    creator_account_id,
)
from krubit.integrations.base import (
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)
from krubit.integrations.credential_bridge import CredentialResolvingConnector
from krubit.security.credential_vault import CredentialVault
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
GUILD_ID = 111
VAULT_KEY = "test-key-for-credential-bridge-tests"


class _FakeInnerConnectorError(RuntimeError):
    def __init__(self, failure: ConnectorFailure) -> None:
        super().__init__(failure.kind.value)
        self.failure = failure


class _FakeInnerConnector:
    """Records the access token it was constructed with, so tests can
    assert the bridge resolved and passed through the right one."""

    def __init__(self, session: object, access_token: str) -> None:
        self.session = session
        self.access_token = access_token

    async def resolve_account(self, recognized: object) -> ConnectorAccount:  # pragma: no cover
        raise NotImplementedError

    async def fetch_page(self, account: CreatorAccount, *, cursor: str | None) -> ConnectorPage:
        return ConnectorPage(items=(), next_cursor=None)

    async def health(self, account: CreatorAccount | None = None) -> ConnectorHealth:
        return ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.READY)


def _account(account_id: str) -> CreatorAccount:
    return CreatorAccount(
        guild_id=GUILD_ID,
        account_id=account_id,
        owner_member_id=222,
        platform=Platform.INSTAGRAM,
        handle="examplecreator",
        canonical_url="https://www.instagram.com/examplecreator",
        external_id="ig-external-id",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault.from_env_key(VAULT_KEY)


def _bridge(store: SQLiteStore, vault: CredentialVault) -> CredentialResolvingConnector:
    return CredentialResolvingConnector(
        object(),
        store,
        vault,
        platform=Platform.INSTAGRAM,
        inner_connector_factory=_FakeInnerConnector,
        error_type=_FakeInnerConnectorError,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_no_authorization_exists(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_status_is_not_active(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    await store.save_creator_account(_account(account_id))
    secret_ref = vault.seal_json(
        {"access_token": "tok-123", "refresh_token": None, "expires_at": None}
    )
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="revoked",
        expires_at=None,
        now=NOW,
    )
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_raises_authorization_failure_when_expired(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    await store.save_creator_account(_account(account_id))
    secret_ref = vault.seal_json(
        {"access_token": "tok-123", "refresh_token": None, "expires_at": None}
    )
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=NOW - timedelta(minutes=1),
        now=NOW,
    )
    bridge = _bridge(store, vault)

    with pytest.raises(_FakeInnerConnectorError) as exc_info:
        await bridge.fetch_page(_account(account_id), cursor=None)
    assert exc_info.value.failure.kind.value == "authorization"


@pytest.mark.asyncio
async def test_fetch_page_delegates_to_inner_connector_with_the_resolved_token(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    await store.save_creator_account(_account(account_id))
    secret_ref = vault.seal_json(
        {"access_token": "the-real-token", "refresh_token": None, "expires_at": None}
    )
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=None,
        now=NOW,
    )
    bridge = _bridge(store, vault)

    result = await bridge.fetch_page(_account(account_id), cursor=None)

    assert result == ConnectorPage(items=(), next_cursor=None)


@pytest.mark.asyncio
async def test_fetch_page_accepts_an_authorization_with_no_expiry_and_a_future_expiry(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    await store.save_creator_account(_account(account_id))
    secret_ref = vault.seal_json(
        {"access_token": "tok-123", "refresh_token": None, "expires_at": None}
    )
    await store.save_connector_authorization(
        guild_id=GUILD_ID,
        account_id=account_id,
        capability=Capability.SOCIAL.value,
        secret_ref=secret_ref,
        provider_resource_id="ig-external-id",
        authorization_subject_id="ig-external-id",
        status="active",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    bridge = _bridge(store, vault)

    result = await bridge.fetch_page(_account(account_id), cursor=None)

    assert result == ConnectorPage(items=(), next_cursor=None)


@pytest.mark.asyncio
async def test_health_reports_authorization_required_without_fetching_when_no_authorization(
    store: SQLiteStore, vault: CredentialVault
) -> None:
    account_id = creator_account_id(Platform.INSTAGRAM, "ig-external-id")
    bridge = _bridge(store, vault)

    health = await bridge.health(_account(account_id))

    assert health.state.value == "authorization_required"
