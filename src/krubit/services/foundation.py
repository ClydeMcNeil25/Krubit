"""Phase 0 orchestration independent of the Discord framework."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from krubit.contracts.zariya import ZariyaSignal
from krubit.domain.models import ActionReceipt, Card, CardField, GuildEvent, StatusSnapshot
from krubit.storage.sqlite import SQLiteStore


class AuthorizationError(PermissionError):
    """Raised when a caller lacks authority for a foundation action."""


class GuildDisabledError(RuntimeError):
    """Raised when an event targets a guild not enabled for Krubit."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    duplicate: bool


class FoundationService:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def _receipt(
        self,
        *,
        guild_id: int,
        action: str,
        status: str,
        actor_id: int | None,
        detail: dict[str, str | bool | int],
    ) -> ActionReceipt:
        receipt = ActionReceipt(
            receipt_id=f"receipt:{uuid4().hex}",
            guild_id=guild_id,
            action=action,
            status=status,
            actor_id=actor_id,
            detail=detail,
            created_at=datetime.now(UTC),
        )
        await self._store.record_receipt(receipt)
        return receipt

    async def _require_enabled(self, guild_id: int, *, action: str) -> None:
        if await self._store.guild_is_enabled(guild_id):
            return
        await self._receipt(
            guild_id=guild_id,
            action=action,
            status="rejected",
            actor_id=None,
            detail={"reason": "guild_disabled"},
        )
        raise GuildDisabledError(f"guild {guild_id} is not enabled")

    async def _require_manager(
        self, guild_id: int, actor_id: int, can_manage_guild: bool, *, action: str
    ) -> None:
        if can_manage_guild:
            return
        await self._receipt(
            guild_id=guild_id,
            action=action,
            status="denied",
            actor_id=actor_id,
            detail={"reason": "manage_guild_required"},
        )
        raise AuthorizationError("Manage Guild permission is required")

    async def ingest(self, event: GuildEvent) -> IngestResult:
        await self._require_enabled(event.guild_id, action="ingest_event")
        accepted = await self._store.accept_event(event)
        await self._receipt(
            guild_id=event.guild_id,
            action="ingest_event",
            status="succeeded" if accepted else "duplicate",
            actor_id=None,
            detail={"event_id": event.event_id, "accepted": accepted},
        )
        return IngestResult(accepted=accepted, duplicate=not accepted)

    async def status(self, guild_id: int) -> StatusSnapshot:
        enabled = await self._store.guild_is_enabled(guild_id)
        event_count, receipt_count = await self._store.counts(guild_id)
        await self._receipt(
            guild_id=guild_id,
            action="fetch_status",
            status="succeeded",
            actor_id=None,
            detail={"enabled": enabled},
        )
        return StatusSnapshot(
            guild_id=guild_id,
            enabled=enabled,
            event_count=event_count,
            receipt_count=receipt_count + 1,
            database_healthy=True,
        )

    async def test_card(self, guild_id: int, actor_id: int, can_manage_guild: bool) -> Card:
        await self._require_enabled(guild_id, action="fetch_test_card")
        await self._require_manager(
            guild_id, actor_id, can_manage_guild, action="fetch_test_card"
        )
        status = await self.status(guild_id)
        await self._receipt(
            guild_id=guild_id,
            action="fetch_test_card",
            status="succeeded",
            actor_id=actor_id,
            detail={"card_kind": "fetched"},
        )
        return Card(
            kind="fetched",
            title="🦴 Fetched: Phase 0 Status",
            description="Krubit's foundation is connected and healthy.",
            fields=(
                CardField("Guild", str(guild_id), inline=True),
                CardField("Events", str(status.event_count), inline=True),
                CardField("Database", "Healthy", inline=True),
            ),
        )

    async def test_signal(
        self, guild_id: int, actor_id: int, can_manage_guild: bool
    ) -> ZariyaSignal:
        await self._require_enabled(guild_id, action="emit_test_signal")
        await self._require_manager(
            guild_id, actor_id, can_manage_guild, action="emit_test_signal"
        )
        source_event_id = f"phase0-smoke-{uuid4().hex}"
        signal = ZariyaSignal.create_test(
            guild_id=guild_id,
            source_event_id=source_event_id,
            occurred_at=datetime.now(UTC),
        )
        await self._receipt(
            guild_id=guild_id,
            action="emit_test_signal",
            status="succeeded",
            actor_id=actor_id,
            detail={"signal_id": signal.signal_id},
        )
        return signal

