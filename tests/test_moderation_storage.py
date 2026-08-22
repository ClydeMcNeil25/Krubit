"""Integration tests for the moderation storage service (slice 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.domain.moderation import (
    ActionReceipt,
    AppealStatus,
    ModerationCase,
    ModerationStatus,
)
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


def _case(**overrides: object) -> ModerationCase:
    fields: dict[str, object] = dict(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        offense_number=1,
        recommended_action=None,
        executed_action=None,
        action_expiration=None,
        status=ModerationStatus.RECORDED,
        review_deadline=None,
        reviewer_id=None,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )
    fields.update(overrides)
    return ModerationCase(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_save_and_get_moderation_case_round_trips(store: SQLiteStore) -> None:
    saved = await store.save_moderation_case(_case())
    assert saved.case_id == "case:1"
    fetched = await store.get_moderation_case("case:1")
    assert fetched == saved


@pytest.mark.asyncio
async def test_get_moderation_case_by_incident(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case())
    fetched = await store.get_moderation_case_by_incident(100, "incident:1")
    assert fetched is not None
    assert fetched.case_id == "case:1"
    assert await store.get_moderation_case_by_incident(100, "incident:missing") is None


@pytest.mark.asyncio
async def test_count_moderation_cases_for_member_excludes_overturned(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case(case_id="case:1", incident_id="incident:1"))
    await store.save_moderation_case(
        _case(case_id="case:2", incident_id="incident:2", appeal_status=AppealStatus.OVERTURNED)
    )
    assert await store.count_moderation_cases_for_member(100, 200) == 1


@pytest.mark.asyncio
async def test_save_and_get_action_receipt(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case())
    receipt = ActionReceipt(receipt_id="receipt:1", succeeded=True, detail="applied 24h timeout")
    await store.save_action_receipt(100, receipt, "case:1")
    fetched = await store.get_action_receipt_by_id("receipt:1")
    assert fetched is not None
    assert fetched[0] == receipt
    assert fetched[1] == "case:1"
    assert await store.get_action_receipt_by_id("receipt:missing") is None


@pytest.mark.asyncio
async def test_idempotency_cache_round_trips(store: SQLiteStore) -> None:
    assert await store.get_moderation_idempotency_response(100, "record_incident", "idem:1") is None
    await store.save_moderation_idempotency_response(
        100, "record_incident", "idem:1", "case:1", '{"case_id": "case:1"}'
    )
    cached = await store.get_moderation_idempotency_response(100, "record_incident", "idem:1")
    assert cached == '{"case_id": "case:1"}'


@pytest.mark.asyncio
async def test_list_pending_review_cases(store: SQLiteStore) -> None:
    await store.save_moderation_case(
        _case(case_id="case:1", incident_id="incident:1", status=ModerationStatus.RECORDED)
    )
    await store.save_moderation_case(
        _case(
            case_id="case:2",
            incident_id="incident:2",
            status=ModerationStatus.APPROVAL_REQUIRED,
        )
    )
    pending = await store.list_pending_review_cases(100)
    assert [case.case_id for case in pending] == ["case:2"]
