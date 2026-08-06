"""Tests for `krubit.services.activity_privacy`: retention sweep, member deletion,
and member data export.

Exercises the service functions against a real on-disk `SQLiteStore` (never mocked),
matching `test_activity_ledger_storage.py`'s convention. Per the plan's own
verification step, deletion-completeness and cross-member export leakage get
dedicated coverage, not just happy paths.

## Why the deletion-completeness test here differs from the storage-layer one

`test_activity_ledger_storage.py::test_delete_member_ledger_data_seeds_and_clears_every_member_scoped_table`
already proves `SQLiteStore.delete_member_ledger_data` alone leaves *zero* rows in
every member-scoped table. `delete_member` (this module) wraps that call and then
deliberately writes ONE new row back into `activity_receipts`: its own durable
deletion receipt (design doc: "a durable receipt recording that deletion occurred").
So the service-layer completeness property is "every member-scoped table is empty
EXCEPT `activity_receipts`, which holds exactly the fresh minimal deletion receipt
and nothing else" — not literally zero everywhere. See
`test_delete_member_clears_every_table_except_its_own_minimal_receipt` below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite
import pytest

from krubit.domain.activity_ledger import LedgerEvent, MessageEvent, Milestone, MilestoneKind
from krubit.services.activity_privacy import (
    ACTIVITY_LEDGER_TABLES,
    ALL_MEMBER_SCOPED_TABLES,
    ActivityReceipt,
    MemberExportPackage,
    RetentionSweepService,
    delete_member,
    export_member_data,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "krubit.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(db_path)
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def ledger_event(
    *, guild_id: int = 111, member_id: int = 222, occurred_at: datetime = NOW
) -> LedgerEvent:
    return MessageEvent(
        guild_id=guild_id, member_id=member_id, occurred_at=occurred_at, channel_id=333
    )


def milestone(*, guild_id: int = 111, member_id: int = 222) -> Milestone:
    return Milestone(
        guild_id=guild_id,
        member_id=member_id,
        kind=MilestoneKind.MESSAGE_COUNT,
        reached_at=NOW,
        detail="reached 10 messages",
    )


async def seed_ledger_event(store: SQLiteStore, *, guild_id: int, member_id: int) -> None:
    await store.record_ledger_event(ledger_event(guild_id=guild_id, member_id=member_id))


async def seed_full_member_footprint(
    store: SQLiteStore, *, guild_id: int, member_id: int
) -> None:
    """Seed one row in every table `ALL_MEMBER_SCOPED_TABLES` names for one member."""
    await store.record_ledger_event(ledger_event(guild_id=guild_id, member_id=member_id))
    await store.save_milestone(milestone(guild_id=guild_id, member_id=member_id))
    await store.record_activity_receipt(
        guild_id=guild_id,
        receipt_id=str(uuid4()),
        member_id=member_id,
        action="milestone_reached",
        detail={"milestone_kind": "message_count"},
        created_at=NOW,
    )


async def table_row_count(db_path: Path, table: str, *, guild_id: int, member_id: int) -> int:
    assert table in ACTIVITY_LEDGER_TABLES  # guard against interpolating arbitrary SQL
    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND member_id = ?",
            (guild_id, member_id),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


# -- ALL_MEMBER_SCOPED_TABLES drift check ------------------------------------------


@pytest.mark.asyncio
async def test_all_member_scoped_tables_matches_live_schema(
    store: SQLiteStore, db_path: Path
) -> None:
    """Cross-checks `ALL_MEMBER_SCOPED_TABLES` against a live `PRAGMA table_info`
    scan of every table in `ACTIVITY_LEDGER_TABLES`, so a future activity-ledger
    table with a `member_id` column that isn't added to `ALL_MEMBER_SCOPED_TABLES`
    fails this test instead of silently drifting.
    """
    derived: list[str] = []
    async with aiosqlite.connect(db_path) as connection:
        for table in ACTIVITY_LEDGER_TABLES:
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "member_id" in columns:
                derived.append(table)
    assert set(derived) == set(ALL_MEMBER_SCOPED_TABLES)


# -- delete_member: completeness ----------------------------------------------------


@pytest.mark.asyncio
async def test_delete_member_clears_every_table_except_its_own_minimal_receipt(
    store: SQLiteStore, db_path: Path
) -> None:
    await seed_full_member_footprint(store, guild_id=111, member_id=222)
    assert await table_row_count(db_path, "activity_receipts", guild_id=111, member_id=222) == 1

    await delete_member(store, 111, 222, requested_by=999, now=NOW)

    assert await table_row_count(db_path, "ledger_events", guild_id=111, member_id=222) == 0
    assert await table_row_count(db_path, "milestones", guild_id=111, member_id=222) == 0
    # The seeded "milestone_reached" receipt is gone too (deleted along with
    # everything else); the single surviving row is the fresh deletion receipt.
    assert await table_row_count(db_path, "activity_receipts", guild_id=111, member_id=222) == 1


@pytest.mark.asyncio
async def test_delete_member_does_not_affect_other_members_or_guilds(
    store: SQLiteStore, db_path: Path
) -> None:
    await seed_full_member_footprint(store, guild_id=111, member_id=222)
    await seed_full_member_footprint(store, guild_id=111, member_id=333)
    await seed_full_member_footprint(store, guild_id=999, member_id=222)

    await delete_member(store, 111, 222, requested_by=999, now=NOW)

    for table in ALL_MEMBER_SCOPED_TABLES:
        assert await table_row_count(db_path, table, guild_id=111, member_id=333) == 1
        assert await table_row_count(db_path, table, guild_id=999, member_id=222) == 1


# -- delete_member: receipt content is minimal and content-free --------------------


@pytest.mark.asyncio
async def test_delete_member_returns_receipt_with_only_minimal_facts(
    store: SQLiteStore,
) -> None:
    await seed_full_member_footprint(store, guild_id=111, member_id=222)

    receipt = await delete_member(store, 111, 222, requested_by=999, now=NOW)

    assert isinstance(receipt, ActivityReceipt)
    assert receipt.guild_id == 111
    assert receipt.member_id == 222
    assert receipt.action == "member_data_deleted"
    assert receipt.created_at == NOW
    # Exactly two facts: who asked, and when. Nothing describing what was deleted
    # (no table names, no row counts, no event kinds, no detail summaries).
    assert set(receipt.detail.keys()) == {"requested_by", "deleted_at"}
    assert receipt.detail["requested_by"] == 999
    assert receipt.detail["deleted_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_delete_member_persisted_receipt_matches_returned_receipt(
    store: SQLiteStore, db_path: Path
) -> None:
    """The receipt actually written to storage carries the same minimal detail as
    the one returned to the caller -- not a richer version persisted silently.
    """
    await seed_full_member_footprint(store, guild_id=111, member_id=222)
    receipt = await delete_member(store, 111, 222, requested_by=999, now=NOW)

    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute(
            "SELECT action, detail_json FROM activity_receipts "
            "WHERE guild_id = ? AND member_id = ? AND receipt_id = ?",
            (111, 222, receipt.receipt_id),
        )
        row = await cursor.fetchone()
    assert row is not None
    import json

    assert row[0] == "member_data_deleted"
    assert json.loads(row[1]) == {"requested_by": 999, "deleted_at": NOW.isoformat()}


@pytest.mark.asyncio
async def test_delete_member_is_idempotent(store: SQLiteStore) -> None:
    await seed_ledger_event(store, guild_id=111, member_id=222)
    await delete_member(store, 111, 222, requested_by=999, now=NOW)
    # Deleting again (nothing left but the prior deletion receipt) must not raise.
    second_receipt = await delete_member(store, 111, 222, requested_by=999, now=NOW)
    assert second_receipt.action == "member_data_deleted"


# -- export_member_data: no cross-member leakage ------------------------------------


@pytest.mark.asyncio
async def test_export_never_includes_another_members_data(store: SQLiteStore) -> None:
    await seed_ledger_event(store, guild_id=111, member_id=222)
    await seed_ledger_event(store, guild_id=111, member_id=333)

    package = await export_member_data(store, 111, 222, now=NOW)

    assert isinstance(package, MemberExportPackage)
    assert all(e.member_id == 222 for e in package.events)


@pytest.mark.asyncio
async def test_export_never_includes_another_guilds_data(store: SQLiteStore) -> None:
    await seed_ledger_event(store, guild_id=111, member_id=222)
    await seed_ledger_event(store, guild_id=999, member_id=222)

    package = await export_member_data(store, 111, 222, now=NOW)

    assert all(e.guild_id == 111 for e in package.events)


@pytest.mark.asyncio
async def test_export_includes_own_milestones_only(store: SQLiteStore) -> None:
    await store.save_milestone(milestone(guild_id=111, member_id=222))
    await store.save_milestone(milestone(guild_id=111, member_id=333))

    package = await export_member_data(store, 111, 222, now=NOW)

    assert len(package.milestones) == 1
    assert package.milestones[0].member_id == 222


@pytest.mark.asyncio
async def test_export_returns_empty_package_for_member_with_no_data(
    store: SQLiteStore,
) -> None:
    package = await export_member_data(store, 111, 222, now=NOW)
    assert package.events == ()
    assert package.milestones == ()


@pytest.mark.asyncio
async def test_export_is_not_truncated_beyond_the_interactive_view_cap(
    store: SQLiteStore,
) -> None:
    """`list_ledger_events` (interactive views) caps at 500 rows; export must not
    silently inherit that cap -- a data export has to be complete.
    """
    for i in range(510):
        await store.record_ledger_event(
            ledger_event(guild_id=111, member_id=222, occurred_at=NOW - timedelta(minutes=i))
        )

    package = await export_member_data(store, 111, 222, now=NOW)

    assert len(package.events) == 510


# -- RetentionSweepService: only raw ledger rows age out ----------------------------


@pytest.mark.asyncio
async def test_sweep_prunes_ledger_events_older_than_the_retention_window(
    store: SQLiteStore,
) -> None:
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=30, updated_by=999, updated_at=NOW)
    )
    old_event = ledger_event(occurred_at=NOW - timedelta(days=31))
    recent_event = ledger_event(occurred_at=NOW - timedelta(days=29))
    await store.record_ledger_event(old_event)
    await store.record_ledger_event(recent_event)

    service = RetentionSweepService(store)
    removed = await service.sweep(111, NOW)

    assert removed == 1
    remaining = await store.list_ledger_events(111, member_id=222)
    assert len(remaining) == 1
    assert remaining[0].occurred_at == recent_event.occurred_at


@pytest.mark.asyncio
async def test_sweep_boundary_keeps_row_exactly_at_the_cutoff(store: SQLiteStore) -> None:
    """A row aged exactly `max_age_days` old is not yet pruned; only rows strictly
    older than the cutoff are removed.
    """
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=30, updated_by=999, updated_at=NOW)
    )
    boundary_event = ledger_event(occurred_at=NOW - timedelta(days=30))
    await store.record_ledger_event(boundary_event)

    service = RetentionSweepService(store)
    removed = await service.sweep(111, NOW)

    assert removed == 0
    assert len(await store.list_ledger_events(111, member_id=222)) == 1


@pytest.mark.asyncio
async def test_sweep_is_a_no_op_without_a_configured_retention_policy(
    store: SQLiteStore,
) -> None:
    await store.record_ledger_event(ledger_event(occurred_at=NOW - timedelta(days=999)))

    service = RetentionSweepService(store)
    removed = await service.sweep(111, NOW)

    assert removed == 0
    assert len(await store.list_ledger_events(111, member_id=222)) == 1


@pytest.mark.asyncio
async def test_sweep_never_prunes_milestones_or_activity_receipts(
    store: SQLiteStore, db_path: Path
) -> None:
    """Materialized milestone/receipt rows are retained even after the raw events
    they were computed from age out -- the design doc's explicit rule that only the
    raw event, not the fact that a milestone was reached, ages out.
    """
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=1, updated_by=999, updated_at=NOW)
    )
    await store.record_ledger_event(ledger_event(occurred_at=NOW - timedelta(days=100)))
    await store.save_milestone(milestone())
    await store.record_activity_receipt(
        guild_id=111,
        receipt_id="receipt-old",
        member_id=222,
        action="milestone_reached",
        detail={},
        created_at=NOW - timedelta(days=100),
    )

    service = RetentionSweepService(store)
    await service.sweep(111, NOW)

    assert await store.list_ledger_events(111, member_id=222) == ()
    assert len(await store.list_milestones(111, member_id=222)) == 1
    assert await table_row_count(db_path, "activity_receipts", guild_id=111, member_id=222) == 1


# -- RetentionSweepService: isolation ------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_isolates_one_prune_targets_failure_from_another(
    store: SQLiteStore,
) -> None:
    """A real concurrent-failure scenario at the per-target level: one prune target
    raises, another prune target for the same guild must still run to completion in
    the same `sweep` call.
    """
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=1, updated_by=999, updated_at=NOW)
    )
    await store.record_ledger_event(ledger_event(occurred_at=NOW - timedelta(days=10)))

    service = RetentionSweepService(store)
    calls: list[str] = []

    async def failing_target() -> int:
        calls.append("failing")
        raise RuntimeError("synthetic prune failure")

    async def working_target() -> int:
        calls.append("working")
        return await store.prune_ledger_events_older_than(111, NOW - timedelta(days=1))

    service._prune_targets = lambda guild_id, cutoff: (  # type: ignore[method-assign]
        ("failing_target", failing_target),
        ("ledger_events", working_target),
    )

    removed = await service.sweep(111, NOW)

    assert calls == ["failing", "working"]
    assert removed == 1
    assert await store.list_ledger_events(111, member_id=222) == ()


class _ExplodingRetentionPolicyStore:
    """Wraps a real `SQLiteStore`, raising from `get_retention_policy` for one
    specific guild and delegating everything else -- used to prove `sweep_all_guilds`
    isolates a real per-guild failure without weakening the rest of the store.
    """

    def __init__(self, inner: SQLiteStore, exploding_guild_id: int) -> None:
        self._inner = inner
        self._exploding_guild_id = exploding_guild_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def get_retention_policy(self, guild_id: int):
        if guild_id == self._exploding_guild_id:
            raise RuntimeError("synthetic retention-policy lookup failure for guild A")
        return await self._inner.get_retention_policy(guild_id)


@pytest.mark.asyncio
async def test_sweep_all_guilds_isolates_one_guilds_failure_from_another(
    store: SQLiteStore,
) -> None:
    """A real concurrent-failure scenario: guild A's retention-policy lookup raises,
    guild B's sweep must still run to completion in the same cycle -- mirroring
    `test_sweep_cycle_isolates_one_guilds_detector_failure_from_another_guild` in
    `test_watchdog_runtime.py`.
    """
    from krubit.domain.activity_ledger import RetentionPolicy

    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=1, updated_by=999, updated_at=NOW)
    )
    await store.save_retention_policy(
        RetentionPolicy(guild_id=222, max_age_days=1, updated_by=999, updated_at=NOW)
    )
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=10, occurred_at=NOW - timedelta(days=10), channel_id=1)
    )
    await store.record_ledger_event(
        MessageEvent(guild_id=222, member_id=20, occurred_at=NOW - timedelta(days=10), channel_id=1)
    )

    exploding_store = _ExplodingRetentionPolicyStore(store, exploding_guild_id=111)
    service = RetentionSweepService(cast(SQLiteStore, exploding_store))

    await service.sweep_all_guilds((111, 222), NOW)

    # Guild A's sweep raised before pruning anything -- its old event survives.
    assert len(await store.list_ledger_events(111, member_id=10)) == 1
    # Guild B's sweep must have completed despite guild A's failure.
    assert await store.list_ledger_events(222, member_id=20) == ()


# -- Validation ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_member_rejects_non_positive_ids(store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        await delete_member(store, 0, 222, requested_by=999, now=NOW)
    with pytest.raises(ValueError):
        await delete_member(store, 111, 222, requested_by=0, now=NOW)


@pytest.mark.asyncio
async def test_delete_member_rejects_naive_datetime(store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        await delete_member(
            store, 111, 222, requested_by=999, now=datetime(2026, 8, 6, 12, 0)
        )


@pytest.mark.asyncio
async def test_sweep_rejects_naive_datetime(store: SQLiteStore) -> None:
    service = RetentionSweepService(store)
    with pytest.raises(ValueError):
        await service.sweep(111, datetime(2026, 8, 6, 12, 0))
