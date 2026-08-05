"""Integration tests for `krubit.services.entry_sniff.EntrySniffService`.

Exercises `assess_join` against a real on-disk `SQLiteStore` (never mocked), matching
the `test_watchdog_storage.py`/`test_creator_registry_storage.py` convention. A small
local `FakeMember`/`FakeGuild` pair stands in for `discord.Member`/`discord.Guild`,
matching the `test_live_signal_runtime.py` `as_member`/`cast` convention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from krubit.domain.watchdog import AllowBlockEntry, RiskBand
from krubit.services.entry_sniff import EntrySniffService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID, members: list[FakeMember] | None = None) -> None:
        self.id = guild_id
        self.members: list[FakeMember] = members or []


class FakeMember:
    def __init__(
        self,
        member_id: int = 222,
        *,
        guild: FakeGuild | None = None,
        created_hours_ago: float = 24 * 365,
        has_avatar: bool = True,
        bot: bool = False,
        system: bool = False,
        pending: bool = False,
        name: str = "krucialmember",
        joined_at: datetime = NOW,
    ) -> None:
        self.id = member_id
        self.guild = guild if guild is not None else FakeGuild()
        self.created_at = NOW - timedelta(hours=created_hours_ago)
        self.avatar = object() if has_avatar else None
        self.bot = bot
        self.system = system
        self.pending = pending
        self.name = name
        self.joined_at: datetime | None = joined_at


def member(
    member_id: int = 222,
    *,
    guild: FakeGuild | None = None,
    created_hours_ago: float = 24 * 365,
    has_avatar: bool = True,
    joined_at: datetime = NOW,
) -> discord.Member:
    return cast(
        discord.Member,
        FakeMember(
            member_id,
            guild=guild,
            created_hours_ago=created_hours_ago,
            has_avatar=has_avatar,
            joined_at=joined_at,
        ),
    )


@pytest.mark.asyncio
async def test_assess_join_persists_exactly_one_assessment_per_join(store: SQLiteStore) -> None:
    service = EntrySniffService(store)
    assessment = await service.assess_join(member(), now=NOW)

    stored = await store.get_entry_sniff_assessment(
        assessment.guild_id, assessment.member_id, joined_at=assessment.joined_at
    )
    assert stored == assessment


@pytest.mark.asyncio
async def test_assess_join_records_a_sniff_receipt(store: SQLiteStore) -> None:
    service = EntrySniffService(store)
    assessment = await service.assess_join(member(), now=NOW)

    receipts = await store.list_sniff_receipts(assessment.guild_id, member_id=assessment.member_id)
    assert len(receipts) == 1
    assert receipts[0].action == "entry_sniff_assessment_recorded"
    assert receipts[0].detail["band"] == assessment.band.value


@pytest.mark.asyncio
async def test_leave_then_rejoin_produces_two_independent_assessments(
    store: SQLiteStore,
) -> None:
    service = EntrySniffService(store)
    later = NOW + timedelta(days=1)

    first = await service.assess_join(member(joined_at=NOW), now=NOW)
    second = await service.assess_join(member(joined_at=later), now=later)

    assert first.joined_at != second.joined_at
    first_stored = await store.get_entry_sniff_assessment(
        GUILD_ID, first.member_id, joined_at=NOW
    )
    second_stored = await store.get_entry_sniff_assessment(
        GUILD_ID, first.member_id, joined_at=later
    )
    assert first_stored == first
    assert second_stored == second
    assert first_stored != second_stored


@pytest.mark.asyncio
async def test_block_listed_member_gets_a_strong_named_signal_and_incident_band(
    store: SQLiteStore,
) -> None:
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=222,
            list_kind="block",
            reason="known raid participant",
            set_by=999,
            set_at=NOW,
        )
    )
    service = EntrySniffService(store)

    assessment = await service.assess_join(member(), now=NOW)

    assert assessment.band is RiskBand.INCIDENT
    assert any(signal.name == "guild_block_list" for signal in assessment.signals)


@pytest.mark.asyncio
async def test_allow_listed_member_overrides_to_clear_band_but_keeps_evidence(
    store: SQLiteStore,
) -> None:
    await store.save_allow_block_entry(
        AllowBlockEntry(
            guild_id=GUILD_ID,
            discord_user_id=222,
            list_kind="allow",
            reason="verified staff alt",
            set_by=999,
            set_at=NOW,
        )
    )
    service = EntrySniffService(store)

    # A brand-new, default-avatar member would ordinarily accrue risk signals.
    assessment = await service.assess_join(
        member(created_hours_ago=1, has_avatar=False), now=NOW
    )

    assert assessment.band is RiskBand.CLEAR
    assert any(signal.name == "account_age" for signal in assessment.signals)
    assert "allow list" in assessment.explanation


@pytest.mark.asyncio
async def test_unremarkable_join_is_clear_with_no_signals(store: SQLiteStore) -> None:
    service = EntrySniffService(store)
    assessment = await service.assess_join(member(), now=NOW)

    assert assessment.band is RiskBand.CLEAR
    assert assessment.signals == ()
