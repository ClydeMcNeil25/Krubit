import pytest

from krubit.domain.creator_signals import Capability, CapabilityState, Platform
from krubit.integrations.catalog import CATALOG, recognize_account_url


def test_catalog_recognizes_supported_profile_urls() -> None:
    cases = {
        "https://youtube.com/@KrucialStudios": (Platform.YOUTUBE, "KrucialStudios"),
        "https://x.com/KrucialStudios": (Platform.X, "KrucialStudios"),
        "https://www.instagram.com/krucialstudios/": (Platform.INSTAGRAM, "krucialstudios"),
        "https://www.facebook.com/krucialstudios": (Platform.FACEBOOK, "krucialstudios"),
        "https://www.threads.net/@krucialstudios": (Platform.THREADS, "krucialstudios"),
        "https://bsky.app/profile/krucialstudios.bsky.social": (
            Platform.BLUESKY,
            "krucialstudios.bsky.social",
        ),
        "https://www.tiktok.com/@krucialstudios": (Platform.TIKTOK, "krucialstudios"),
        "https://fanbase.app/krucialstudios": (Platform.FANBASE, "krucialstudios"),
    }
    for url, expected in cases.items():
        result = recognize_account_url(url)
        assert (result.platform, result.handle) == expected


def test_catalog_rejects_credentials_fragments_and_lookalike_hosts() -> None:
    for url in (
        "https://user:pass@youtube.com/@safe",
        "https://youtube.com.evil.example/@safe",
        "https://x.com/safe#token=secret",
        "javascript:alert(1)",
    ):
        with pytest.raises(ValueError):
            recognize_account_url(url)


def test_catalog_recognizes_twitch_and_distinguishes_facebook_page_from_profile() -> None:
    twitch = recognize_account_url("https://www.twitch.tv/KrucialStudios/")
    assert (twitch.platform, twitch.handle) == (Platform.TWITCH, "KrucialStudios")

    page = recognize_account_url("https://www.facebook.com/pages/Krucial-Studios/123456789")
    assert (page.platform, page.handle) == (Platform.FACEBOOK_PAGE, "123456789")

    profile = recognize_account_url("https://www.facebook.com/krucialstudios")
    assert profile.platform is Platform.FACEBOOK


def test_recognized_url_carries_a_stable_https_canonical_url() -> None:
    result = recognize_account_url("https://youtube.com/@KrucialStudios")
    assert result.canonical_url == "https://www.youtube.com/@KrucialStudios"


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com:8443/@safe",
        "https://[::1]/@safe",
        "https://youtube.com/@safe%2f../..",
        "http://youtube.com/@safe",
        "https://youtube.com/@",
        "https://youtube.com/",
    ],
)
def test_catalog_rejects_ports_ip_literals_encoded_paths_and_unrecognized_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        recognize_account_url(url)


def test_every_platform_has_a_connector_descriptor_with_all_three_capabilities() -> None:
    assert set(CATALOG.keys()) == set(Platform)
    for platform, descriptor in CATALOG.items():
        assert descriptor.platform is platform
        declared = {fact.capability for fact in descriptor.capabilities}
        assert declared == {Capability.ACCOUNT, Capability.SOCIAL, Capability.LIVE}


def test_connector_descriptor_capability_lookup_and_known_baseline_states() -> None:
    twitch_social = CATALOG[Platform.TWITCH].capability(Capability.SOCIAL)
    tiktok_live = CATALOG[Platform.TIKTOK].capability(Capability.LIVE)
    bluesky_account = CATALOG[Platform.BLUESKY].capability(Capability.ACCOUNT)
    fanbase_social = CATALOG[Platform.FANBASE].capability(Capability.SOCIAL)

    assert twitch_social.state is CapabilityState.UNSUPPORTED
    assert tiktok_live.state is CapabilityState.APPROVAL_REQUIRED
    assert bluesky_account.state is CapabilityState.READY
    assert fanbase_social.state is CapabilityState.UNSUPPORTED

    with pytest.raises(ValueError, match="capability must be a Capability"):
        CATALOG[Platform.TWITCH].capability("live")  # type: ignore[arg-type]
