from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.storage.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def _store(tmp_path: Path) -> SQLiteStore:
    store = await SQLiteStore.open(tmp_path / "test.db")
    await store.initialize()
    return store


async def test_save_and_get_data_deletion_request_round_trips(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-abc123",
        authorization_subject_id="subj-1",
        platform="facebook_page",
        requested_at=now,
        rows_deleted=2,
    )
    row = await store.get_data_deletion_request("conf-abc123")
    assert row is not None
    assert row.authorization_subject_id == "subj-1"
    assert row.rows_deleted == 2
    await store.close()


async def test_get_data_deletion_request_returns_none_for_unknown_code(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.get_data_deletion_request("nope") is None
    await store.close()


async def test_find_recent_data_deletion_request_within_window(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-1", authorization_subject_id="subj-1",
        platform="facebook_page", requested_at=now, rows_deleted=1,
    )
    found = await store.find_recent_data_deletion_request(
        "subj-1", "facebook_page", since=now - timedelta(minutes=5)
    )
    assert found is not None
    assert found.confirmation_code == "conf-1"
    await store.close()


async def test_find_recent_data_deletion_request_outside_window_returns_none(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    await store.save_data_deletion_request(
        confirmation_code="conf-1", authorization_subject_id="subj-1",
        platform="facebook_page", requested_at=now - timedelta(minutes=10),
        rows_deleted=1,
    )
    found = await store.find_recent_data_deletion_request(
        "subj-1", "facebook_page", since=now - timedelta(minutes=5)
    )
    assert found is None
    await store.close()
