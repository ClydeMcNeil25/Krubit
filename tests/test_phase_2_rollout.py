"""Phase 2 completion rollout contract: safe defaults and an honest capability catalog.

These tests exist to protect the two claims the operator runbook and completion audit
depend on: (1) every new Phase 2 connector/social-delivery surface defaults OFF so
installing this build never silently starts monitoring or posting, and (2) the static
connector catalog — the thing `/fetch creator verify` and the design doc's platform
capability matrix are built from — declares all three capability classes for every
platform, with no platform silently missing from an operator's honest-state read.

This file intentionally does not re-test `Settings.from_env`'s per-field parsing
(`tests/test_config.py` already does that in detail) or per-platform URL recognition
(`tests/test_connector_catalog.py` already does that). It tests the rollout-facing
*contract* across both modules at once, matching the design doc's platform capability
matrix table so a future edit to one without the other fails loudly here.
"""

from __future__ import annotations

from krubit.config import Settings
from krubit.domain.creator_signals import Capability, CapabilityState, Platform
from krubit.integrations.catalog import CATALOG


def base_env() -> dict[str, str]:
    return {
        "DISCORD_KRUBIT_APPLICATION_ID": "123456789012345678",
        "KRUBIT_DATABASE_PATH": "state/test.db",
    }


def test_new_connectors_default_disabled_and_can_be_enabled_independently() -> None:
    settings = Settings.from_env(base_env())
    assert settings.creator_signals_enabled is False
    assert settings.social_delivery_enabled is False

    creator_only = Settings.from_env({**base_env(), "KRUBIT_CREATOR_SIGNALS_ENABLED": "true"})
    assert creator_only.creator_signals_enabled is True
    assert creator_only.social_delivery_enabled is False

    social_only = Settings.from_env({**base_env(), "KRUBIT_SOCIAL_DELIVERY_ENABLED": "true"})
    assert social_only.creator_signals_enabled is False
    assert social_only.social_delivery_enabled is True


def test_live_signals_flag_remains_independent_of_the_new_phase_2_flags() -> None:
    """Enabling the Phase 2A Twitch/Discord-presence flag alone must not imply the new
    creator-registry or social-delivery surfaces are enabled, and vice versa — the
    design doc's "enabling one connector does not enable any other platform or content
    class" rule applies to the top-level rollout flags too."""
    settings = Settings.from_env({**base_env(), "KRUBIT_LIVE_SIGNALS_ENABLED": "true"})
    assert settings.live_signals_enabled is True
    assert settings.creator_signals_enabled is False
    assert settings.social_delivery_enabled is False


def expected_catalog_capabilities() -> set[tuple[Platform, Capability]]:
    """Every (platform, capability) pair the design doc's platform capability matrix
    promises Krubit declares an honest state for — one entry per platform for each of
    account/social/live, regardless of whether that capability is presently `ready`."""
    return {(platform, capability) for platform in Platform for capability in Capability}


def test_every_catalog_capability_appears_even_when_unconfigured() -> None:
    """`CATALOG` is the source every operator-facing health surface (`/fetch creator
    verify`, the completion audit) reads from. A platform or capability silently
    missing here would silently vanish from every honest-state report downstream, so
    this asserts the full cross product against the design doc's matrix rather than
    trusting `CATALOG`'s own keys."""
    declared = {
        (platform, fact.capability)
        for platform, descriptor in CATALOG.items()
        for fact in descriptor.capabilities
    }
    assert declared == expected_catalog_capabilities()


def test_no_catalog_capability_is_silently_reported_ready_by_default() -> None:
    """A capability may only ever be `ready` when the design doc's matrix says the
    platform needs no credential/authorization for it (public-URL account enrollment,
    or Bluesky's public reads). Every other declared fact must start at a
    non-operational state — this is the "connector begins disabled" rollout rule
    applied to the static catalog baseline itself."""
    always_ready_by_design = {
        (Platform.TWITCH, Capability.ACCOUNT),
        (Platform.YOUTUBE, Capability.ACCOUNT),
        (Platform.BLUESKY, Capability.ACCOUNT),
        (Platform.BLUESKY, Capability.SOCIAL),
        (Platform.FANBASE, Capability.ACCOUNT),
    }
    for platform, descriptor in CATALOG.items():
        for fact in descriptor.capabilities:
            key = (platform, fact.capability)
            if key in always_ready_by_design:
                continue
            assert fact.state is not CapabilityState.READY, (
                f"{platform}/{fact.capability} starts READY without a design-doc "
                "justification for needing no credential or authorization"
            )
