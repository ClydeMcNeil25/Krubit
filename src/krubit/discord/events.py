"""Deterministic conversion of Discord gateway facts into durable events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256

from krubit.domain.models import GuildEvent, JSONValue
from krubit.security.redaction import redact


def guild_event(
    event_type: str,
    guild_id: int,
    entity_id: int,
    occurred_at: datetime,
    before: Mapping[str, JSONValue] | None,
    after: Mapping[str, JSONValue] | None,
) -> GuildEvent:
    payload: dict[str, JSONValue] = {
        "entity_id": str(entity_id),
        "before": dict(before) if before is not None else None,
        "after": dict(after) if after is not None else None,
    }
    safe_payload = redact(payload)
    canonical = json.dumps(
        {"guild_id": str(guild_id), "event_type": event_type, "payload": safe_payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(canonical.encode()).hexdigest()
    return GuildEvent(
        event_id=f"{event_type}:{entity_id}:{digest}",
        guild_id=guild_id,
        event_type=event_type,
        occurred_at=occurred_at,
        payload=safe_payload if isinstance(safe_payload, dict) else payload,
    )
