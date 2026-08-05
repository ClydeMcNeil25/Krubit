from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krubit.domain.creator_signals import (
    Capability,
    CapabilityState,
    CreatorAccount,
    Platform,
    RecognizedAccountUrl,
    creator_account_id,
)
from krubit.integrations.base import UnsupportedConnectorError
from krubit.integrations.fanbase import FanbaseConnector

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def fanbase_account(external_id: str = "krucialstudios") -> CreatorAccount:
    return CreatorAccount(
        guild_id=111,
        account_id=creator_account_id(Platform.FANBASE, external_id),
        owner_member_id=222,
        platform=Platform.FANBASE,
        handle="krucialstudios",
        canonical_url="https://fanbase.app/krucialstudios",
        external_id=external_id,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_fanbase_is_recognized_but_never_polled() -> None:
    connector = FanbaseConnector()
    assert (await connector.health()).state is CapabilityState.UNSUPPORTED
    with pytest.raises(UnsupportedConnectorError):
        await connector.fetch_page(fanbase_account(), cursor=None)


@pytest.mark.asyncio
async def test_fanbase_health_reports_unsupported_for_social_capability() -> None:
    connector = FanbaseConnector()
    health = await connector.health()
    assert health.capability is Capability.SOCIAL
    assert health.state is CapabilityState.UNSUPPORTED


@pytest.mark.asyncio
async def test_fanbase_resolve_account_recognizes_metadata_without_a_network_call() -> None:
    connector = FanbaseConnector()
    recognized = RecognizedAccountUrl(
        platform=Platform.FANBASE,
        handle="krucialstudios",
        canonical_url="https://fanbase.app/krucialstudios",
    )
    account = await connector.resolve_account(recognized)
    assert account.platform is Platform.FANBASE
    assert account.handle == "krucialstudios"
    assert account.canonical_url == "https://fanbase.app/krucialstudios"


@pytest.mark.asyncio
async def test_fanbase_has_no_underlying_session_or_credential_attributes() -> None:
    connector = FanbaseConnector()
    assert not hasattr(connector, "_session")
    assert not hasattr(connector, "_access_token")


def test_fanbase_descriptor_declares_account_ready_and_social_live_unsupported() -> None:
    connector = FanbaseConnector()
    assert connector.descriptor.capability(Capability.ACCOUNT).state is CapabilityState.READY
    assert connector.descriptor.capability(Capability.SOCIAL).state is CapabilityState.UNSUPPORTED
    assert connector.descriptor.capability(Capability.LIVE).state is CapabilityState.UNSUPPORTED
