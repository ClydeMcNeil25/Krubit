from __future__ import annotations

from datetime import UTC, datetime, timedelta

from krubit.discord.inventory import render_connector_health
from krubit.domain.creator_signals import Capability, CapabilityState, ContentCursor, Platform
from krubit.integrations.base import ConnectorHealth
from krubit.services.creator_analytics import DeliveryCounts
from krubit.services.health import HealthService

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def cursor(*, updated_at: datetime) -> ContentCursor:
    return ContentCursor(
        guild_id=111,
        account_id="account-1",
        platform=Platform.TWITCH,
        value="cursor-value",
        baselined_at=updated_at,
        updated_at=updated_at,
    )


def health_with_secret_in_internal_error() -> ConnectorHealth:
    """A `ConnectorHealth` whose `detail` field carries a raw internal error string
    (as if a connector's exception handler leaked one) — used to prove
    `render_connector_health` never echoes it."""
    return ConnectorHealth(
        capability=Capability.LIVE,
        state=CapabilityState.AUTHORIZATION_REQUIRED,
        detail="Bearer secret-token-abc123 rejected by upstream",
    )


def test_integration_status_exposes_state_not_token_or_raw_api_body() -> None:
    card = render_connector_health(health_with_secret_in_internal_error())

    assert "authorization_required" in card.description
    assert "secret-token" not in card.description
    assert all("secret-token" not in field.value for field in card.fields)


def test_render_connector_health_ready_state_is_safe_too() -> None:
    card = render_connector_health(
        ConnectorHealth(capability=Capability.ACCOUNT, state=CapabilityState.READY)
    )
    assert "ready" in card.description


def test_creator_health_flags_non_ready_connector_capability() -> None:
    report = HealthService().creator_health(
        connector_health=(
            ConnectorHealth(capability=Capability.SOCIAL, state=CapabilityState.UNCONFIGURED),
        ),
        cursor=cursor(updated_at=NOW),
        delivery_counts=DeliveryCounts(delivered=1, pending=0, failed=0, cancelled=0),
        quota_exhausted=False,
        now=NOW,
    )
    assert report.status == "limited"
    assert [finding.code for finding in report.findings] == ["connector_social_unconfigured"]


def test_creator_health_ready_capability_produces_no_finding() -> None:
    ready = ConnectorHealth(capability=Capability.ACCOUNT, state=CapabilityState.READY)
    report = HealthService().creator_health(
        connector_health=(ready,),
        cursor=cursor(updated_at=NOW),
        delivery_counts=DeliveryCounts(delivered=1, pending=0, failed=0, cancelled=0),
        quota_exhausted=False,
        now=NOW,
    )
    assert report.status == "healthy"
    assert report.findings == ()


def test_creator_health_flags_missing_and_stale_cursor() -> None:
    missing = HealthService().creator_health(
        connector_health=(),
        cursor=None,
        delivery_counts=DeliveryCounts(delivered=0, pending=0, failed=0, cancelled=0),
        quota_exhausted=False,
        now=NOW,
    )
    assert "cursor_missing" in [finding.code for finding in missing.findings]

    stale = HealthService().creator_health(
        connector_health=(),
        cursor=cursor(updated_at=NOW - timedelta(hours=30)),
        delivery_counts=DeliveryCounts(delivered=0, pending=0, failed=0, cancelled=0),
        quota_exhausted=False,
        now=NOW,
    )
    assert "cursor_stale" in [finding.code for finding in stale.findings]
    assert stale.status == "warning"


def test_creator_health_flags_failed_and_pending_deliveries_and_exhausted_quota() -> None:
    report = HealthService().creator_health(
        connector_health=(),
        cursor=cursor(updated_at=NOW),
        delivery_counts=DeliveryCounts(delivered=0, pending=2, failed=1, cancelled=0),
        quota_exhausted=True,
        now=NOW,
    )
    codes = [finding.code for finding in report.findings]
    assert "delivery_failed" in codes
    assert "delivery_pending" in codes
    assert "quota_exhausted" in codes
    assert report.status == "warning"


def test_bootstrap_health_flags_missing_and_ambiguous_resources() -> None:
    missing = HealthService().bootstrap_health(
        role_present=False,
        role_ambiguous=False,
        channel_present=False,
        channel_ambiguous=False,
        now=NOW,
    )
    codes = [finding.code for finding in missing.findings]
    assert "creator_role_missing" in codes
    assert "creator_channel_missing" in codes

    ambiguous = HealthService().bootstrap_health(
        role_present=True,
        role_ambiguous=True,
        channel_present=True,
        channel_ambiguous=True,
        now=NOW,
    )
    ambiguous_codes = [finding.code for finding in ambiguous.findings]
    assert "creator_role_ambiguous" in ambiguous_codes
    assert "creator_channel_ambiguous" in ambiguous_codes


def test_bootstrap_health_clean_when_both_resources_resolved() -> None:
    report = HealthService().bootstrap_health(
        role_present=True,
        role_ambiguous=False,
        channel_present=True,
        channel_ambiguous=False,
        now=NOW,
    )
    assert report.status == "healthy"
    assert report.findings == ()
