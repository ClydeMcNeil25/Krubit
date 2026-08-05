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
from datetime import datetime
from uuid import uuid4

from krubit.domain.creator_signals import (
    ContentKind,
    ContentObservation,
    ContentState,
    CreatorAccount,
    IngestionResult,
)
from krubit.domain.models import JSONValue
from krubit.integrations.base import ConnectorPage
from krubit.storage.sqlite import SQLiteStore


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
