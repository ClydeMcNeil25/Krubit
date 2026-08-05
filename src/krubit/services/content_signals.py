"""Durable content ledger ingestion: baseline suppression and delivery claiming.

`ContentSignalService.ingest_page` is the single entry point every connector's fetched
`ConnectorPage` passes through before its content can ever reach Discord. It normalizes
each raw item into a `ContentObservation`, then delegates the atomic ledger update to
`SQLiteStore.record_content_observations`, which alone decides whether an account is on
its baseline page (identities stored, nothing claimed) or a later page (lifecycle
changes upserted, at most one pending delivery claimed per item that newly reaches
`PUBLISHED` or `LIVE`). Malformed items never abort a page: they are skipped and
recorded as a redacted diagnostic receipt, matching the design's "malformed events enter
a redacted diagnostic receipt rather than delivery" rule.

The raw item envelope a connector must produce in `ConnectorPage.items` is:

- `external_id` (str, required): the platform's stable identifier for this item.
- `kind` (str, required): one of `ContentKind`'s values.
- `state` (str, required): one of `ContentState`'s values.
- `canonical_url` (str, required): an `https://` URL for this item.
- `title` (str, optional): a bounded display title or excerpt.
- `published_at` (str, optional): an ISO 8601 timestamp with a timezone offset.

Platform-specific response shapes never enter this module directly — each connector is
responsible for mapping its provider's payload into this shared envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from krubit.domain.creator_signals import (
    ContentKind,
    ContentObservation,
    ContentState,
    CreatorAccount,
    IngestionResult,
    Platform,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import ConnectorPage
from krubit.services.notification_policy import CorrelationDecision
from krubit.storage.sqlite import SQLiteStore

_MAX_URL_LENGTH = 2_048
_MAX_TITLE_LENGTH = 300
_MAX_FINGERPRINT_LENGTH = 200
_DEFAULT_CORRELATION_WINDOW = timedelta(minutes=30)


class MalformedContentItemError(ValueError):
    """Raised when a raw connector item does not match the shared item envelope."""


def _required_str(item: Mapping[str, JSONValue], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise MalformedContentItemError(f"{key} must be a string")
    return value


def _optional_str(item: Mapping[str, JSONValue], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedContentItemError(f"{key} must be a string")
    return value


def parse_observation(item: Mapping[str, JSONValue]) -> ContentObservation:
    """Normalize one raw connector item into a `ContentObservation`.

    Raises `MalformedContentItemError` (a `ValueError`) for a missing/mistyped required
    field, an unrecognized `kind`/`state`, a non-https `canonical_url`, or any other
    value a `ContentObservation` rejects.
    """
    external_id = _required_str(item, "external_id")
    kind = _required_str(item, "kind")
    state = _required_str(item, "state")
    canonical_url = _required_str(item, "canonical_url")
    title = _optional_str(item, "title")
    published_at_raw = _optional_str(item, "published_at")
    published_at = (
        datetime.fromisoformat(published_at_raw) if published_at_raw is not None else None
    )
    try:
        content_kind = ContentKind(kind)
        content_state = ContentState(state)
    except ValueError as exc:
        raise MalformedContentItemError(str(exc)) from exc
    try:
        return ContentObservation(
            external_id=external_id,
            content_kind=content_kind,
            state=content_state,
            canonical_url=canonical_url,
            title=title,
            published_at=published_at,
        )
    except ValueError as exc:
        raise MalformedContentItemError(str(exc)) from exc


class ContentSignalService:
    """Turns connector pages into durable ledger events and delivery claims."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def ingest_page(
        self, account: CreatorAccount, page: ConnectorPage, *, now: datetime
    ) -> IngestionResult:
        observations: list[ContentObservation] = []
        for item in page.items:
            try:
                observations.append(parse_observation(item))
            except ValueError as exc:
                await self._record_malformed_item(account, item, exc, now)
        cursor, plans = await self._store.record_content_observations(
            guild_id=account.guild_id,
            account_id=account.account_id,
            platform=account.platform,
            observations=tuple(observations),
            cursor_value=page.next_cursor,
            now=now,
        )
        return IngestionResult(account_id=account.account_id, cursor=cursor, plans=plans)

    async def _record_malformed_item(
        self,
        account: CreatorAccount,
        item: Mapping[str, JSONValue],
        error: Exception,
        now: datetime,
    ) -> None:
        raw_external_id = item.get("external_id")
        keys: list[JSONValue] = []
        for key in sorted(item.keys()):
            keys.append(key)
        detail: dict[str, JSONValue] = {"error": str(error), "keys": keys}
        await self._store.record_content_receipt(
            guild_id=account.guild_id,
            receipt_id=f"content:{uuid4().hex}",
            account_id=account.account_id,
            platform=account.platform,
            external_id=raw_external_id if isinstance(raw_external_id, str) else None,
            action="malformed_event",
            detail=detail,
            created_at=now,
        )


