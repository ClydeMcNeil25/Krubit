"""Integration tests for guild-scoped live signal persistence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from krubit.domain.live_signals import (
    LiveSignalConfig,
    LiveSignalSession,
    LiveSignalStatus,
    TwitchStream,
)
from krubit.domain.models import GuildEvent
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)


def stream(stream_id: str = "stream-1") -> TwitchStream:
    return TwitchStream(
        stream_id=stream_id,
        user_login="krucialstudios",
        user_name="Krucial Studios",
        title="Building Krucial Town",
        game_name="Just Chatting",
        started_at=NOW,
        thumbnail_url="https://example.test/preview.jpg",
    )


def session(
    *,
    guild_id: int = 111,
    session_key: str = "session-1",
    detected_at: datetime = NOW,
    stream_value: TwitchStream | None = None,
    role_assigned_by_krubit: bool = False,
    twitch_url: str = "https://twitch.tv/krucialstudios",
) -> LiveSignalSession:
    return LiveSignalSession(
        guild_id=guild_id,
        session_key=session_key,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url=twitch_url,
        status=LiveSignalStatus.LIVE,
        detected_at=detected_at,
        presence_started_at=NOW - timedelta(minutes=2),
        stream=stream_value,
        role_id=333,
        role_assigned_by_krubit=role_assigned_by_krubit,
        last_discord_at=NOW,
        last_twitch_at=NOW,
    )


@pytest.mark.asyncio
async def test_live_session_and_delivery_are_guild_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session(guild_id=111))

        assert await store.get_live_session(111, "session-1") is not None
        assert await store.get_live_session(222, "session-1") is None
        opened = await store.open_live_session(111, 222, "krucialstudios")
        assert opened is not None and opened.session_key == "session-1"
        assert await store.open_live_session(222, 222, "krucialstudios") is None
        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is True
        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is False
        assert await store.claim_live_delivery(222, "stream:abc", "session-1") is True
        assert await store.claim_live_delivery(111, "provisional:merge", "session-1") is True
        assert await store.claim_live_delivery(222, "provisional:merge", "session-1") is True
        await store.merge_live_delivery_identity(
            111, "provisional:merge", "stream:merged", "session-1"
        )
        assert await store.get_live_delivery(111, "provisional:merge") is None
        assert await store.get_live_delivery(222, "provisional:merge") is not None
        assert await store.get_live_delivery(222, "stream:merged") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_config_keeps_configured_ids_after_resources_are_renamed(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        configured = LiveSignalConfig(111, 444, 555, NOW)
        await store.set_live_signal_config(configured)

        assert await store.get_live_signal_config(111) == configured
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_active_sessions_are_newest_first_and_exclude_ended(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session(session_key="older", detected_at=NOW))
        await store.save_live_session(
            session(session_key="newer", detected_at=NOW + timedelta(minutes=1))
        )
        await store.save_live_session(
            replace(
                session(session_key="ended", detected_at=NOW + timedelta(minutes=2)),
                status=LiveSignalStatus.ENDED,
                ended_at=NOW + timedelta(minutes=3),
            )
        )

        assert [item.session_key for item in await store.list_active_live_sessions(111)] == [
            "newer",
            "older",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_saving_enriched_session_merges_stream_id_without_duplicate_row(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session(stream_value=stream()))
        await store.save_live_session(
            session(session_key="later-observation", stream_value=stream())
        )

        saved = await store.get_live_session(111, "session-1")
        assert saved is not None and saved.stream == stream()
        assert await store.get_live_session(111, "later-observation") is None
        async with aiosqlite.connect(tmp_path / "krubit.db") as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM live_signal_sessions")
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_session_url_is_canonical_and_never_stores_query_or_fragment_secrets(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(
            session(
                twitch_url=(
                    "https://twitch.tv/krucialstudios?access_token=do-not-store#credential"
                )
            )
        )

        saved = await store.get_live_session(111, "session-1")
        assert saved is not None
        assert saved.twitch_url == "https://www.twitch.tv/krucialstudios"
        async with aiosqlite.connect(tmp_path / "krubit.db") as connection:
            cursor = await connection.execute("SELECT twitch_url FROM live_signal_sessions")
            row = await cursor.fetchone()
        assert row is not None
        assert str(row[0]) == "https://www.twitch.tv/krucialstudios"
        assert "do-not-store" not in str(row[0])
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_session_and_stream_identity_collision_coalesces_safely(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(
            replace(
                session(session_key="session-a", stream_value=stream("stream-a")),
                announcement_channel_id=444,
                announcement_message_id=1001,
                role_id=333,
                role_assigned_by_krubit=False,
            )
        )
        await store.save_live_session(
            replace(
                session(session_key="session-b", stream_value=stream("stream-b")),
                announcement_channel_id=555,
                announcement_message_id=2002,
                role_id=999,
                role_assigned_by_krubit=True,
            )
        )

        await store.save_live_session(
            replace(
                session(session_key="session-a", stream_value=stream("stream-b")),
                announcement_channel_id=None,
                announcement_message_id=None,
                role_id=None,
                role_assigned_by_krubit=False,
            )
        )

        saved = await store.get_live_session(111, "session-a")
        assert saved is not None
        assert saved.stream == stream("stream-b")
        assert (saved.announcement_channel_id, saved.announcement_message_id) == (555, 2002)
        assert (saved.role_id, saved.role_assigned_by_krubit) == (999, True)
        assert await store.get_live_session(111, "session-b") is None
        async with aiosqlite.connect(tmp_path / "krubit.db") as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM live_signal_sessions")
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_session_retains_preexisting_role_ownership(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session(role_assigned_by_krubit=False))

        saved = await store.get_live_session(111, "session-1")
        assert saved is not None and saved.role_assigned_by_krubit is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_delivery_can_be_claimed_again_for_retry(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is True
        await store.complete_live_delivery(
            111,
            "stream:abc",
            status="failed",
            channel_id=444,
            message_id=None,
            attempt=1,
        )

        assert await store.claim_live_delivery(111, "stream:abc", "session-1") is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_identity_merge_preserves_failed_retry_under_the_stream_key(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session())
        assert await store.claim_live_delivery(111, "provisional:session-1", "session-1") is True
        await store.complete_live_delivery(
            111,
            "provisional:session-1",
            status="failed",
            channel_id=444,
            message_id=None,
            attempt=1,
        )

        await store.merge_live_delivery_identity(
            111,
            "provisional:session-1",
            "stream:stream-1",
            "session-1",
        )

        assert await store.get_live_delivery(111, "provisional:session-1") is None
        delivery = await store.get_live_delivery(111, "stream:stream-1")
        assert delivery is not None
        assert delivery.session_key == "session-1"
        assert delivery.status == "failed"
        assert await store.claim_live_delivery(111, "stream:stream-1", "session-1") is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_identity_merge_preserves_a_claim_so_stream_enrichment_cannot_duplicate(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session())
        assert await store.claim_live_delivery(111, "provisional:session-1", "session-1") is True

        await store.merge_live_delivery_identity(
            111,
            "provisional:session-1",
            "stream:stream-1",
            "session-1",
        )

        delivery = await store.get_live_delivery(111, "stream:stream-1")
        assert delivery is not None and delivery.status == "claimed"
        assert await store.claim_live_delivery(111, "stream:stream-1", "session-1") is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_attempt_increments_on_retry_and_rejects_stale_completion(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session())
        first = await store.claim_live_delivery_attempt(111, "provisional:session-1", "session-1")
        assert first == 1
        assert await store.complete_live_delivery(
            111,
            "provisional:session-1",
            status="failed",
            channel_id=444,
            message_id=None,
            attempt=first,
        ) is True
        second = await store.claim_live_delivery_attempt(111, "provisional:session-1", "session-1")

        assert second == 2
        assert await store.complete_live_delivery(
            111,
            "provisional:session-1",
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=first,
        ) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_delivery_rejects_late_completion_before_it_is_reclaimed(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session())
        attempt = await store.claim_live_delivery_attempt(111, "provisional:session-1", "session-1")
        assert attempt == 1
        assert await store.complete_live_delivery(
            111,
            "provisional:session-1",
            status="failed",
            channel_id=444,
            message_id=None,
            attempt=attempt,
        ) is True

        assert await store.complete_live_delivery(
            111,
            "provisional:session-1",
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=attempt,
        ) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_identity_collision_invalidates_callbacks_from_both_old_rows(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.save_live_session(session())
        provisional_attempt = await store.claim_live_delivery_attempt(
            111, "provisional:session-1", "session-1"
        )
        assert provisional_attempt == 1
        assert await store.claim_live_delivery_attempt(111, "stream:stream-1", "session-1") == 1

        await store.merge_live_delivery_identity(
            111, "provisional:session-1", "stream:stream-1", "session-1"
        )

        merged = await store.get_live_delivery(111, "stream:stream-1")
        assert merged is not None and merged.status == "claimed" and merged.attempt == 2
        assert await store.complete_live_delivery(
            111,
            "stream:stream-1",
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=1,
        ) is False
        assert await store.complete_live_delivery(
            111,
            "stream:stream-1",
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=2,
        ) is True
        assert await store.complete_live_delivery(
            111,
            "stream:stream-1",
            status="succeeded",
            channel_id=444,
            message_id=1001,
            attempt=2,
        ) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_adds_delivery_attempt_to_existing_phase_two_table(tmp_path: Path) -> None:
    database = tmp_path / "krubit.db"
    async with aiosqlite.connect(database) as connection:
        await connection.execute(
            """
            CREATE TABLE live_signal_deliveries (
                guild_id INTEGER NOT NULL,
                delivery_key TEXT NOT NULL,
                session_key TEXT NOT NULL,
                status TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, delivery_key)
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO live_signal_deliveries VALUES
            (111, 'stream:existing', 'session-1', 'succeeded', 444, 1001, 'old', 'old')
            """
        )
        await connection.commit()

    store = await SQLiteStore.open(database)
    try:
        await store.initialize()
        delivery = await store.get_live_delivery(111, "stream:existing")
        assert delivery is not None and delivery.attempt == 1 and delivery.message_id == 1001
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_latest_live_check_result_is_guild_scoped(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.record_live_check(
            111, "first", "session-1", result="live", detail={}, checked_at=NOW
        )
        await store.record_live_check(
            111,
            "second",
            "session-1",
            result="unavailable",
            detail={"reason": "timeout"},
            checked_at=NOW + timedelta(minutes=1),
        )

        assert await store.latest_live_check_result(111) == "unavailable"
        assert await store.latest_live_check_result(222) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_check_details_are_redacted_before_storage(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        await store.record_live_check(
            111,
            "check-1",
            "session-1",
            result="unavailable",
            detail={"secret": "do-not-store"},
            checked_at=NOW,
        )

        async with aiosqlite.connect(tmp_path / "krubit.db") as connection:
            cursor = await connection.execute("SELECT detail_json FROM live_signal_checks")
            row = await cursor.fetchone()
        assert row is not None
        assert "do-not-store" not in str(row[0])
        assert '"secret":"[REDACTED]"' in str(row[0])
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_migrates_phase_one_database_without_losing_events(tmp_path: Path) -> None:
    database = tmp_path / "krubit.db"
    async with aiosqlite.connect(database) as connection:
        await connection.executescript(
            """
            CREATE TABLE guild_events (
                guild_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, event_id)
            );
            INSERT INTO guild_events VALUES (111, 'phase-one', 'ready',
                '2026-08-04T20:14:00+00:00', '{"source":"phase-one"}');
            """
        )
        await connection.commit()

    store = await SQLiteStore.open(database)
    try:
        await store.initialize()
        event = await store.get_event(111, "phase-one")
        assert event == GuildEvent(
            event_id="phase-one",
            guild_id=111,
            event_type="ready",
            occurred_at=NOW,
            payload={"source": "phase-one"},
        )
        assert await store.get_live_signal_config(111) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_signal_methods_reject_nonpositive_guild_ids(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    try:
        await store.initialize()
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.get_live_signal_config(0)
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.get_live_session(0, "session-1")
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.open_live_session(0, 222, "krucialstudios")
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.list_active_live_sessions(0)
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.claim_live_delivery(0, "stream:abc", "session-1")
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.get_live_delivery(0, "stream:abc")
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.merge_live_delivery_identity(0, "provisional:a", "stream:a", "session-1")
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.complete_live_delivery(
                0, "stream:abc", status="failed", channel_id=None, message_id=None, attempt=1
            )
        with pytest.raises(ValueError, match="guild_id must be positive"):
            await store.record_live_check(
                0, "check-1", "session-1", result="ok", detail={}, checked_at=NOW
            )
    finally:
        await store.close()
