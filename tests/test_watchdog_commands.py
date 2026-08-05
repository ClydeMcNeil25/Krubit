"""Tests for `krubit.discord.watchdog_commands.WatchdogCommandService` — Task 8's
staff-only `/fetch sniff`-family command surface.

Matches `tests/test_content_commands.py`'s convention: every test calls the
framework-independent service directly (never a `discord.Interaction`), against a
real on-disk `SQLiteStore` (never mocked), so authority and redaction properties are
exercised end to end through real storage rather than a fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from krubit.discord.content_commands import CommandStatus
from krubit.discord.watchdog_commands import WatchdogActorContext, WatchdogCommandService
from krubit.domain.watchdog import (
    AllowBlockEntry,
    EntrySniffAssessment,
    Incident,
    IncidentKind,
    RiskBand,
    RiskSignal,
    WatchWindow,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

GUILD_ID = 111
STAFF_ID = 999
REGULAR_ID = 222
TARGET_ID = 333


def staff_member() -> WatchdogActorContext:
    return WatchdogActorContext(guild_id=GUILD_ID, member_id=STAFF_ID, is_staff=True)


def regular_member() -> WatchdogActorContext:
    return WatchdogActorContext(guild_id=GUILD_ID, member_id=REGULAR_ID, is_staff=False)


def other_member() -> WatchdogActorContext:
    return WatchdogActorContext(guild_id=GUILD_ID, member_id=TARGET_ID, is_staff=False)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def commands(store: SQLiteStore) -> WatchdogCommandService:
    return WatchdogCommandService(store, now=lambda: NOW)


async def _seed_assessment(
    store: SQLiteStore, *, member_id: int, band: RiskBand, joined_at: datetime
) -> EntrySniffAssessment:
    signals = (
        ()
        if band is RiskBand.CLEAR
        else (RiskSignal(name="new_account", weight=8, detail="account is new", confidence=0.9),)
    )
    assessment = EntrySniffAssessment(
        guild_id=GUILD_ID,
        member_id=member_id,
        joined_at=joined_at,
        band=band,
        signals=signals,
        explanation=f"{band.value} band: seeded for test",
        created_at=joined_at,
    )
    return await store.save_entry_sniff_assessment(assessment)


async def _seed_open_window(store: SQLiteStore, *, member_id: int) -> WatchWindow:
    window = WatchWindow(
        guild_id=GUILD_ID,
        member_id=member_id,
        opened_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(hours=1),
        band=RiskBand.WATCH,
        closed_at=None,
        close_reason=None,
    )
    return await store.open_watch_window(window)


async def _seed_incident(
    store: SQLiteStore,
    *,
    incident_id: str = "raid:test-1",
    signal_names: tuple[str, ...] = ("raid_join_cluster",),
) -> Incident:
    incident = Incident(
        guild_id=GUILD_ID,
        incident_id=incident_id,
        kind=IncidentKind.RAID,
        band=RiskBand.INCIDENT,
        opened_at=NOW,
        evidence_packet_id=f"evidence:{incident_id}",
        recommended_action="Review the flagged joins; no automatic action was taken.",
        acknowledged_by=None,
    )
    saved = await store.record_incident(incident)
    await store.record_sniff_receipt(
        guild_id=GUILD_ID,
        receipt_id=f"incident:{incident_id}",
        member_id=None,
        action="incident_recorded",
        detail={"kind": incident.kind.value, "signal_names": list(signal_names)},
        created_at=NOW,
    )
    return saved


async def seeded_incident_with_secret(store: SQLiteStore) -> str:
    """Seed an incident whose underlying receipt carries a credential-shaped value
    that `redact()` is expected to strip — see `WatchdogCommandService`'s module
    docstring for why `record_sniff_receipt` already redacts this at write time, and
    `evidence()`'s reconstruction never reintroduces raw content."""
    incident_id = "raid:credential-1"
    await _seed_incident(
        store,
        incident_id=incident_id,
        signal_names=("password=secretvalue123",),
    )
    return incident_id


