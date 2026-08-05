from dataclasses import FrozenInstanceError

import pytest

from krubit.domain.creator_signals import (
    Capability,
    CapabilityFact,
    CapabilityState,
    ContentKind,
    ContentState,
    Platform,
    RecognizedAccountUrl,
)


def test_platform_enumerates_every_supported_creator_platform() -> None:
    assert [platform.value for platform in Platform] == [
        "twitch",
        "youtube",
        "x",
        "instagram",
        "facebook",
        "facebook_page",
        "threads",
        "bluesky",
        "tiktok",
        "fanbase",
    ]


def test_capability_enumerates_the_three_tracked_classes() -> None:
    assert [capability.value for capability in Capability] == ["account", "social", "live"]


def test_capability_state_never_implies_operational_by_default() -> None:
    assert [state.value for state in CapabilityState] == [
        "ready",
        "unconfigured",
        "authorization_required",
        "approval_required",
        "degraded",
        "quota_limited",
        "unsupported",
    ]


def test_content_kind_and_content_state_cover_the_documented_lifecycle() -> None:
    assert {kind.value for kind in ContentKind} == {"live", "video", "short", "post", "reel"}
    assert [state.value for state in ContentState] == [
        "scheduled",
        "delayed",
        "live",
        "ended",
        "cancelled",
        "published",
        "corrected",
        "retracted",
        "failed",
    ]


def test_recognized_account_url_is_immutable_and_requires_https_canonical_url() -> None:
    recognized = RecognizedAccountUrl(
        platform=Platform.TWITCH,
        handle="krucialstudios",
        canonical_url="https://www.twitch.tv/krucialstudios",
    )
    with pytest.raises(FrozenInstanceError):
        recognized.handle = "someone-else"  # type: ignore[misc]

    with pytest.raises(ValueError, match="canonical_url must use https"):
        RecognizedAccountUrl(
            platform=Platform.TWITCH,
            handle="krucialstudios",
            canonical_url="http://www.twitch.tv/krucialstudios",
        )
    with pytest.raises(ValueError, match="handle must not be blank"):
        RecognizedAccountUrl(
            platform=Platform.TWITCH, handle="   ", canonical_url="https://www.twitch.tv/x"
        )


def test_capability_fact_requires_matching_enum_types_and_bounds_detail_text() -> None:
    fact = CapabilityFact(Capability.LIVE, CapabilityState.READY, "Verified via API")
    assert fact.capability is Capability.LIVE
    assert fact.state is CapabilityState.READY

    with pytest.raises(ValueError, match="capability must be a Capability"):
        CapabilityFact("live", CapabilityState.READY)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="state must be a CapabilityState"):
        CapabilityFact(Capability.LIVE, "ready")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="detail exceeds 200 characters"):
        CapabilityFact(Capability.LIVE, CapabilityState.READY, "x" * 201)
