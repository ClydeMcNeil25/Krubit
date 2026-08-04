from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from krubit.domain.models import ActionReceipt, GuildEvent
from krubit.storage.sqlite import SQLiteStore


def event(guild_id: int, payload: dict[str, object] | None = None) -> GuildEvent:
    return GuildEvent(
        event_id="gateway-event-1",
        guild_id=guild_id,
        event_type="guild_available",
        occurred_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        payload=payload or {"name": "Krucial Town"},  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_event_deduplication_is_scoped_to_each_guild(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        assert await store.accept_event(event(111)) is True
        assert await store.accept_event(event(111)) is False
        assert await store.accept_event(event(222)) is True

        first = await store.get_event(111, "gateway-event-1")
        second = await store.get_event(222, "gateway-event-1")
        missing = await store.get_event(333, "gateway-event-1")

        assert first is not None and first.guild_id == 111
        assert second is not None and second.guild_id == 222
        assert missing is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_payload_is_redacted_before_storage(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.accept_event(event(111, {"debug": "bot_token=do-not-store"}))

        stored = await store.get_event(111, "gateway-event-1")

        assert stored is not None
        assert stored.payload == {"debug": "bot_token=[REDACTED]"}
        async with aiosqlite.connect(tmp_path / "krubit.db") as connection:
            cursor = await connection.execute("SELECT payload_json FROM guild_events")
            row = await cursor.fetchone()
            assert row is not None
            raw_payload = str(row[0])
        assert "do-not-store" not in raw_payload
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_receipts_and_counts_are_tenant_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.record_receipt(
            ActionReceipt(
                receipt_id="receipt-111",
                guild_id=111,
                action="status",
                status="succeeded",
                actor_id=42,
                detail={"debug": "password: hidden"},
                created_at=datetime(2026, 8, 3, 12, 1, tzinfo=UTC),
            )
        )
        await store.record_receipt(
            ActionReceipt(
                receipt_id="receipt-222",
                guild_id=222,
                action="status",
                status="succeeded",
                actor_id=43,
                detail={},
                created_at=datetime(2026, 8, 3, 12, 2, tzinfo=UTC),
            )
        )

        first_receipts = await store.list_receipts(111)
        second_receipts = await store.list_receipts(222)
        first_counts = await store.counts(111)

        assert [item.receipt_id for item in first_receipts] == ["receipt-111"]
        assert first_receipts[0].detail == {"debug": "password: [REDACTED]"}
        assert [item.receipt_id for item in second_receipts] == ["receipt-222"]
        assert first_counts == (0, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_guild_enablement_defaults_to_false_and_is_isolated(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        assert await store.guild_is_enabled(111) is False
        await store.set_guild_enabled(111, True)
        assert await store.guild_is_enabled(111) is True
        assert await store.guild_is_enabled(222) is False
    finally:
        await store.close()
