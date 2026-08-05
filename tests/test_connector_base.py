import pytest

from krubit.domain.creator_signals import Capability, CapabilityState, Platform
from krubit.integrations.base import (
    ConnectorAccount,
    ConnectorFailure,
    ConnectorHealth,
    ConnectorPage,
)


def test_connector_failure_never_renders_secret_values() -> None:
    failure = ConnectorFailure.authorization("token abc-secret-value expired")
    assert "abc-secret-value" not in failure.safe_detail


def test_connector_failure_keeps_raw_detail_separate_from_safe_detail() -> None:
    failure = ConnectorFailure.rate_limited("Retry-After: 30, key=sk_live_super_secret")
    assert failure.detail == "Retry-After: 30, key=sk_live_super_secret"
    assert "sk_live_super_secret" not in failure.safe_detail
    assert failure.safe_detail == "the platform rate-limited this request"


def test_connector_failure_factories_cover_every_kind() -> None:
    factories = (
        ConnectorFailure.authorization,
        ConnectorFailure.rate_limited,
        ConnectorFailure.quota_exceeded,
        ConnectorFailure.unavailable,
        ConnectorFailure.invalid_response,
        ConnectorFailure.timeout,
        ConnectorFailure.not_found,
    )
    safe_details = {factory().safe_detail for factory in factories}
    assert len(safe_details) == len(factories)


def test_connector_failure_rejects_oversized_detail() -> None:
    with pytest.raises(ValueError, match="detail"):
        ConnectorFailure.unavailable("x" * 201)


def test_connector_account_requires_https_canonical_url() -> None:
    with pytest.raises(ValueError, match="https"):
        ConnectorAccount(
            platform=Platform.YOUTUBE,
            external_id="channel-1",
            handle="handle",
            canonical_url="http://youtube.com/@handle",
        )


def test_connector_account_round_trips_valid_fields() -> None:
    account = ConnectorAccount(
        platform=Platform.YOUTUBE,
        external_id="channel-1",
        handle="handle",
        canonical_url="https://www.youtube.com/@handle",
        display_name="Handle Display Name",
    )
    assert account.platform is Platform.YOUTUBE
    assert account.display_name == "Handle Display Name"


def test_connector_page_defaults_to_no_next_cursor() -> None:
    page = ConnectorPage(items=({"id": "1"},))
    assert page.next_cursor is None
    assert page.items == ({"id": "1"},)


def test_connector_page_rejects_blank_next_cursor() -> None:
    with pytest.raises(ValueError, match="next_cursor"):
        ConnectorPage(items=(), next_cursor="   ")


def test_connector_health_validates_enum_types() -> None:
    health = ConnectorHealth(
        capability=Capability.SOCIAL,
        state=CapabilityState.UNCONFIGURED,
        detail="Requires a YouTube API key",
    )
    assert health.capability is Capability.SOCIAL
    assert health.state is CapabilityState.UNCONFIGURED

    with pytest.raises(ValueError, match="capability"):
        ConnectorHealth(capability="social", state=CapabilityState.READY)  # type: ignore[arg-type]


def test_connector_protocol_is_satisfied_structurally() -> None:
    from krubit.domain.creator_signals import CapabilityFact, RecognizedAccountUrl
    from krubit.integrations.base import Connector
    from krubit.integrations.catalog import ConnectorDescriptor

    class _StubConnector:
        descriptor = ConnectorDescriptor(
            platform=Platform.YOUTUBE,
            capabilities=(CapabilityFact(Capability.ACCOUNT, CapabilityState.READY),),
        )

        async def resolve_account(self, recognized: RecognizedAccountUrl) -> ConnectorAccount:
            return ConnectorAccount(
                platform=recognized.platform,
                external_id="resolved-id",
                handle=recognized.handle,
                canonical_url=recognized.canonical_url,
            )

        async def fetch_page(self, account: object, *, cursor: str | None) -> ConnectorPage:
            return ConnectorPage(items=())

        async def health(self, account: object | None = None) -> ConnectorHealth:
            return ConnectorHealth(capability=Capability.ACCOUNT, state=CapabilityState.READY)

    stub: Connector = _StubConnector()
    assert isinstance(stub.descriptor, ConnectorDescriptor)
