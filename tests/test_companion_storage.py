from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.domain.companion import CoverageIssue
from krubit.domain.models import GuildEvent
from krubit.storage.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_snapshot_versions_are_deduplicated_and_guild_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    captured = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    try:
        first = await store.save_snapshot(111, {"roles": [{"id": "1"}]}, (), captured)
        duplicate = await store.save_snapshot(111, {"roles": [{"id": "1"}]}, (), captured)
        other = await store.save_snapshot(222, {"roles": [{"id": "1"}]}, (), captured)

        assert first.snapshot_id == duplicate.snapshot_id
        assert first.version == duplicate.version == 1
        assert other.snapshot_id != first.snapshot_id
        assert await store.get_snapshot(222, first.snapshot_id) is None
        assert await store.latest_snapshot(111) == first
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_snapshot_hash_includes_redacted_content_and_coverage(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    captured = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    try:
        first = await store.save_snapshot(
            111,
            {"integration": {"token": "secret-one"}},
            (CoverageIssue("webhooks", "limited", "forbidden"),),
            captured,
        )
        duplicate = await store.save_snapshot(
            111,
            {"integration": {"token": "secret-two"}},
            (CoverageIssue("webhooks", "limited", "forbidden"),),
            captured + timedelta(minutes=1),
        )

        assert duplicate.snapshot_id == first.snapshot_id
        assert first.content == {"integration": {"token": "[REDACTED]"}}
        assert first.coverage[0].section == "webhooks"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_events_returns_only_requested_guild_newest_first(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    captured = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    try:
        for guild_id, suffix, offset in ((111, "old", 0), (222, "other", 1), (111, "new", 2)):
            await store.accept_event(
                GuildEvent(
                    event_id=f"event-{suffix}",
                    guild_id=guild_id,
                    event_type="role_updated",
                    occurred_at=captured + timedelta(minutes=offset),
                    payload={"suffix": suffix},
                )
            )

        events = await store.list_events(111, limit=2)

        assert [event.event_id for event in events] == ["event-new", "event-old"]
        assert all(event.guild_id == 111 for event in events)
    finally:
        await store.close()
