"""Framework-independent creator-signal vocabulary shared by every platform connector.

This module defines the platform, capability, and content-lifecycle enums plus the
small immutable value objects that later Phase 2 completion tasks build on: creator
registry, connector protocol, content ledger, policy, and delivery. It intentionally
contains no I/O, no platform-specific parsing, and no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

_MAX_HANDLE_LENGTH = 128
_MAX_URL_LENGTH = 2_048
_MAX_DETAIL_LENGTH = 200
_MAX_EXTERNAL_ID_LENGTH = 200
_MAX_ACCOUNT_ID_LENGTH = 64
_MAX_TITLE_LENGTH = 300
_MAX_CURSOR_LENGTH = 500


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


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


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


def creator_account_id(platform: Platform, external_id: str) -> str:
    """Derive the stable, guild-independent identity for one platform account.

    Two enrollment attempts for the same resolved platform identity always produce the
    same account_id, which is what lets storage reject a second owner for that identity
    within a guild.
    """
    if type(platform) is not Platform:
        raise ValueError("platform must be a Platform")
    _require_text("external_id", external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
    identity = f"{platform.value}:{external_id}"
    return sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CreatorProfile:
    """A Discord member's creator profile within one guild.

    Grouping entity for a member's owned accounts. Carries no mutable display name;
    identity comes from the Discord member ID.
    """

    guild_id: int
    owner_member_id: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("owner_member_id", self.owner_member_id)
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class CreatorAccount:
    """One approved external platform account owned by a member in one guild.

    `account_id` is the stable identity from `creator_account_id`; it is derived from
    the platform and resolved external ID, not from `guild_id` or `owner_member_id`, so
    the same external account is recognized consistently across guilds while storage
    still enforces one owner per guild.
    """

    guild_id: int
    account_id: str
    owner_member_id: int
    platform: Platform
    handle: str
    canonical_url: str
    external_id: str
    paused: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        _require_positive_id("owner_member_id", self.owner_member_id)
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("handle", self.handle, limit=_MAX_HANDLE_LENGTH)
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        _require_text("external_id", self.external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
        if type(self.paused) is not bool:
            raise ValueError("paused must be a bool")
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class CreatorRoute:
    """The Discord destination for one account's content-kind bucket in one guild."""

    guild_id: int
    account_id: str
    content_kind: ContentKind
    channel_id: int
    mention_role_id: int | None
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        if type(self.content_kind) is not ContentKind:
            raise ValueError("content_kind must be a ContentKind")
        _require_positive_id("channel_id", self.channel_id)
        if self.mention_role_id is not None:
            _require_positive_id("mention_role_id", self.mention_role_id)
        _require_aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class ContentObservation:
    """One connector-reported content item, normalized to shared vocabulary.

    Produced by parsing a single raw `ConnectorPage` item. Carries no guild, account, or
    storage identity of its own — `ContentSignalService` combines it with the ingesting
    `CreatorAccount` to build a durable `ContentEvent`.
    """

    external_id: str
    content_kind: ContentKind
    state: ContentState
    canonical_url: str
    title: str | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("external_id", self.external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
        if type(self.content_kind) is not ContentKind:
            raise ValueError("content_kind must be a ContentKind")
        if type(self.state) is not ContentState:
            raise ValueError("state must be a ContentState")
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if self.title is not None:
            _require_text("title", self.title, limit=_MAX_TITLE_LENGTH)
        if self.published_at is not None:
            _require_aware("published_at", self.published_at)


@dataclass(frozen=True, slots=True)
class ContentEvent:
    """The durable ledger record for one piece of content observed for one account.

    Identity is `(guild_id, platform, external_id)`, matching the storage layer's
    `UNIQUE(guild_id, platform, external_id)` constraint. `first_observed_at` never
    changes after creation; `last_observed_at` and `state` advance as later connector
    pages report lifecycle changes.
    """

    guild_id: int
    account_id: str
    platform: Platform
    external_id: str
    content_kind: ContentKind
    state: ContentState
    canonical_url: str
    title: str | None
    published_at: datetime | None
    first_observed_at: datetime
    last_observed_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("external_id", self.external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
        if type(self.content_kind) is not ContentKind:
            raise ValueError("content_kind must be a ContentKind")
        if type(self.state) is not ContentState:
            raise ValueError("state must be a ContentState")
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if self.title is not None:
            _require_text("title", self.title, limit=_MAX_TITLE_LENGTH)
        if self.published_at is not None:
            _require_aware("published_at", self.published_at)
        _require_aware("first_observed_at", self.first_observed_at)
        _require_aware("last_observed_at", self.last_observed_at)


_LIVE_OR_PUBLISHED_STATES = frozenset({ContentState.PUBLISHED, ContentState.LIVE})


def reaches_publish_or_live(state: ContentState) -> bool:
    """Whether `state` counts as a claimable publish/live transition target.

    Baseline ingestion never claims regardless of this check. On later pages, an event
    claims exactly one pending delivery the first time it reaches `PUBLISHED` or `LIVE`
    — not on every re-observation while it stays in one of those states, and not for
    `SCHEDULED`, `DELAYED`, `ENDED`, `CANCELLED`, `CORRECTED`, `RETRACTED`, or `FAILED`.
    """
    return state in _LIVE_OR_PUBLISHED_STATES


@dataclass(frozen=True, slots=True)
class ContentCursor:
    """A durable per-account connector cursor and its one-time baseline marker.

    `baselined_at` is set once, the first time an account is ever successfully
    ingested, and never changes afterward — it is what lets `ContentSignalService`
    distinguish the baseline page (identities stored, nothing claimed) from every later
    page (lifecycle changes upserted, new publish/live transitions claimed).
    """

    guild_id: int
    account_id: str
    platform: Platform
    value: str | None
    baselined_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        if self.value is not None:
            _require_text("value", self.value, limit=_MAX_CURSOR_LENGTH)
        if self.baselined_at is not None:
            _require_aware("baselined_at", self.baselined_at)
        _require_aware("updated_at", self.updated_at)


_DELIVERY_STATUSES = frozenset({"pending", "delivered", "cancelled", "failed"})


@dataclass(frozen=True, slots=True)
class ContentDelivery:
    """A durable, at-most-once claim to announce one content event.

    Identity matches its `ContentEvent`'s `(guild_id, platform, external_id)`, so a
    given piece of content can never accumulate more than one claim, however many times
    it is observed or how many times it re-enters a publish/live state.
    """

    guild_id: int
    platform: Platform
    external_id: str
    account_id: str
    status: str
    attempt: int
    discord_channel_id: int | None
    discord_message_id: int | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("external_id", self.external_id, limit=_MAX_EXTERNAL_ID_LENGTH)
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        if self.status not in _DELIVERY_STATUSES:
            raise ValueError(f"status must be one of {sorted(_DELIVERY_STATUSES)}")
        if self.attempt <= 0:
            raise ValueError("attempt must be positive")
        if self.discord_channel_id is not None:
            _require_positive_id("discord_channel_id", self.discord_channel_id)
        if self.discord_message_id is not None:
            _require_positive_id("discord_message_id", self.discord_message_id)
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class ContentPlan:
    """One claimed delivery paired with the ledger event that triggered it."""

    event: ContentEvent
    delivery: ContentDelivery

    def __post_init__(self) -> None:
        if type(self.event) is not ContentEvent:
            raise ValueError("event must be a ContentEvent")
        if type(self.delivery) is not ContentDelivery:
            raise ValueError("delivery must be a ContentDelivery")
        if (
            self.event.guild_id != self.delivery.guild_id
            or self.event.platform != self.delivery.platform
            or self.event.external_id != self.delivery.external_id
        ):
            raise ValueError("delivery must share its event's guild, platform, and external_id")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The outcome of ingesting one connector page for one account.

    `plans` holds one `ContentPlan` per item that newly claimed a pending delivery this
    page, in the order those items appeared in the page. It is empty for a baseline page
    and empty for a later page whose items are all no-op re-observations.
    """

    account_id: str
    cursor: ContentCursor
    plans: tuple[ContentPlan, ...]

    def __post_init__(self) -> None:
        _require_text("account_id", self.account_id, limit=_MAX_ACCOUNT_ID_LENGTH)
        if type(self.cursor) is not ContentCursor:
            raise ValueError("cursor must be a ContentCursor")
        if type(self.plans) is not tuple:
            raise ValueError("plans must be a tuple")
