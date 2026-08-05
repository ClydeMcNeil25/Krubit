"""Creator profile URL recognition and the per-platform connector capability catalog.

`recognize_account_url` turns a pasted creator profile URL into a
`RecognizedAccountUrl` using an explicit host allowlist and a strict per-platform path
parser. It never scrapes, follows redirects, or guesses: unrecognized hosts or paths,
non-HTTPS schemes, userinfo, fragments, ports, IP-literal hosts, and percent-encoded
separators are all rejected with `ValueError`.

`CATALOG` declares each platform's connector metadata: which of the three capability
classes (`Capability.ACCOUNT`, `Capability.SOCIAL`, `Capability.LIVE`) it has, and the
honest `CapabilityState` each starts in before any credentials or authorization exist.
Later tasks (registry, connector protocol, health) refine these states at runtime; this
module only declares the static, platform-inherent baseline described by the platform
capability matrix.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from krubit.domain.creator_signals import (
    Capability,
    CapabilityFact,
    CapabilityState,
    Platform,
    RecognizedAccountUrl,
)

_MAX_URL_LENGTH = 2_048


@dataclass(frozen=True, slots=True)
class ConnectorDescriptor:
    """Static connector metadata: which capabilities a platform declares and their state."""

    platform: Platform
    capabilities: tuple[CapabilityFact, ...]

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        if type(self.capabilities) is not tuple or not self.capabilities:
            raise ValueError("capabilities must be a non-empty tuple")
        seen: set[Capability] = set()
        for fact in self.capabilities:
            if type(fact) is not CapabilityFact:
                raise ValueError("capabilities must contain CapabilityFact values")
            if fact.capability in seen:
                raise ValueError("capabilities must not declare a Capability twice")
            seen.add(fact.capability)

    def capability(self, capability: Capability) -> CapabilityFact:
        """Return the declared fact for `capability`, raising if it is not declared."""
        if type(capability) is not Capability:
            raise ValueError("capability must be a Capability")
        for fact in self.capabilities:
            if fact.capability is capability:
                return fact
        raise KeyError(f"{self.platform} does not declare {capability}")


@dataclass(frozen=True, slots=True)
class _PlatformEntry:
    platform: Platform
    hosts: frozenset[str]
    path_pattern: re.Pattern[str]
    canonical: str
    capabilities: tuple[CapabilityFact, ...]

    def canonical_url(self, handle: str) -> str:
        return self.canonical.format(handle=handle)


def _handle_pattern(body: str, *, prefix: str = "") -> re.Pattern[str]:
    return re.compile(rf"/{prefix}(?P<handle>{body})/?")


_TWITCH_HANDLE = r"[A-Za-z0-9_]{1,25}"
_YOUTUBE_HANDLE = r"[A-Za-z0-9_.-]{1,30}"
_X_HANDLE = r"[A-Za-z0-9_]{1,15}"
_INSTAGRAM_HANDLE = r"[A-Za-z0-9_.]{1,30}"
_FACEBOOK_HANDLE = r"[A-Za-z0-9_.]{1,50}"
_FACEBOOK_PAGE_ID = r"[0-9]{1,20}"
_THREADS_HANDLE = r"[A-Za-z0-9_.]{1,30}"
_BLUESKY_HANDLE = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?"
_TIKTOK_HANDLE = r"[A-Za-z0-9_.]{1,24}"
_FANBASE_HANDLE = r"[A-Za-z0-9_.-]{1,60}"

# Ordered: the first entry whose host and path both match wins. Facebook Page's
# explicit `/pages/<name>/<id>` permalink is checked before the generic Facebook
# profile slug pattern so a Page permalink is never mistaken for a profile handle.
_CATALOG_ENTRIES: tuple[_PlatformEntry, ...] = (
    _PlatformEntry(
        platform=Platform.TWITCH,
        hosts=frozenset({"twitch.tv", "www.twitch.tv"}),
        path_pattern=_handle_pattern(_TWITCH_HANDLE),
        canonical="https://www.twitch.tv/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT, CapabilityState.READY, "Public URL or Discord presence"
            ),
            CapabilityFact(
                Capability.SOCIAL, CapabilityState.UNSUPPORTED, "Social content is not in scope"
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.READY,
                "Discord presence plus Twitch API verification",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.YOUTUBE,
        hosts=frozenset({"youtube.com", "www.youtube.com"}),
        path_pattern=_handle_pattern(_YOUTUBE_HANDLE, prefix="@"),
        canonical="https://www.youtube.com/@{handle}",
        capabilities=(
            CapabilityFact(Capability.ACCOUNT, CapabilityState.READY, "Public channel URL"),
            CapabilityFact(
                Capability.SOCIAL, CapabilityState.UNCONFIGURED, "Requires a YouTube API key"
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.UNCONFIGURED,
                "Requires API push/poll configuration",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.X,
        hosts=frozenset({"x.com", "www.x.com"}),
        path_pattern=_handle_pattern(_X_HANDLE),
        canonical="https://x.com/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT,
                CapabilityState.UNCONFIGURED,
                "Requires Krubit application access",
            ),
            CapabilityFact(
                Capability.SOCIAL, CapabilityState.UNCONFIGURED, "Requires a bearer token"
            ),
            CapabilityFact(
                Capability.LIVE, CapabilityState.UNSUPPORTED, "No promised live surface"
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.INSTAGRAM,
        hosts=frozenset({"instagram.com", "www.instagram.com"}),
        path_pattern=_handle_pattern(_INSTAGRAM_HANDLE),
        canonical="https://www.instagram.com/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Professional account authorization",
            ),
            CapabilityFact(
                Capability.SOCIAL, CapabilityState.AUTHORIZATION_REQUIRED, "Posts and Reels"
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.APPROVAL_REQUIRED,
                "Authorized live media only when official access exposes it",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.FACEBOOK_PAGE,
        hosts=frozenset({"facebook.com", "www.facebook.com"}),
        path_pattern=re.compile(
            rf"/pages/[A-Za-z0-9_.-]{{1,100}}/(?P<handle>{_FACEBOOK_PAGE_ID})/?"
        ),
        canonical="https://www.facebook.com/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT, CapabilityState.AUTHORIZATION_REQUIRED, "Page authorization"
            ),
            CapabilityFact(
                Capability.SOCIAL,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Posts, videos, and Reels",
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Authorized Page live broadcasts",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.FACEBOOK,
        hosts=frozenset({"facebook.com", "www.facebook.com"}),
        path_pattern=_handle_pattern(_FACEBOOK_HANDLE),
        canonical="https://www.facebook.com/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Owner authorization and approved Meta access",
            ),
            CapabilityFact(
                Capability.SOCIAL,
                CapabilityState.APPROVAL_REQUIRED,
                "Authorized owner content only",
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.UNSUPPORTED,
                "No promised live surface unless an approved API exposes it",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.THREADS,
        hosts=frozenset({"threads.net", "www.threads.net"}),
        path_pattern=_handle_pattern(_THREADS_HANDLE, prefix="@"),
        canonical="https://www.threads.net/@{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT, CapabilityState.AUTHORIZATION_REQUIRED, "Creator OAuth"
            ),
            CapabilityFact(
                Capability.SOCIAL, CapabilityState.AUTHORIZATION_REQUIRED, "Original Threads posts"
            ),
            CapabilityFact(
                Capability.LIVE, CapabilityState.UNSUPPORTED, "No promised live surface"
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.BLUESKY,
        hosts=frozenset({"bsky.app", "www.bsky.app"}),
        path_pattern=re.compile(rf"/profile/(?P<handle>{_BLUESKY_HANDLE})/?"),
        canonical="https://bsky.app/profile/{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT, CapabilityState.READY, "Public URL; no OAuth required"
            ),
            CapabilityFact(Capability.SOCIAL, CapabilityState.READY, "Original public posts"),
            CapabilityFact(
                Capability.LIVE, CapabilityState.UNSUPPORTED, "No promised live surface"
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.TIKTOK,
        hosts=frozenset({"tiktok.com", "www.tiktok.com"}),
        path_pattern=_handle_pattern(_TIKTOK_HANDLE, prefix="@"),
        canonical="https://www.tiktok.com/@{handle}",
        capabilities=(
            CapabilityFact(
                Capability.ACCOUNT,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Creator OAuth and approved Display API access",
            ),
            CapabilityFact(
                Capability.SOCIAL,
                CapabilityState.AUTHORIZATION_REQUIRED,
                "Authorized uploaded videos",
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.APPROVAL_REQUIRED,
                "Pending reliable TikTok detection access",
            ),
        ),
    ),
    _PlatformEntry(
        platform=Platform.FANBASE,
        hosts=frozenset({"fanbase.app", "www.fanbase.app"}),
        path_pattern=_handle_pattern(_FANBASE_HANDLE),
        canonical="https://fanbase.app/{handle}",
        capabilities=(
            CapabilityFact(Capability.ACCOUNT, CapabilityState.READY, "URL recognition"),
            CapabilityFact(
                Capability.SOCIAL,
                CapabilityState.UNSUPPORTED,
                "Pending official API or partner access",
            ),
            CapabilityFact(
                Capability.LIVE,
                CapabilityState.UNSUPPORTED,
                "Pending official API or partner access",
            ),
        ),
    ),
)

CATALOG: Mapping[Platform, ConnectorDescriptor] = {
    entry.platform: ConnectorDescriptor(platform=entry.platform, capabilities=entry.capabilities)
    for entry in _CATALOG_ENTRIES
}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _split_url(url: str) -> SplitResult:
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise ValueError("url could not be parsed") from exc


def recognize_account_url(url: str) -> RecognizedAccountUrl:
    """Recognize a supported creator profile URL and return its normalized identity.

    Raises `ValueError` for unsupported hosts/paths, non-HTTPS schemes, userinfo,
    fragments, ports, IP-literal hosts, and percent-encoded separators.
    """
    if len(url) > _MAX_URL_LENGTH:
        raise ValueError("url exceeds the maximum length")

    parsed = _split_url(url)
    if parsed.scheme != "https":
        raise ValueError("url must use https")
    if "%" in parsed.netloc or "%" in parsed.path:
        raise ValueError("url must not use percent-encoding")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain userinfo")
    if parsed.fragment:
        raise ValueError("url must not contain a fragment")

    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url has an invalid host or port") from exc
    if host is None:
        raise ValueError("url must include a host")
    if port is not None:
        raise ValueError("url must not specify a port")
    if _is_ip_literal(host):
        raise ValueError("url host must not be an IP literal")

    for entry in _CATALOG_ENTRIES:
        if host not in entry.hosts:
            continue
        match = entry.path_pattern.fullmatch(parsed.path)
        if match is None:
            continue
        handle = match.group("handle")
        return RecognizedAccountUrl(
            platform=entry.platform,
            handle=handle,
            canonical_url=entry.canonical_url(handle),
        )
    raise ValueError("url does not match a supported creator profile")
