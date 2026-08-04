"""Framework-independent live-stream signal values and identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from re import fullmatch
from urllib.parse import urlsplit

_TWITCH_LOGIN_PATTERN = r"[a-zA-Z0-9_]{1,25}"
_TWITCH_URL_MAX_LENGTH = 2_048
_RESERVED_TWITCH_PATHS = frozenset({"directory", "downloads", "jobs", "p", "settings", "videos"})


class LiveSignalStatus(StrEnum):
    DETECTED = "detected"
    LIVE = "live"
    ENDING = "ending"
    ENDED = "ended"
    FAILED = "failed"


class LiveSignalAction(StrEnum):
    ENSURE_ROLE = "ensure_role"
    ANNOUNCE = "announce"
    EDIT_ANNOUNCEMENT = "edit_announcement"
    REMOVE_ROLE = "remove_role"


class TwitchLookupKind(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_aware(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


def normalize_twitch_channel(url: str) -> str | None:
    """Return the canonical Twitch login for a supported channel URL."""
    if len(url) > _TWITCH_URL_MAX_LENGTH:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in {"twitch.tv", "www.twitch.tv"}:
        return None
    match = fullmatch(rf"/({_TWITCH_LOGIN_PATTERN})/?", parsed.path)
    if match is None:
        return None
    login = match.group(1).lower()
    return None if login in _RESERVED_TWITCH_PATHS else login


@dataclass(frozen=True, slots=True)
class StreamingObservation:
    guild_id: int
    member_id: int
    twitch_login: str
    twitch_url: str
    activity_started_at: datetime | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        if fullmatch(_TWITCH_LOGIN_PATTERN, self.twitch_login) is None:
            raise ValueError("twitch_login must be a valid Twitch login")
        _require_text("twitch_url", self.twitch_url, limit=_TWITCH_URL_MAX_LENGTH)
        if normalize_twitch_channel(self.twitch_url) != self.twitch_login.lower():
            raise ValueError("twitch_url must match twitch_login")
        if self.activity_started_at is not None:
            _require_aware(self.activity_started_at)
        _require_aware(self.observed_at)


@dataclass(frozen=True, slots=True)
class TwitchStream:
    stream_id: str
    user_login: str
    user_name: str
    title: str
    game_name: str
    started_at: datetime
    thumbnail_url: str

    def __post_init__(self) -> None:
        _require_text("stream_id", self.stream_id, limit=100)
        if fullmatch(_TWITCH_LOGIN_PATTERN, self.user_login) is None:
            raise ValueError("user_login must be a valid Twitch login")
        _require_text("user_name", self.user_name, limit=100)
        _require_text("title", self.title, limit=300)
        _require_text("game_name", self.game_name, limit=100)
        _require_aware(self.started_at)
        _require_text("thumbnail_url", self.thumbnail_url, limit=2_048)


@dataclass(frozen=True, slots=True)
class TwitchLookup:
    kind: TwitchLookupKind
    stream: TwitchStream | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not TwitchLookupKind:
            raise ValueError("kind must be a TwitchLookupKind")
        if self.kind is TwitchLookupKind.LIVE and self.stream is None:
            raise ValueError("a live Twitch lookup requires a stream")
        if self.kind is not TwitchLookupKind.LIVE and self.stream is not None:
            raise ValueError("only a live Twitch lookup may include a stream")
        if self.unavailable_reason is not None:
            _require_text("unavailable_reason", self.unavailable_reason, limit=100)


@dataclass(frozen=True, slots=True)
class LiveSignalConfig:
    guild_id: int
    channel_id: int
    role_id: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("channel_id", self.channel_id)
        _require_positive_id("role_id", self.role_id)
        _require_aware(self.updated_at)


@dataclass(frozen=True, slots=True)
class LiveSignalSession:
    guild_id: int
    session_key: str
    member_id: int
    twitch_login: str
    twitch_url: str
    status: LiveSignalStatus
    detected_at: datetime
    presence_started_at: datetime | None = None
    stream: TwitchStream | None = None
    announcement_channel_id: int | None = None
    announcement_message_id: int | None = None
    role_id: int | None = None
    role_assigned_by_krubit: bool = False
    presence_active: bool = True
    missing_since: datetime | None = None
    last_discord_at: datetime | None = None
    last_twitch_at: datetime | None = None
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("session_key", self.session_key, limit=64)
        _require_positive_id("member_id", self.member_id)
        if fullmatch(_TWITCH_LOGIN_PATTERN, self.twitch_login) is None:
            raise ValueError("twitch_login must be a valid Twitch login")
        _require_text("twitch_url", self.twitch_url, limit=_TWITCH_URL_MAX_LENGTH)
        if normalize_twitch_channel(self.twitch_url) != self.twitch_login.lower():
            raise ValueError("twitch_url must match twitch_login")
        if type(self.status) is not LiveSignalStatus:
            raise ValueError("status must be a LiveSignalStatus")
        _require_aware(self.detected_at)
        for timestamp in (
            self.presence_started_at,
            self.missing_since,
            self.last_discord_at,
            self.last_twitch_at,
            self.ended_at,
        ):
            if timestamp is not None:
                _require_aware(timestamp)
        for name, identifier in (
            ("announcement_channel_id", self.announcement_channel_id),
            ("announcement_message_id", self.announcement_message_id),
            ("role_id", self.role_id),
        ):
            if identifier is not None:
                _require_positive_id(name, identifier)


@dataclass(frozen=True, slots=True)
class LiveSignalPlan:
    guild_id: int
    session_key: str
    actions: tuple[LiveSignalAction, ...]
    stream: TwitchStream | None = None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_text("session_key", self.session_key, limit=64)
        if type(self.actions) is not tuple:
            raise ValueError("actions must be a tuple")
        if not all(type(action) is LiveSignalAction for action in self.actions):
            raise ValueError("actions must contain LiveSignalAction values")


def provisional_session_key(observation: StreamingObservation) -> str:
    """Build a stable identity before Twitch exposes a durable stream ID."""
    started_at = observation.activity_started_at or observation.observed_at
    identity = ":".join(
        (
            str(observation.guild_id),
            str(observation.member_id),
            observation.twitch_login.lower(),
            started_at.isoformat(),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()
