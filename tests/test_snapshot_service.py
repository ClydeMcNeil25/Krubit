from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.discord.inventory import InventoryCapture
from krubit.services.snapshots import SnapshotService, compare_inventory
from krubit.storage.sqlite import SQLiteStore


def test_compare_inventory_treats_same_id_rename_as_modification() -> None:
    diff = compare_inventory(
        {"channels": [{"id": "10", "name": "old-name"}]},
        {"channels": [{"id": "10", "name": "new-name"}, {"id": "20", "name": "new"}]},
    )

    assert [(item.resource_id, item.change) for item in diff.items] == [
        ("20", "added"),
        ("10", "modified"),
    ]
    assert diff.items[1].fields == {"name": {"before": "old-name", "after": "new-name"}}


@pytest.mark.asyncio
async def test_restore_preview_reads_target_without_saving_or_applying(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    service = SnapshotService(store)
    captured = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    try:
        target = await service.capture(
            111,
            InventoryCapture({"roles": [{"id": "1", "name": "Original"}]}, ()),
            captured,
        )
        preview = await service.preview_restore(
            111,
            target.snapshot_id,
            InventoryCapture({"roles": [{"id": "1", "name": "Changed"}]}, ()),
        )

        assert preview.direction == "current_to_target"
        assert preview.items[0].fields["name"] == {
            "before": "Changed",
            "after": "Original",
        }
        assert not hasattr(service, "apply_restore")
        assert await store.latest_snapshot(111) == target
    finally:
        await store.close()
