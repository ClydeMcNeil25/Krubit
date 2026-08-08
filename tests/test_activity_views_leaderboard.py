"""Unit tests for `krubit.services.activity_views.leaderboard`.

Uses a real on-disk `SQLiteStore` (never mocked), matching every sibling
`activity_views` test file's convention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.domain.activity_ledger import MessageEvent, RetentionPolicy
from krubit.services.activity_views import leaderboard
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_leaderboard_defaults_to_current_year_and_ranks_by_count(
    store: SQLiteStore,
) -> None:
    inside_year = datetime(2026, 3, 1, tzinfo=UTC)
    for _ in range(3):
        await store.record_ledger_event(
            MessageEvent(guild_id=111, member_id=1, occurred_at=inside_year, channel_id=333)
        )
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=2, occurred_at=inside_year, channel_id=333)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.year == 2026
    assert result.retention_caveat is False
    assert [entry.member_id for entry in result.entries] == [1, 2]
    assert [entry.count for entry in result.entries] == [3, 1]


@pytest.mark.asyncio
async def test_leaderboard_past_year_uses_full_calendar_year_not_bounded_by_now(
    store: SQLiteStore,
) -> None:
    december_2025 = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    await store.record_ledger_event(
        MessageEvent(guild_id=111, member_id=1, occurred_at=december_2025, channel_id=333)
    )

    result = await leaderboard(store, 111, year=2025, now=NOW)

    assert result.year == 2025
    assert [entry.member_id for entry in result.entries] == [1]


@pytest.mark.asyncio
async def test_leaderboard_truncates_to_top_ten_and_omits_zero_activity(
    store: SQLiteStore,
) -> None:
    inside_year = datetime(2026, 3, 1, tzinfo=UTC)
    for member_id in range(1, 13):
        for _ in range(member_id):
            await store.record_ledger_event(
                MessageEvent(
                    guild_id=111, member_id=member_id, occurred_at=inside_year, channel_id=333
                )
            )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert len(result.entries) == 10
    assert [entry.member_id for entry in result.entries] == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_true_when_policy_shorter_than_elapsed_span(
    store: SQLiteStore,
) -> None:
    # NOW is 2026-08-08; year start is 2026-01-01 -> 219 days elapsed.
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=90, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is True


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_false_when_no_policy_configured(
    store: SQLiteStore,
) -> None:
    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is False


@pytest.mark.asyncio
async def test_leaderboard_retention_caveat_false_when_policy_covers_full_elapsed_span(
    store: SQLiteStore,
) -> None:
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=365, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2026, now=NOW)

    assert result.retention_caveat is False


@pytest.mark.asyncio
async def test_leaderboard_past_year_retention_caveat_uses_full_year_span(
    store: SQLiteStore,
) -> None:
    # For a fully-elapsed past year, the relevant span is the whole year
    # (365/366 days), not bounded by `now` -- a 90-day policy cannot cover it.
    await store.save_retention_policy(
        RetentionPolicy(guild_id=111, max_age_days=90, updated_by=555, updated_at=NOW)
    )

    result = await leaderboard(store, 111, year=2025, now=NOW)

    assert result.retention_caveat is True
