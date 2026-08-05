"""Integration tests for guild-scoped watchdog (Phase 3) persistence.

Exercises `SQLiteStore`'s watchdog methods against a real on-disk SQLite database
(never mocked), matching the `test_creator_registry_storage.py` convention. Per the
design doc's Testing and Rollout section, guild-isolation and idempotent-close
behavior get dedicated coverage, not just happy paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from krubit.domain.watchdog import (
    AllowBlockEntry,
    EntrySniffAssessment,
    Incident,
    IncidentKind,
    RiskBand,
    RiskSignal,
    WatchWindow,
    WatchWindowCloseReason,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
EVEN_LATER = datetime(2026, 8, 5, 13, 0, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "krubit.db"


def signals() -> tuple[RiskSignal, ...]:
    return (
        RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),
        RiskSignal(name="join_velocity", weight=4, detail="12 joins in 60s", confidence=0.8),
    )


def entry_sniff_assessment(
    *,
    guild_id: int = 111,
    member_id: int = 222,
    joined_at: datetime = NOW,
    band: RiskBand = RiskBand.SUSPICIOUS,
    created_at: datetime = NOW,
) -> EntrySniffAssessment:
    return EntrySniffAssessment(
        guild_id=guild_id,
        member_id=member_id,
        joined_at=joined_at,
        band=band,
        signals=signals(),
        explanation="suspicious band: two corroborating signals",
        created_at=created_at,
    )


def watch_window(
    *,
    guild_id: int = 111,
    member_id: int = 222,
    opened_at: datetime = NOW,
    expires_at: datetime = LATER,
    band: RiskBand = RiskBand.WATCH,
) -> WatchWindow:
    return WatchWindow(
        guild_id=guild_id,
        member_id=member_id,
        opened_at=opened_at,
        expires_at=expires_at,
        band=band,
        closed_at=None,
        close_reason=None,
    )


def incident(
    *,
    guild_id: int = 111,
    incident_id: str = "incident-1",
    kind: IncidentKind = IncidentKind.MEMBER,
    opened_at: datetime = NOW,
    evidence_packet_id: str = "evidence-1",
    recommended_action: str = "recommend a temporary role restriction, pending staff review",
    acknowledged_by: int | None = None,
) -> Incident:
    return Incident(
        guild_id=guild_id,
        incident_id=incident_id,
        kind=kind,
        band=RiskBand.INCIDENT,
        opened_at=opened_at,
        evidence_packet_id=evidence_packet_id,
        recommended_action=recommended_action,
        acknowledged_by=acknowledged_by,
    )


def allow_block_entry(
    *,
    guild_id: int = 111,
    discord_user_id: int = 333,
    list_kind: str = "allow",
    reason: str = "verified community partner",
    set_by: int = 444,
    set_at: datetime = NOW,
) -> AllowBlockEntry:
    return AllowBlockEntry(
        guild_id=guild_id,
        discord_user_id=discord_user_id,
        list_kind=list_kind,
        reason=reason,
        set_by=set_by,
        set_at=set_at,
    )


# -- entry_sniff_assessments --------------------------------------------------


@pytest.mark.asyncio
async def test_entry_sniff_assessments_are_guild_scoped(store: SQLiteStore) -> None:
    await store.save_entry_sniff_assessment(entry_sniff_assessment(guild_id=111, member_id=222))
    await store.save_entry_sniff_assessment(entry_sniff_assessment(guild_id=999, member_id=222))

    first = await store.get_entry_sniff_assessment(111, 222)
    second = await store.get_entry_sniff_assessment(999, 222)
    missing = await store.get_entry_sniff_assessment(555, 222)

    assert first is not None and first.guild_id == 111
    assert second is not None and second.guild_id == 999
    assert missing is None


@pytest.mark.asyncio
async def test_entry_sniff_assessment_round_trips_signals_and_explanation(
    store: SQLiteStore,
) -> None:
    saved = await store.save_entry_sniff_assessment(entry_sniff_assessment())

    assert saved.band is RiskBand.SUSPICIOUS
    assert saved.signals == signals()
    assert saved.explanation == "suspicious band: two corroborating signals"


@pytest.mark.asyncio
async def test_entry_sniff_assessment_signal_detail_is_redacted_before_storage(
    store: SQLiteStore, db_path: Path
) -> None:
    leaky_signals = (
        RiskSignal(
            name="webhook_flag", weight=3, detail="bot_token=do-not-store", confidence=0.9
        ),
    )
    await store.save_entry_sniff_assessment(
        EntrySniffAssessment(
            guild_id=111,
            member_id=222,
            joined_at=NOW,
            band=RiskBand.WATCH,
            signals=leaky_signals,
            explanation="watch band",
            created_at=NOW,
        )
    )

    stored = await store.get_entry_sniff_assessment(111, 222)
    assert stored is not None
    assert "do-not-store" not in stored.signals[0].detail

    async with aiosqlite.connect(db_path) as connection:
        cursor = await connection.execute("SELECT signals_json FROM entry_sniff_assessments")
        row = await cursor.fetchone()
        assert row is not None
        raw_signals_json = str(row[0])
    assert "do-not-store" not in raw_signals_json


@pytest.mark.asyncio
async def test_rejoin_creates_a_new_assessment_row_rather_than_overwriting(
    store: SQLiteStore,
) -> None:
    await store.save_entry_sniff_assessment(
        entry_sniff_assessment(joined_at=NOW, band=RiskBand.WATCH)
    )
    await store.save_entry_sniff_assessment(
        entry_sniff_assessment(joined_at=LATER, band=RiskBand.SUSPICIOUS)
    )

    first_join = await store.get_entry_sniff_assessment(111, 222, joined_at=NOW)
    most_recent = await store.get_entry_sniff_assessment(111, 222)

    assert first_join is not None and first_join.band is RiskBand.WATCH
    assert most_recent is not None and most_recent.joined_at == LATER
    assert most_recent.band is RiskBand.SUSPICIOUS


# -- watch_windows --------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_windows_are_guild_scoped(store: SQLiteStore) -> None:
    await store.open_watch_window(watch_window(guild_id=111, member_id=222))
    await store.open_watch_window(watch_window(guild_id=999, member_id=222))
    assert len(await store.list_open_watch_windows(111)) == 1
    assert len(await store.list_open_watch_windows(999)) == 1


@pytest.mark.asyncio
async def test_closing_a_watch_window_twice_is_idempotent(store: SQLiteStore) -> None:
    window = watch_window(guild_id=111, member_id=222)
    await store.open_watch_window(window)
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=NOW)
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=LATER)
    closed = await store.list_open_watch_windows(111)
    assert closed == ()


@pytest.mark.asyncio
async def test_closing_a_watch_window_preserves_the_first_close_reason_and_time(
    store: SQLiteStore,
) -> None:
    await store.open_watch_window(watch_window(guild_id=111, member_id=222))
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=NOW)
    result = await store.close_watch_window(
        111, 222, reason=WatchWindowCloseReason.STAFF_OVERRIDE, now=LATER
    )

    assert result is not None
    assert result.closed_at == NOW
    assert result.close_reason is WatchWindowCloseReason.EXPIRED


@pytest.mark.asyncio
async def test_closing_a_watch_window_that_never_existed_does_not_raise(
    store: SQLiteStore,
) -> None:
    result = await store.close_watch_window(
        111, 222, reason=WatchWindowCloseReason.EXPIRED, now=NOW
    )
    assert result is None


@pytest.mark.asyncio
async def test_reopening_a_watch_window_replaces_the_single_row_for_that_member(
    store: SQLiteStore,
) -> None:
    await store.open_watch_window(watch_window(guild_id=111, member_id=222, band=RiskBand.WATCH))
    await store.close_watch_window(111, 222, reason=WatchWindowCloseReason.EXPIRED, now=NOW)
    await store.open_watch_window(
        watch_window(
            guild_id=111,
            member_id=222,
            opened_at=LATER,
            expires_at=EVEN_LATER,
            band=RiskBand.SUSPICIOUS,
        )
    )

    open_windows = await store.list_open_watch_windows(111)
    assert len(open_windows) == 1
    assert open_windows[0].band is RiskBand.SUSPICIOUS
    assert open_windows[0].closed_at is None


# -- incidents --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incidents_are_guild_scoped(store: SQLiteStore) -> None:
    await store.record_incident(incident(guild_id=111, incident_id="incident-1"))
    await store.record_incident(incident(guild_id=999, incident_id="incident-1"))

    first = await store.get_incident(111, "incident-1")
    second = await store.get_incident(999, "incident-1")
    missing = await store.get_incident(555, "incident-1")

    assert first is not None and first.guild_id == 111
    assert second is not None and second.guild_id == 999
    assert missing is None


@pytest.mark.asyncio
async def test_list_recent_incidents_orders_newest_first_within_one_guild(
    store: SQLiteStore,
) -> None:
    await store.record_incident(incident(incident_id="incident-early", opened_at=NOW))
    await store.record_incident(incident(incident_id="incident-late", opened_at=LATER))
    await store.record_incident(incident(guild_id=999, incident_id="incident-other-guild"))

    recent = await store.list_recent_incidents(111)

    assert [row.incident_id for row in recent] == ["incident-late", "incident-early"]


@pytest.mark.asyncio
async def test_record_incident_can_set_acknowledged_by_on_an_existing_incident(
    store: SQLiteStore,
) -> None:
    await store.record_incident(incident(incident_id="incident-1", acknowledged_by=None))
    updated = await store.record_incident(
        incident(incident_id="incident-1", acknowledged_by=777)
    )

    assert updated.acknowledged_by == 777


# -- guild_allow_block_lists --------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_block_entries_are_guild_scoped(store: SQLiteStore) -> None:
    await store.save_allow_block_entry(allow_block_entry(guild_id=111, discord_user_id=333))
    await store.save_allow_block_entry(
        allow_block_entry(guild_id=999, discord_user_id=333, list_kind="block")
    )

    first = await store.list_allow_block_entries(111)
    second = await store.list_allow_block_entries(999)

    assert len(first) == 1 and first[0].list_kind == "allow"
    assert len(second) == 1 and second[0].list_kind == "block"


@pytest.mark.asyncio
async def test_saving_an_allow_block_entry_twice_replaces_rather_than_duplicates(
    store: SQLiteStore,
) -> None:
    await store.save_allow_block_entry(allow_block_entry(list_kind="allow", reason="first"))
    await store.save_allow_block_entry(allow_block_entry(list_kind="block", reason="second"))

    entries = await store.list_allow_block_entries(111)

    assert len(entries) == 1
    assert entries[0].list_kind == "block"
    assert entries[0].reason == "second"


@pytest.mark.asyncio
async def test_list_allow_block_entries_can_filter_by_list_kind(store: SQLiteStore) -> None:
    await store.save_allow_block_entry(allow_block_entry(discord_user_id=1, list_kind="allow"))
    await store.save_allow_block_entry(allow_block_entry(discord_user_id=2, list_kind="block"))

    allowed = await store.list_allow_block_entries(111, list_kind="allow")
    blocked = await store.list_allow_block_entries(111, list_kind="block")

    assert [row.discord_user_id for row in allowed] == [1]
    assert [row.discord_user_id for row in blocked] == [2]


# -- sniff_receipts -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sniff_receipts_are_guild_scoped(store: SQLiteStore) -> None:
    await store.record_sniff_receipt(
        guild_id=111,
        receipt_id="receipt-1",
        member_id=222,
        action="watch_window_opened",
        detail={"band": "watch"},
        created_at=NOW,
    )
    await store.record_sniff_receipt(
        guild_id=999,
        receipt_id="receipt-1",
        member_id=222,
        action="watch_window_opened",
        detail={"band": "watch"},
        created_at=NOW,
    )

    first = await store.list_sniff_receipts(111)
    second = await store.list_sniff_receipts(999)

    assert len(first) == 1 and first[0].guild_id == 111
    assert len(second) == 1 and second[0].guild_id == 999


@pytest.mark.asyncio
async def test_sniff_receipt_detail_is_redacted_before_storage(store: SQLiteStore) -> None:
    await store.record_sniff_receipt(
        guild_id=111,
        receipt_id="receipt-1",
        member_id=222,
        action="watch_window_opened",
        detail={"bot_token": "do-not-store"},
        created_at=NOW,
    )

    receipts = await store.list_sniff_receipts(111)

    assert receipts[0].detail == {"bot_token": "[REDACTED]"}


@pytest.mark.asyncio
async def test_list_sniff_receipts_can_filter_by_member(store: SQLiteStore) -> None:
    await store.record_sniff_receipt(
        guild_id=111,
        receipt_id="receipt-1",
        member_id=222,
        action="watch_window_opened",
        detail={},
        created_at=NOW,
    )
    await store.record_sniff_receipt(
        guild_id=111,
        receipt_id="receipt-2",
        member_id=333,
        action="watch_window_opened",
        detail={},
        created_at=NOW,
    )
    await store.record_sniff_receipt(
        guild_id=111,
        receipt_id="receipt-3",
        member_id=None,
        action="raid_incident_opened",
        detail={},
        created_at=NOW,
    )

    for_member = await store.list_sniff_receipts(111, member_id=222)
    all_receipts = await store.list_sniff_receipts(111)

    assert [row.receipt_id for row in for_member] == ["receipt-1"]
    assert len(all_receipts) == 3
