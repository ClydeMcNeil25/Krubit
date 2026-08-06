"""Integration tests for `krubit.services.raid_detection.RaidDetector`/`SpamWaveDetector`.

Exercises `evaluate` against a real on-disk `SQLiteStore` (never mocked), matching the
`test_entry_sniff_service.py`/`test_watch_window_service.py` convention. `seed_assessment`
inserts a synthetic `EntrySniffAssessment` directly (bypassing `EntrySniffService`) so
each test can control `band`/`joined_at` precisely without needing a real `discord.Member`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from krubit.domain.watchdog import EntrySniffAssessment, IncidentKind, RiskBand, RiskSignal
from krubit.services.raid_detection import RaidDetector, SpamWaveDetector
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111

_member_id_counter = count(1000)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def seed_assessment(
    store: SQLiteStore,
    *,
    guild_id: int,
    band: RiskBand,
    joined_within_seconds: float,
) -> EntrySniffAssessment:
    """Insert one synthetic assessment for a fresh member, joined
    `joined_within_seconds` before `NOW`.
    """
    member_id = next(_member_id_counter)
    signals: tuple[RiskSignal, ...] = ()
    if band is not RiskBand.CLEAR:
        signals = (RiskSignal(name="account_age", weight=3, detail="new account", confidence=0.7),)
    assessment = EntrySniffAssessment(
        guild_id=guild_id,
        member_id=member_id,
        joined_at=NOW - timedelta(seconds=joined_within_seconds),
        band=band,
        signals=signals,
        explanation=f"synthetic fixture band={band.value}",
        created_at=NOW,
    )
    return await store.save_entry_sniff_assessment(assessment)


@pytest.mark.asyncio
async def test_raid_detector_fires_on_correlated_join_velocity_and_similarity(
    store: SQLiteStore,
) -> None:
    for _ in range(10):
        await seed_assessment(store, guild_id=111, band=RiskBand.WATCH, joined_within_seconds=30)
    incident = await RaidDetector(store).evaluate(111, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.RAID
    assert incident.band is RiskBand.INCIDENT


@pytest.mark.asyncio
async def test_raid_detector_does_not_fire_on_organic_growth(store: SQLiteStore) -> None:
    for i in range(10):
        await seed_assessment(
            store, guild_id=111, band=RiskBand.CLEAR, joined_within_seconds=i * 600
        )
    assert await RaidDetector(store).evaluate(111, now=NOW) is None


@pytest.mark.asyncio
async def test_raid_detector_does_not_fire_below_elevated_join_threshold(
    store: SQLiteStore,
) -> None:
    # Only 5 elevated-band joins clustered tightly -- below the 8-join floor.
    for _ in range(5):
        await seed_assessment(store, guild_id=111, band=RiskBand.WATCH, joined_within_seconds=10)
    assert await RaidDetector(store).evaluate(111, now=NOW) is None


@pytest.mark.asyncio
async def test_raid_detector_records_incident_and_receipt(store: SQLiteStore) -> None:
    for _ in range(10):
        await seed_assessment(
            store, guild_id=111, band=RiskBand.SUSPICIOUS, joined_within_seconds=5
        )
    incident = await RaidDetector(store).evaluate(111, now=NOW)
    assert incident is not None

    stored = await store.get_incident(111, incident.incident_id)
    assert stored == incident

    receipts = await store.list_sniff_receipts(111)
    assert any(receipt.action == "incident_recorded" for receipt in receipts)


@pytest.mark.asyncio
async def test_raid_detector_does_not_renotify_for_the_same_ongoing_raid_across_sweeps(
    store: SQLiteStore,
) -> None:
    """Important #4: a still-ongoing single raid must not mint a new incident (and
    staff notification) on every 60-second sweep cycle for the full 10-minute window
    duration. Simulates consecutive sweeps against the same still-elevated join
    cluster: only the first sweep should fire.
    """
    for _ in range(10):
        await seed_assessment(store, guild_id=111, band=RiskBand.WATCH, joined_within_seconds=30)
    detector = RaidDetector(store)

    first = await detector.evaluate(111, now=NOW)
    assert first is not None

    # Three more sweeps, one minute apart, well inside the 10-minute raid window --
    # the underlying condition (same elevated joins still in window) is unchanged.
    for minutes in (1, 2, 3):
        again = await detector.evaluate(111, now=NOW + timedelta(minutes=minutes))
        assert again is None

    incidents = await store.list_recent_incidents(111)
    assert len([i for i in incidents if i.kind is IncidentKind.RAID]) == 1


@pytest.mark.asyncio
async def test_raid_detector_is_scoped_to_its_own_guild(store: SQLiteStore) -> None:
    for _ in range(10):
        await seed_assessment(store, guild_id=222, band=RiskBand.WATCH, joined_within_seconds=30)
    assert await RaidDetector(store).evaluate(111, now=NOW) is None


@pytest.mark.asyncio
async def test_raid_detector_uses_injected_evidence_builder(store: SQLiteStore) -> None:
    calls: list[int] = []

    def builder(guild_id: int, signals: tuple[RiskSignal, ...], now: datetime) -> str:
        calls.append(guild_id)
        return "custom-evidence-id"

    for _ in range(10):
        await seed_assessment(store, guild_id=111, band=RiskBand.WATCH, joined_within_seconds=30)
    incident = await RaidDetector(store, evidence_builder=builder).evaluate(111, now=NOW)

    assert incident is not None
    assert incident.evidence_packet_id == "custom-evidence-id"
    assert calls == [111]


@pytest.mark.asyncio
async def test_spam_wave_detector_fires_when_three_distinct_members_post_near_duplicates(
    store: SQLiteStore,
) -> None:
    detector = SpamWaveDetector(store)
    payload = "Free nitro! Click this link now: totally-legit-nitro.example"
    detector.record_message(GUILD_ID, 1, payload, NOW)
    detector.record_message(GUILD_ID, 2, payload, NOW + timedelta(seconds=5))
    detector.record_message(GUILD_ID, 3, payload + "!!", NOW + timedelta(seconds=10))

    incident = await detector.evaluate(GUILD_ID, now=NOW + timedelta(seconds=15))
    assert incident is not None
    assert incident.kind is IncidentKind.SPAM_WAVE
    assert incident.band is RiskBand.INCIDENT


@pytest.mark.asyncio
async def test_spam_wave_detector_does_not_fire_below_member_threshold(
    store: SQLiteStore,
) -> None:
    detector = SpamWaveDetector(store)
    payload = "hey everyone check this out"
    detector.record_message(GUILD_ID, 1, payload, NOW)
    detector.record_message(GUILD_ID, 2, payload, NOW + timedelta(seconds=5))

    assert await detector.evaluate(GUILD_ID, now=NOW + timedelta(seconds=10)) is None


@pytest.mark.asyncio
async def test_spam_wave_detector_does_not_fire_on_distinct_content(store: SQLiteStore) -> None:
    detector = SpamWaveDetector(store)
    detector.record_message(GUILD_ID, 1, "good morning everyone", NOW)
    detector.record_message(
        GUILD_ID, 2, "anyone up for a game tonight?", NOW + timedelta(seconds=5)
    )
    detector.record_message(
        GUILD_ID, 3, "does anyone know the event time?", NOW + timedelta(seconds=10)
    )

    assert await detector.evaluate(GUILD_ID, now=NOW + timedelta(seconds=15)) is None


@pytest.mark.asyncio
async def test_spam_wave_detector_ignores_messages_outside_window(store: SQLiteStore) -> None:
    detector = SpamWaveDetector(store)
    payload = "Free nitro! Click this link now: totally-legit-nitro.example"
    detector.record_message(GUILD_ID, 1, payload, NOW)
    detector.record_message(GUILD_ID, 2, payload, NOW + timedelta(minutes=10))
    detector.record_message(GUILD_ID, 3, payload, NOW + timedelta(minutes=20))

    # By the time all three are in the cache, only the most recent one is still
    # inside the 5-minute trailing window -- no cluster of 3 within the window.
    assert await detector.evaluate(GUILD_ID, now=NOW + timedelta(minutes=20)) is None


@pytest.mark.asyncio
async def test_spam_wave_detector_does_not_renotify_for_the_same_ongoing_wave_across_sweeps(
    store: SQLiteStore,
) -> None:
    """Important #4, spam-wave case: repeated evaluation against the same still-
    in-window cluster must not mint a new incident on every sweep."""
    detector = SpamWaveDetector(store)
    payload = "Free nitro! Click this link now: totally-legit-nitro.example"
    detector.record_message(GUILD_ID, 1, payload, NOW)
    detector.record_message(GUILD_ID, 2, payload, NOW + timedelta(seconds=5))
    detector.record_message(GUILD_ID, 3, payload + "!!", NOW + timedelta(seconds=10))

    first = await detector.evaluate(GUILD_ID, now=NOW + timedelta(seconds=15))
    assert first is not None

    for offset_seconds in (60, 120, 180):
        again = await detector.evaluate(GUILD_ID, now=NOW + timedelta(seconds=15 + offset_seconds))
        assert again is None

    incidents = await store.list_recent_incidents(GUILD_ID)
    assert len([i for i in incidents if i.kind is IncidentKind.SPAM_WAVE]) == 1


@pytest.mark.asyncio
async def test_spam_wave_detector_does_not_fire_with_empty_cache(store: SQLiteStore) -> None:
    detector = SpamWaveDetector(store)
    assert await detector.evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_spam_wave_detector_records_incident_and_receipt(store: SQLiteStore) -> None:
    detector = SpamWaveDetector(store)
    payload = "Free nitro! Click this link now: totally-legit-nitro.example"
    detector.record_message(GUILD_ID, 1, payload, NOW)
    detector.record_message(GUILD_ID, 2, payload, NOW)
    detector.record_message(GUILD_ID, 3, payload, NOW)

    incident = await detector.evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    stored = await store.get_incident(GUILD_ID, incident.incident_id)
    assert stored == incident

    receipts = await store.list_sniff_receipts(GUILD_ID)
    assert any(receipt.action == "incident_recorded" for receipt in receipts)
