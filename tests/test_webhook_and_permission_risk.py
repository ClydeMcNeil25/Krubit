"""Integration tests for `krubit.services.webhook_and_permission_risk`.

Exercises `WebhookAbuseDetector.evaluate`/`PermissionRiskDetector.evaluate` against a
real on-disk `SQLiteStore` (never mocked), matching `test_raid_detection.py`'s
convention. Guild events are seeded with `krubit.discord.events.guild_event`, the same
constructor `KrubitBot._ingest_change` uses in production, then persisted through
`SQLiteStore.accept_event` -- so these tests exercise the real Phase 1 event shape
(`entity_id`/`before`/`after`), not a hand-rolled approximation of it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
import pytest

from krubit.discord.events import guild_event
from krubit.domain.watchdog import (
    EntrySniffAssessment,
    IncidentKind,
    RiskBand,
    RiskSignal,
    WatchWindow,
)
from krubit.services.webhook_and_permission_risk import (
    PermissionRiskDetector,
    WebhookAbuseDetector,
)
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111
ADMINISTRATOR_PERMISSIONS = discord.Permissions(administrator=True).value
NO_PERMISSIONS = discord.Permissions.none().value


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def seed_webhook_event(store: SQLiteStore, *, entity_id: int, occurred_at: datetime) -> None:
    event = guild_event(
        "webhooks_updated",
        GUILD_ID,
        entity_id,
        occurred_at,
        None,
        {"channel": f"channel-{entity_id}"},
    )
    await store.accept_event(event)


async def seed_role_event(
    store: SQLiteStore,
    *,
    role_id: int,
    permissions: int,
    occurred_at: datetime,
    event_type: str = "role_created",
) -> None:
    after = {
        "name": f"role-{role_id}",
        "position": 1,
        "permissions": str(permissions),
        "managed": False,
    }
    event = guild_event(event_type, GUILD_ID, role_id, occurred_at, None, after)
    await store.accept_event(event)


async def seed_role_grant_event(
    store: SQLiteStore,
    *,
    member_id: int,
    before_role_ids: tuple[int, ...],
    after_role_ids: tuple[int, ...],
    occurred_at: datetime,
) -> None:
    before = {"role_ids": ",".join(str(r) for r in before_role_ids)}
    after = {"role_ids": ",".join(str(r) for r in after_role_ids)}
    event = guild_event("member_roles_updated", GUILD_ID, member_id, occurred_at, before, after)
    await store.accept_event(event)


@pytest.mark.asyncio
async def test_webhook_abuse_detector_fires_on_config_change_burst(store: SQLiteStore) -> None:
    for index, offset in enumerate((0, 60, 120)):
        await seed_webhook_event(
            store, entity_id=555 + index, occurred_at=NOW - timedelta(seconds=offset)
        )

    incident = await WebhookAbuseDetector(store).evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.WEBHOOK_ABUSE
    assert incident.band is RiskBand.INCIDENT


@pytest.mark.asyncio
async def test_webhook_abuse_detector_does_not_fire_below_channel_threshold(
    store: SQLiteStore,
) -> None:
    for index, offset in enumerate((0, 60)):
        await seed_webhook_event(
            store, entity_id=555 + index, occurred_at=NOW - timedelta(seconds=offset)
        )
    assert await WebhookAbuseDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_webhook_abuse_detector_does_not_fire_on_spread_out_config_changes(
    store: SQLiteStore,
) -> None:
    # Three distinct channels, but each touched a day apart -- ordinary long-run
    # maintenance, not a burst.
    for index, offset_days in enumerate((0, 1, 2)):
        await seed_webhook_event(
            store, entity_id=555 + index, occurred_at=NOW - timedelta(days=offset_days)
        )
    assert await WebhookAbuseDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_webhook_abuse_detector_storage_path_alone_cannot_see_same_channel_repeats(
    store: SQLiteStore,
) -> None:
    # `on_webhooks_update`'s payload only ever carries the channel name, so repeated
    # updates to the *same* channel hash identically and collapse to one stored row
    # (see `guild_event`'s docstring and this module's "The distinct-channel signal
    # alone misses same-channel repeated abuse" section) -- this is a real, known
    # limitation of the underlying Phase 1 event, not a detector bug: three genuine
    # same-channel updates in a burst still only ever produce one distinguishable
    # stored row. Without a corresponding `record_webhook_event` call, the detector
    # correctly cannot see this pattern from storage alone -- see the next test for
    # the fix (the in-memory `record_webhook_event` path).
    for offset in (0, 60, 120):
        await seed_webhook_event(store, entity_id=555, occurred_at=NOW - timedelta(seconds=offset))
    assert await WebhookAbuseDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_webhook_abuse_detector_fires_on_repeated_same_channel_updates_via_ingestion(
    store: SQLiteStore,
) -> None:
    # This is the fix for the gap proven by the previous test: `record_webhook_event`
    # observes each raw same-channel update directly, sidestepping `guild_events`'
    # storage-layer dedup entirely (mirrors `SpamWaveDetector.record_message`'s
    # in-memory correlation cache). Note the underlying `guild_events` table only
    # ever stores one distinguishable row for this channel (as the previous test
    # shows) -- the fix does not depend on storage capturing all three.
    detector = WebhookAbuseDetector(store)
    for offset in (0, 60, 120):
        detector.record_webhook_event(GUILD_ID, 555, NOW - timedelta(seconds=offset))

    incident = await detector.evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.WEBHOOK_ABUSE
    assert incident.band is RiskBand.INCIDENT

    stored = await store.get_incident(GUILD_ID, incident.incident_id)
    assert stored == incident
    receipts = await store.list_sniff_receipts(GUILD_ID)
    assert any(receipt.action == "incident_recorded" for receipt in receipts)


@pytest.mark.asyncio
async def test_webhook_abuse_detector_does_not_fire_below_same_channel_threshold(
    store: SQLiteStore,
) -> None:
    detector = WebhookAbuseDetector(store)
    for offset in (0, 60):
        detector.record_webhook_event(GUILD_ID, 555, NOW - timedelta(seconds=offset))
    assert await detector.evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_webhook_abuse_detector_ignores_same_channel_observations_outside_window(
    store: SQLiteStore,
) -> None:
    detector = WebhookAbuseDetector(store)
    detector.record_webhook_event(GUILD_ID, 555, NOW - timedelta(minutes=30))
    detector.record_webhook_event(GUILD_ID, 555, NOW - timedelta(minutes=20))
    detector.record_webhook_event(GUILD_ID, 555, NOW)
    # Only the most recent observation is within the 10-minute window.
    assert await detector.evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_webhook_abuse_detector_records_incident_and_receipt(store: SQLiteStore) -> None:
    for index, offset in enumerate((0, 60, 120)):
        await seed_webhook_event(
            store, entity_id=555 + index, occurred_at=NOW - timedelta(seconds=offset)
        )
    incident = await WebhookAbuseDetector(store).evaluate(GUILD_ID, now=NOW)
    assert incident is not None

    stored = await store.get_incident(GUILD_ID, incident.incident_id)
    assert stored == incident
    receipts = await store.list_sniff_receipts(GUILD_ID)
    assert any(receipt.action == "incident_recorded" for receipt in receipts)


@pytest.mark.asyncio
async def test_webhook_abuse_detector_uses_injected_evidence_builder(store: SQLiteStore) -> None:
    for index, offset in enumerate((0, 60, 120)):
        await seed_webhook_event(
            store, entity_id=555 + index, occurred_at=NOW - timedelta(seconds=offset)
        )

    def builder(guild_id: int, signals: tuple[RiskSignal, ...], now: datetime) -> str:
        return "custom-evidence-id"

    incident = await WebhookAbuseDetector(store, evidence_builder=builder).evaluate(
        GUILD_ID, now=NOW
    )
    assert incident is not None
    assert incident.evidence_packet_id == "custom-evidence-id"


@pytest.mark.asyncio
async def test_permission_risk_detector_fires_when_watched_member_gains_elevated_role(
    store: SQLiteStore,
) -> None:
    member_id = 777
    await seed_role_event(
        store,
        role_id=999,
        permissions=ADMINISTRATOR_PERMISSIONS,
        occurred_at=NOW - timedelta(minutes=20),
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1, 2),
        after_role_ids=(1, 2, 999),
        occurred_at=NOW - timedelta(minutes=5),
    )
    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=member_id,
            opened_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=23),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )

    incident = await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.PERMISSION_RISK
    assert incident.band is RiskBand.INCIDENT


@pytest.mark.asyncio
async def test_permission_risk_detector_fires_for_newly_joined_member_without_watch_window(
    store: SQLiteStore,
) -> None:
    member_id = 778
    await seed_role_event(
        store,
        role_id=998,
        permissions=ADMINISTRATOR_PERMISSIONS,
        occurred_at=NOW - timedelta(minutes=20),
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1,),
        after_role_ids=(1, 998),
        occurred_at=NOW - timedelta(minutes=5),
    )
    await store.save_entry_sniff_assessment(
        EntrySniffAssessment(
            guild_id=GUILD_ID,
            member_id=member_id,
            joined_at=NOW - timedelta(hours=2),
            band=RiskBand.CLEAR,
            signals=(),
            explanation="clean join",
            created_at=NOW - timedelta(hours=2),
        )
    )

    incident = await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    assert incident.kind is IncidentKind.PERMISSION_RISK


@pytest.mark.asyncio
async def test_permission_risk_detector_does_not_fire_for_non_elevated_role(
    store: SQLiteStore,
) -> None:
    member_id = 779
    await seed_role_event(
        store, role_id=100, permissions=NO_PERMISSIONS, occurred_at=NOW - timedelta(minutes=20)
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1,),
        after_role_ids=(1, 100),
        occurred_at=NOW - timedelta(minutes=5),
    )
    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=member_id,
            opened_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=23),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )

    assert await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_permission_risk_detector_does_not_fire_for_unwatched_established_member(
    store: SQLiteStore,
) -> None:
    member_id = 780
    await seed_role_event(
        store,
        role_id=997,
        permissions=ADMINISTRATOR_PERMISSIONS,
        occurred_at=NOW - timedelta(minutes=20),
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1,),
        after_role_ids=(1, 997),
        occurred_at=NOW - timedelta(minutes=5),
    )
    # No watch window, and this member's own join was long ago -- an ordinary staff
    # promotion of a trusted, established member should not be flagged.
    await store.save_entry_sniff_assessment(
        EntrySniffAssessment(
            guild_id=GUILD_ID,
            member_id=member_id,
            joined_at=NOW - timedelta(days=200),
            band=RiskBand.CLEAR,
            signals=(),
            explanation="clean join long ago",
            created_at=NOW - timedelta(days=200),
        )
    )

    assert await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_permission_risk_detector_does_not_fire_outside_grant_lookback(
    store: SQLiteStore,
) -> None:
    member_id = 781
    await seed_role_event(
        store,
        role_id=996,
        permissions=ADMINISTRATOR_PERMISSIONS,
        occurred_at=NOW - timedelta(hours=2),
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1,),
        after_role_ids=(1, 996),
        occurred_at=NOW - timedelta(hours=1),
    )
    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=member_id,
            opened_at=NOW - timedelta(hours=1, minutes=30),
            expires_at=NOW + timedelta(hours=22),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )

    assert await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW) is None


@pytest.mark.asyncio
async def test_permission_risk_detector_records_incident_and_receipt(store: SQLiteStore) -> None:
    member_id = 782
    await seed_role_event(
        store,
        role_id=995,
        permissions=ADMINISTRATOR_PERMISSIONS,
        occurred_at=NOW - timedelta(minutes=20),
    )
    await seed_role_grant_event(
        store,
        member_id=member_id,
        before_role_ids=(1,),
        after_role_ids=(1, 995),
        occurred_at=NOW - timedelta(minutes=5),
    )
    await store.open_watch_window(
        WatchWindow(
            guild_id=GUILD_ID,
            member_id=member_id,
            opened_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=23),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )
    )

    incident = await PermissionRiskDetector(store).evaluate(GUILD_ID, now=NOW)
    assert incident is not None
    stored = await store.get_incident(GUILD_ID, incident.incident_id)
    assert stored == incident
    receipts = await store.list_sniff_receipts(GUILD_ID)
    assert any(receipt.action == "incident_recorded" for receipt in receipts)
