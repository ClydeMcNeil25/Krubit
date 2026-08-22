"""Integration tests for the moderation storage service (slice 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from krubit.storage.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def _table_names(store: SQLiteStore) -> set[str]:
    cursor = await store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


@pytest.mark.asyncio
async def test_moderation_tables_exist_after_initialize(store: SQLiteStore) -> None:
    names = await _table_names(store)
    assert "moderation_cases" in names
    assert "moderation_action_receipts" in names
    assert "moderation_idempotency_keys" in names
