from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.domain.models import GuildEvent
from krubit.services.foundation import (
    AuthorizationError,
    FoundationService,
    GuildDisabledError,
)
from krubit.storage.sqlite import SQLiteStore


def sample_event(guild_id: int) -> GuildEvent:
    return GuildEvent(
        event_id="ready-1",
        guild_id=guild_id,
        event_type="guild_available",
        occurred_at=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
        payload={"debug": "api_key=never-store"},
    )


@pytest.mark.asyncio
async def test_disabled_guild_rejects_ingestion_with_receipt(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    service = FoundationService(store)
    try:
        with pytest.raises(GuildDisabledError):
            await service.ingest(sample_event(111))

        receipts = await store.list_receipts(111)
        assert len(receipts) == 1
        assert receipts[0].status == "rejected"
        assert receipts[0].detail == {"reason": "guild_disabled"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ingestion_is_idempotent_and_status_is_tenant_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    await store.set_guild_enabled(222, True)
    service = FoundationService(store)
    try:
        first = await service.ingest(sample_event(111))
        duplicate = await service.ingest(sample_event(111))
        other = await service.status(222)
        status = await service.status(111)

        assert first.accepted is True
        assert duplicate.accepted is False
        assert duplicate.duplicate is True
        assert other.event_count == 0
        assert status.event_count == 1
        assert status.database_healthy is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_test_card_requires_manage_guild_and_records_denial(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    service = FoundationService(store)
    try:
        with pytest.raises(AuthorizationError):
            await service.test_card(111, actor_id=42, can_manage_guild=False)

        card = await service.test_card(111, actor_id=7, can_manage_guild=True)
        receipts = await store.list_receipts(111)

        assert card.kind == "fetched"
        assert card.title == "🦴 Fetched: Phase 0 Status"
        assert {receipt.status for receipt in receipts} == {"denied", "succeeded"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_test_signal_is_safe_and_requires_manage_guild(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    service = FoundationService(store)
    try:
        with pytest.raises(AuthorizationError):
            await service.test_signal(111, actor_id=42, can_manage_guild=False)

        signal = await service.test_signal(111, actor_id=7, can_manage_guild=True)

        assert signal.guild_id == 111
        assert signal.kind == "foundation_test"
        assert signal.action_request is None
        assert signal.evidence == {"phase": 0, "member_data": False}
    finally:
        await store.close()

