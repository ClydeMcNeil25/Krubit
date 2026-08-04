"""Snapshot versioning, comparison, and read-only restore previews."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from krubit.discord.inventory import InventoryCapture
from krubit.domain.companion import DiffItem, SnapshotDiff, SnapshotRecord
from krubit.domain.models import JSONValue
from krubit.storage.sqlite import SQLiteStore

_RESOURCE_SECTIONS = ("roles", "channels", "scheduled_events", "automod_rules", "webhooks")
_CHANGE_ORDER = {"added": 0, "removed": 1, "modified": 2}


def _resources(content: dict[str, JSONValue], section: str) -> dict[str, dict[str, JSONValue]]:
    value = content.get(section, [])
    if not isinstance(value, list):
        return {}
    resources: dict[str, dict[str, JSONValue]] = {}
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, JSONValue], raw)
        resource_id = item.get("id")
        if isinstance(resource_id, str):
            resources[resource_id] = item
    return resources


def compare_inventory(
    older: dict[str, JSONValue], newer: dict[str, JSONValue], *, direction: str = "older_to_newer"
) -> SnapshotDiff:
    items: list[DiffItem] = []
    for section in _RESOURCE_SECTIONS:
        before = _resources(older, section)
        after = _resources(newer, section)
        for resource_id in sorted(after.keys() - before.keys(), key=int):
            items.append(DiffItem(section, resource_id, "added", {"after": after[resource_id]}))
        for resource_id in sorted(before.keys() - after.keys(), key=int):
            items.append(
                DiffItem(section, resource_id, "removed", {"before": before[resource_id]})
            )
        for resource_id in sorted(before.keys() & after.keys(), key=int):
            changed: dict[str, JSONValue] = {}
            for field in sorted(before[resource_id].keys() | after[resource_id].keys()):
                old_value = before[resource_id].get(field)
                new_value = after[resource_id].get(field)
                if old_value != new_value:
                    changed[field] = {"before": old_value, "after": new_value}
            if changed:
                items.append(DiffItem(section, resource_id, "modified", changed))
    items.sort(key=lambda item: (item.section, _CHANGE_ORDER[item.change], int(item.resource_id)))
    return SnapshotDiff(direction=direction, items=tuple(items))


class SnapshotService:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def latest(self, guild_id: int) -> SnapshotRecord | None:
        return await self._store.latest_snapshot(guild_id)

    async def capture(
        self,
        guild_id: int,
        inventory: InventoryCapture,
        captured_at: datetime,
    ) -> SnapshotRecord:
        return await self._store.save_snapshot(
            guild_id, inventory.content, inventory.coverage, captured_at
        )

    async def compare(self, guild_id: int, older_id: str, newer_id: str) -> SnapshotDiff:
        older = await self._store.get_snapshot(guild_id, older_id)
        newer = await self._store.get_snapshot(guild_id, newer_id)
        if older is None or newer is None:
            raise LookupError("snapshot not found for this guild")
        return compare_inventory(older.content, newer.content)

    async def preview_restore(
        self,
        guild_id: int,
        target_id: str,
        current: InventoryCapture,
    ) -> SnapshotDiff:
        target = await self._store.get_snapshot(guild_id, target_id)
        if target is None:
            raise LookupError("snapshot not found for this guild")
        return compare_inventory(
            current.content,
            target.content,
            direction="current_to_target",
        )
