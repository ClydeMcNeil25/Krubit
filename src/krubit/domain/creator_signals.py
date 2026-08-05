"""Framework-independent creator-signal vocabulary shared by every platform connector.

This module defines the platform, capability, and content-lifecycle enums plus the
small immutable value objects that later Phase 2 completion tasks build on: creator
registry, connector protocol, content ledger, policy, and delivery. It intentionally
contains no I/O, no platform-specific parsing, and no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_MAX_HANDLE_LENGTH = 128
_MAX_URL_LENGTH = 2_048
_MAX_DETAIL_LENGTH = 200


class Platform(StrEnum):
    """Every creator platform Krubit can recognize or connect to."""

    TWITCH = "twitch"
    YOUTUBE = "youtube"
    X = "x"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    FACEBOOK_PAGE = "facebook_page"
    THREADS = "threads"
    BLUESKY = "bluesky"
    TIKTOK = "tiktok"
    FANBASE = "fanbase"


class Capability(StrEnum):
    """The three capability classes tracked per platform per account.

    These mirror the "Account enrollment", "Social content", and "Live content"
    columns of the platform capability matrix.
    """

    ACCOUNT = "account"
    SOCIAL = "social"
    LIVE = "live"


class CapabilityState(StrEnum):
    """Honest operational state for one platform capability.

    A capability is never reported as operational unless it is `READY`.
    """

    READY = "ready"
    UNCONFIGURED = "unconfigured"
    AUTHORIZATION_REQUIRED = "authorization_required"
    APPROVAL_REQUIRED = "approval_required"
    DEGRADED = "degraded"
    QUOTA_LIMITED = "quota_limited"
    UNSUPPORTED = "unsupported"


class ContentKind(StrEnum):
    """Normalized content classes emitted by any connector."""

    LIVE = "live"
    VIDEO = "video"
    SHORT = "short"
    POST = "post"
    REEL = "reel"


class ContentState(StrEnum):
    """Normalized content lifecycle states shared by every platform."""

    SCHEDULED = "scheduled"
    DELAYED = "delayed"
    LIVE = "live"
    ENDED = "ended"
    CANCELLED = "cancelled"
    PUBLISHED = "published"
    CORRECTED = "corrected"
    RETRACTED = "retracted"
    FAILED = "failed"


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


@dataclass(frozen=True, slots=True)
class RecognizedAccountUrl:
    """A pasted creator profile URL normalized to a stable platform/handle identity."""

    platform: Platform
    handle: str
    canonical_url: str

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("handle", self.handle, limit=_MAX_HANDLE_LENGTH)
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")


@dataclass(frozen=True, slots=True)
class CapabilityFact:
    """One platform capability's currently declared state."""

    capability: Capability
    state: CapabilityState
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.capability) is not Capability:
            raise ValueError("capability must be a Capability")
        if type(self.state) is not CapabilityState:
            raise ValueError("state must be a CapabilityState")
        if self.detail is not None:
            _require_text("detail", self.detail, limit=_MAX_DETAIL_LENGTH)
