"""Integration tests for `krubit.services.watch_window.WatchWindowService`.

Exercises `open_if_warranted`, `sweep_expired`, and `inspect_message` against a real
on-disk `SQLiteStore` (never mocked), matching the `test_entry_sniff_service.py`
convention. Small local `FakeMessage`/`FakeAuthor`/`FakeGuild` stand in for
`discord.Message`/`discord.Member`/`discord.Guild`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from krubit.domain.watchdog import (
    EntrySniffAssessment,
    RiskBand,
    RiskSignal,
    WatchWindow,
    WatchWindowCloseReason,
)
from krubit.services.entry_sniff import EntrySniffService
from krubit.services.watch_window import WATCH_WINDOW_DURATION, WatchWindowService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)
GUILD_ID = 111
MEMBER_ID = 222


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def assessment(
    *,
    guild_id: int = GUILD_ID,
    member_id: int = MEMBER_ID,
    band: RiskBand = RiskBand.WATCH,
    joined_at: datetime = NOW,
) -> EntrySniffAssessment:
    signals: tuple[RiskSignal, ...] = ()
    if band is not RiskBand.CLEAR:
        signals = (RiskSignal(name="account_age", weight=3, detail="new account", confidence=0.7),)
    return EntrySniffAssessment(
        guild_id=guild_id,
        member_id=member_id,
        joined_at=joined_at,
        band=band,
        signals=signals,
        explanation=f"{band.value} band",
        created_at=joined_at,
    )


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID) -> None:
        self.id = guild_id


class FakeAuthor:
    def __init__(self, author_id: int = MEMBER_ID) -> None:
        self.id = author_id


class FakeMessage:
    def __init__(
        self,
        *,
        content: str = "hello there",
        author_id: int = MEMBER_ID,
        guild: FakeGuild | None = None,
        mention_count: int = 0,
        mention_everyone: bool = False,
    ) -> None:
        self.content = content
        self.author = FakeAuthor(author_id)
        self.guild: FakeGuild | None = guild if guild is not None else FakeGuild()
        self.mentions = [object() for _ in range(mention_count)]
        self.role_mentions: list[object] = []
        self.mention_everyone = mention_everyone


def message(
    *,
    content: str = "hello there",
    author_id: int = MEMBER_ID,
    guild: FakeGuild | None = None,
    mention_count: int = 0,
    mention_everyone: bool = False,
) -> FakeMessage:
    return FakeMessage(
        content=content,
        author_id=author_id,
        guild=guild,
        mention_count=mention_count,
        mention_everyone=mention_everyone,
    )


@pytest.mark.asyncio
async def test_clear_band_never_opens_a_watch_window(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    result = await service.open_if_warranted(assessment(band=RiskBand.CLEAR), now=NOW)
    assert result is None
    assert await store.list_open_watch_windows(GUILD_ID) == ()


@pytest.mark.asyncio
async def test_watch_band_opens_a_watch_window_and_records_a_receipt(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    window = await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)

    assert window is not None
    assert window.band is RiskBand.WATCH
    assert window.closed_at is None
    open_windows = await store.list_open_watch_windows(GUILD_ID)
    assert len(open_windows) == 1

    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=MEMBER_ID)
    assert any(r.action == "watch_window_opened" for r in receipts)


@pytest.mark.asyncio
async def test_expired_watch_window_is_swept_and_closed(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)

    closed = await service.sweep_expired(GUILD_ID, now=NOW + WATCH_WINDOW_DURATION + ONE_SECOND)

    assert len(closed) == 1
    assert closed[0].close_reason is WatchWindowCloseReason.EXPIRED
    assert await store.list_open_watch_windows(GUILD_ID) == ()

    receipts = await store.list_sniff_receipts(GUILD_ID, member_id=MEMBER_ID)
    assert any(r.action == "watch_window_closed" for r in receipts)


@pytest.mark.asyncio
async def test_sweep_expired_is_idempotent(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)
    expired_at = NOW + WATCH_WINDOW_DURATION + ONE_SECOND

    first = await service.sweep_expired(GUILD_ID, now=expired_at)
    second = await service.sweep_expired(GUILD_ID, now=expired_at)

    assert len(first) == 1
    assert second == ()


@pytest.mark.asyncio
async def test_sweep_expired_leaves_unexpired_windows_open(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)

    closed = await service.sweep_expired(GUILD_ID, now=NOW + timedelta(minutes=1))

    assert closed == ()
    assert len(await store.list_open_watch_windows(GUILD_ID)) == 1


@pytest.mark.asyncio
async def test_inspect_message_returns_none_without_an_open_window(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    result = await service.inspect_message(message(content="buy now buy now buy now"), now=NOW)
    assert result is None


@pytest.mark.asyncio
async def test_inspect_message_returns_none_after_window_expires(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)
    await service.sweep_expired(GUILD_ID, now=NOW + WATCH_WINDOW_DURATION + ONE_SECOND)

    result = await service.inspect_message(
        message(content="buy now buy now buy now"),
        now=NOW + WATCH_WINDOW_DURATION + ONE_SECOND,
    )
    assert result is None


@pytest.mark.asyncio
async def test_inspect_message_flags_a_watched_members_risky_message(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH), now=NOW)

    result = await service.inspect_message(
        message(mention_count=25), now=NOW + timedelta(seconds=5)
    )

    assert result is not None
    assert result.name == "mass_mentions"


@pytest.mark.asyncio
async def test_inspect_message_ignores_unwatched_members_in_the_same_guild(
    store: SQLiteStore,
) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH, member_id=MEMBER_ID), now=NOW)

    other_member_id = 333
    result = await service.inspect_message(
        message(author_id=other_member_id, mention_count=25), now=NOW
    )
    assert result is None


@pytest.mark.asyncio
async def test_repeated_content_compares_only_within_the_same_member(store: SQLiteStore) -> None:
    service = WatchWindowService(store)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH, member_id=MEMBER_ID), now=NOW)
    await service.open_if_warranted(assessment(band=RiskBand.WATCH, member_id=333), now=NOW)

    unique_content = "totally unique message about the weather today, nothing suspicious here"

    # Member A posts a message.
    first = await service.inspect_message(
        message(author_id=MEMBER_ID, content=unique_content), now=NOW
    )
    assert first is None

    # Member B posts the near-identical text — must NOT trigger a cross-member match.
    cross_member = await service.inspect_message(
        message(author_id=333, content=unique_content), now=NOW + timedelta(seconds=1)
    )
    assert cross_member is None

    # Member A repeats their own near-identical message — this SHOULD trigger.
    own_repeat = await service.inspect_message(
        message(author_id=MEMBER_ID, content=unique_content), now=NOW + timedelta(seconds=2)
    )
    assert own_repeat is not None
    assert own_repeat.name == "repeated_content_near_duplicate"


@pytest.mark.asyncio
async def test_benign_join_surge_does_not_push_clean_members_past_watch(
    store: SQLiteStore,
) -> None:
    """A legitimate community growth spike must not blanket-flag clean members.

    Nine ordinary, long-established members (varied account ages so no two land
    within `join_cluster_similarity`'s 2-hour tolerance of each other, and every one
    with a custom avatar) join in quick succession — a realistic benign surge, not a
    coordinated raid of cloned accounts. `join_velocity` alone can fire (a short-window
    join burst is still worth noting), but it must never, by itself, escalate any of
    these members past `WATCH` into `SUSPICIOUS`, and `WatchWindowService` must only
    open windows for the ones that reached `WATCH` — never for the untouched early
    joiners still at `CLEAR`.
    """
    entry_service = EntrySniffService(store)
    window_service = WatchWindowService(store)

    class SurgeGuild:
        def __init__(self) -> None:
            self.id = GUILD_ID
            self.members: list[SurgeMember] = []

    class SurgeMember:
        def __init__(self, member_id: int, created_hours_ago: float, guild: SurgeGuild) -> None:
            self.id = member_id
            self.guild = guild
            self.created_at = NOW - timedelta(hours=created_hours_ago)
            self.avatar = object()
            self.bot = False
            self.system = False
            self.pending = False
            self.name = f"member{member_id}"
            self.joined_at: datetime | None = NOW

    surge_guild = SurgeGuild()
    assessments: list[EntrySniffAssessment] = []
    for i in range(9):
        candidate = SurgeMember(1000 + i, created_hours_ago=(i + 1) * 50, guild=surge_guild)
        result = await entry_service.assess_join(cast(discord.Member, candidate), now=NOW)
        assessments.append(result)
        surge_guild.members.append(candidate)

    assert all(a.band in (RiskBand.CLEAR, RiskBand.WATCH) for a in assessments)

    opened: list[WatchWindow] = []
    for a in assessments:
        window = await window_service.open_if_warranted(a, now=NOW)
        if window is not None:
            opened.append(window)

    assert all(window.band is RiskBand.WATCH for window in opened)
    assert 0 < len(opened) < len(assessments)
    assert len(await store.list_open_watch_windows(GUILD_ID)) == len(opened)