# -- authority: denied before any query, every command ------------------------------


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_sniff_command(commands: WatchdogCommandService) -> None:
    result = await commands.sniff(actor=regular_member(), target=other_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_sniff_report(commands: WatchdogCommandService) -> None:
    result = await commands.sniff_report(actor=regular_member())
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_incident(commands: WatchdogCommandService) -> None:
    result = await commands.incident(actor=regular_member(), incident_id="raid:nonexistent")
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_evidence(commands: WatchdogCommandService) -> None:
    result = await commands.evidence(actor=regular_member(), incident_id="raid:nonexistent")
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_non_staff_member_is_denied_watchlist(commands: WatchdogCommandService) -> None:
    result = await commands.watchlist(actor=regular_member())
    assert result.status is CommandStatus.DENIED


# -- sniff --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_member_reads_current_assessment(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    await _seed_assessment(
        store, member_id=TARGET_ID, band=RiskBand.SUSPICIOUS, joined_at=NOW - timedelta(hours=1)
    )
    result = await commands.sniff(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "suspicious" in result.card.description.lower()


@pytest.mark.asyncio
async def test_sniff_fails_for_member_with_no_assessment(
    commands: WatchdogCommandService,
) -> None:
    result = await commands.sniff(actor=staff_member(), target=other_member())
    assert result.status is CommandStatus.FAILED


# -- sniff-report ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sniff_report_surfaces_recent_incident_band_join_and_open_window(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    await _seed_assessment(
        store, member_id=TARGET_ID, band=RiskBand.INCIDENT, joined_at=NOW - timedelta(hours=2)
    )
    # A stale, outside-window assessment must not be counted.
    await _seed_assessment(
        store, member_id=444, band=RiskBand.INCIDENT, joined_at=NOW - timedelta(hours=48)
    )
    # A WATCH-band join is not "high band" and must not be counted either.
    await _seed_assessment(
        store, member_id=555, band=RiskBand.WATCH, joined_at=NOW - timedelta(hours=1)
    )
    await _seed_open_window(store, member_id=666)

    result = await commands.sniff_report(actor=staff_member())

    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["high_band_count"] == 1
    assert result.detail["open_watch_window_count"] == 1
    assert result.card is not None
    assert f"<@{TARGET_ID}>" in result.card.description
    assert "<@444>" not in result.card.description


# -- incident -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_member_reads_incident(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    incident = await _seed_incident(store)
    result = await commands.incident(actor=staff_member(), incident_id=incident.incident_id)
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "raid" in result.card.description.lower()
    assert "raid_join_cluster" in result.card.fields[2].value


@pytest.mark.asyncio
async def test_incident_fails_for_unknown_incident_id(
    commands: WatchdogCommandService,
) -> None:
    result = await commands.incident(actor=staff_member(), incident_id="raid:nonexistent")
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_incident_card_never_presents_fabricated_confidence_or_message_links(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    """`_reconstruct_signals` fabricates a placeholder weight/confidence and
    `build_evidence_packet` is always called with zero messages (see
    `WatchdogCommandService`'s module docstring) -- none of that may ever be
    rendered as if it were the detector's genuine output. The rendered card must
    instead disclose that only signal names were recoverable."""
    incident = await _seed_incident(store)
    result = await commands.incident(actor=staff_member(), incident_id=incident.incident_id)
    assert result.card is not None
    # Only the disclosure notice may mention these words (to explain what is
    # missing) -- no field name/value pair may present a fabricated number under
    # them, which is what would happen if a "Confidence"/"Weight"/"Message links"
    # field were rendered again.
    field_names = {field.name.lower() for field in result.card.fields}
    assert "confidence" not in field_names
    assert "weight" not in field_names
    assert "message links" not in field_names
    rendered = result.card.description + " ".join(
        f"{field.name}:{field.value}" for field in result.card.fields
    )
    assert "1.0" not in rendered
    assert "not persisted" in rendered.lower()


# -- evidence ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_command_never_renders_unredacted_content(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    incident_id = await seeded_incident_with_secret(store)
    result = await commands.evidence(actor=staff_member(), incident_id=incident_id)
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "secret" not in result.card.description
    assert "secretvalue123" not in result.card.description


@pytest.mark.asyncio
async def test_evidence_card_discloses_reconstruction_and_omits_fabricated_numbers(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    """A raid incident that (per real detector behavior) involved real messages and
    a detector-computed confidence must never render as `confidence: 1.0` /
    `message_links: []` looking like genuine detector output -- see the Task 8
    review finding this test guards against."""
    incident = await _seed_incident(store)
    result = await commands.evidence(actor=staff_member(), incident_id=incident.incident_id)
    assert result.card is not None
    description = result.card.description
    assert "not persisted" in description.lower()
    # The disclosure notice legitimately names "confidence"/"weight" to explain
    # what is missing; what must never appear is a fabricated *value* for them, or
    # an evidence-implying-empty "message_links: []"/"event_ids: []" pair.
    assert "confidence:" not in description.lower()
    assert "weight:" not in description.lower()
    assert "1.0" not in description
    assert "message_links: []" not in description
    assert "event_ids: []" not in description


@pytest.mark.asyncio
async def test_evidence_fails_for_unknown_incident_id(
    commands: WatchdogCommandService,
) -> None:
    result = await commands.evidence(actor=staff_member(), incident_id="raid:nonexistent")
    assert result.status is CommandStatus.FAILED


# -- watchlist --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_member_reads_watchlist(
    store: SQLiteStore, commands: WatchdogCommandService
) -> None:
    await _seed_open_window(store, member_id=TARGET_ID)
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=777,
            list_kind="allow",
            reason="vouched by staff",
            set_by=STAFF_ID,
            set_at=NOW,
        )
    )
    result = await commands.watchlist(actor=staff_member())
    assert result.status is CommandStatus.SUCCEEDED
    assert result.detail["open_watch_window_count"] == 1
    assert result.detail["allow_count"] == 1
    assert result.detail["block_count"] == 0