def _require_text(name: str, value: str, *, limit: int) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")


def _require_positive_id(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _normalize_url(url: str) -> str:
    """Fold case/trailing-slash/query/fragment noise so equivalent links compare equal."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


@dataclass(frozen=True, slots=True)
class CorrelationCandidate:
    """The bounded evidence available to correlate one content item across platforms.

    `owner_member_id` is the Discord member who owns the account this item came from —
    "the same creator" for correlation purposes means the same owning member, not the
    same platform-specific `account_id` (which necessarily differs across platforms).
    `outbound_url` and `media_fingerprint` are optional strong-match evidence a connector
    or later enrichment step may supply; their absence never causes a false merge.
    """

    guild_id: int
    owner_member_id: int
    platform: Platform
    external_id: str
    canonical_url: str
    title: str | None = None
    published_at: datetime | None = None
    outbound_url: str | None = None
    media_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("owner_member_id", self.owner_member_id)
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        _require_text("external_id", self.external_id, limit=_MAX_URL_LENGTH)
        _require_text("canonical_url", self.canonical_url, limit=_MAX_URL_LENGTH)
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if self.title is not None:
            _require_text("title", self.title, limit=_MAX_TITLE_LENGTH)
        if self.published_at is not None and (
            self.published_at.tzinfo is None or self.published_at.utcoffset() is None
        ):
            raise ValueError("published_at must include a timezone")
        if self.outbound_url is not None:
            _require_text("outbound_url", self.outbound_url, limit=_MAX_URL_LENGTH)
        if self.media_fingerprint is not None:
            _require_text(
                "media_fingerprint", self.media_fingerprint, limit=_MAX_FINGERPRINT_LENGTH
            )


class ContentCorrelator:
    """Cross-platform duplicate/simulcast detection, conservative by design.

    An exact match — identical `(platform, external_id)` or an identical normalized
    `canonical_url` — merges unconditionally. Everything else requires the same creator
    (same `owner_member_id`), a bounded observation window, AND either a matching
    normalized `outbound_url` or a matching `media_fingerprint`. Title similarity alone
    never merges, and missing evidence never merges: ambiguous crossposts stay separate
    so Krubit never silently drops or suppresses a legitimately distinct post.
    """

    def __init__(self, *, window: timedelta = _DEFAULT_CORRELATION_WINDOW) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        self._window = window

    def correlate(
        self, first: CorrelationCandidate, second: CorrelationCandidate
    ) -> CorrelationDecision:
        if first.guild_id != second.guild_id:
            raise ValueError("correlation candidates must belong to the same guild")

        if (first.platform, first.external_id) == (second.platform, second.external_id):
            return CorrelationDecision(
                merge=True,
                correlation_group=self._group(first, second),
                reason="identical content identity",
            )

        if _normalize_url(first.canonical_url) == _normalize_url(second.canonical_url):
            return CorrelationDecision(
                merge=True,
                correlation_group=self._group(first, second),
                reason="identical canonical url",
            )

        if first.owner_member_id != second.owner_member_id:
            return CorrelationDecision(
                merge=False, correlation_group=None, reason="ambiguous: different creator"
            )

        if not self._within_window(first, second):
            return CorrelationDecision(
                merge=False,
                correlation_group=None,
                reason="ambiguous: outside correlation window",
            )

        if self._strong_match(first, second):
            return CorrelationDecision(
                merge=True,
                correlation_group=self._group(first, second),
                reason="strong simulcast match: outbound link or media fingerprint",
            )

        return CorrelationDecision(
            merge=False,
            correlation_group=None,
            reason="ambiguous: no strong correlation evidence",
        )

    def _within_window(self, first: CorrelationCandidate, second: CorrelationCandidate) -> bool:
        if first.published_at is None or second.published_at is None:
            return False
        return abs(first.published_at - second.published_at) <= self._window

    @staticmethod
    def _strong_match(first: CorrelationCandidate, second: CorrelationCandidate) -> bool:
        outbound_match = (
            first.outbound_url is not None
            and second.outbound_url is not None
            and _normalize_url(first.outbound_url) == _normalize_url(second.outbound_url)
        )
        fingerprint_match = (
            first.media_fingerprint is not None
            and second.media_fingerprint is not None
            and first.media_fingerprint == second.media_fingerprint
        )
        return outbound_match or fingerprint_match

    @staticmethod
    def _group(first: CorrelationCandidate, second: CorrelationCandidate) -> str:
        identities = sorted(
            (
                f"{first.platform.value}:{first.external_id}",
                f"{second.platform.value}:{second.external_id}",
            )
        )
        return "|".join(identities)
