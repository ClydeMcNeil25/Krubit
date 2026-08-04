"""SQLite persistence with guild scope built into every tenant operation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite

from krubit.domain.companion import CoverageIssue, SnapshotRecord
from krubit.domain.live_signals import (
    LiveSignalConfig,
    LiveSignalSession,
    LiveSignalStatus,
    TwitchStream,
)
from krubit.domain.models import ActionReceipt, GuildEvent, JSONValue
from krubit.security.redaction import redact


def _json_object(value: object) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError("stored JSON must be an object")
    return {str(key): item for key, item in value.items()}  # type: ignore[misc]


def _require_guild_id(guild_id: int) -> None:
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")


def _stored_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parsed_optional_timestamp(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (int, str)):
        raise ValueError("stored identifier must be an integer")
    return int(value)


@dataclass(frozen=True, slots=True)
class LiveSignalDelivery:
    """A guild-scoped durable announcement claim."""

    guild_id: int
    delivery_key: str
    session_key: str
    status: str
    attempt: int
    channel_id: int | None
    message_id: int | None


class SQLiteStore:
    """A single database whose public APIs always require tenant scope."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    @classmethod
    async def open(cls, path: Path) -> SQLiteStore:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA journal_mode = WAL")
        return cls(connection)

    async def initialize(self) -> None:
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS guild_events (
                guild_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, event_id)
            );

            CREATE TABLE IF NOT EXISTS action_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                actor_id INTEGER,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_receipts_guild_created
                ON action_receipts (guild_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS configuration_snapshots (
                guild_id INTEGER NOT NULL,
                snapshot_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                content_hash TEXT NOT NULL,
                content_json TEXT NOT NULL,
                coverage_json TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, snapshot_id),
                UNIQUE (guild_id, version),
                UNIQUE (guild_id, content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_guild_version
                ON configuration_snapshots (guild_id, version DESC);

            CREATE TABLE IF NOT EXISTS daily_summaries (
                guild_id INTEGER NOT NULL,
                summary_date TEXT NOT NULL,
                status TEXT NOT NULL,
                channel_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, summary_date)
            );

            CREATE TABLE IF NOT EXISTS live_signal_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS live_signal_sessions (
                guild_id INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                member_id INTEGER NOT NULL,
                twitch_login TEXT NOT NULL,
                twitch_url TEXT NOT NULL,
                status TEXT NOT NULL,
                presence_started_at TEXT,
                detected_at TEXT NOT NULL,
                stream_id TEXT,
                stream_display_name TEXT,
                stream_title TEXT,
                stream_category TEXT,
                stream_started_at TEXT,
                thumbnail_url TEXT,
                announcement_channel_id INTEGER,
                announcement_message_id INTEGER,
                role_id INTEGER,
                role_assigned_by_krubit INTEGER NOT NULL DEFAULT 0
                    CHECK (role_assigned_by_krubit IN (0, 1)),
                presence_active INTEGER NOT NULL DEFAULT 1
                    CHECK (presence_active IN (0, 1)),
                missing_since TEXT,
                last_discord_at TEXT NOT NULL,
                last_twitch_at TEXT,
                ended_at TEXT,
                PRIMARY KEY (guild_id, session_key)
            );

            CREATE INDEX IF NOT EXISTS idx_live_sessions_guild_status_detected
                ON live_signal_sessions (guild_id, status, detected_at DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_live_sessions_guild_stream
                ON live_signal_sessions (guild_id, stream_id)
                WHERE stream_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS live_signal_deliveries (
                guild_id INTEGER NOT NULL,
                delivery_key TEXT NOT NULL,
                session_key TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
                channel_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, delivery_key)
            );

            CREATE TABLE IF NOT EXISTS live_signal_checks (
                guild_id INTEGER NOT NULL,
                check_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                result TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, check_id)
            );
            """
        )
        columns_cursor = await self._connection.execute("PRAGMA table_info(live_signal_deliveries)")
        delivery_columns = {str(row["name"]) for row in await columns_cursor.fetchall()}
        if "attempt" not in delivery_columns:
            await self._connection.execute(
                """
                ALTER TABLE live_signal_deliveries
                ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0)
                """
            )
        await self._connection.commit()

    async def close(self) -> None:
        await self._connection.close()

    async def set_live_signal_config(self, config: LiveSignalConfig) -> None:
        """Persist the configured notification resources by their stable Discord IDs."""
        _require_guild_id(config.guild_id)
        await self._connection.execute(
            """
            INSERT INTO live_signal_config (guild_id, channel_id, role_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                role_id = excluded.role_id,
                updated_at = excluded.updated_at
            """,
            (config.guild_id, config.channel_id, config.role_id, config.updated_at.isoformat()),
        )
        await self._connection.commit()

    async def get_live_signal_config(self, guild_id: int) -> LiveSignalConfig | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM live_signal_config WHERE guild_id = ?", (guild_id,)
        )
        return self._live_signal_config_from_row(await cursor.fetchone())

    async def save_live_session(self, session: LiveSignalSession) -> LiveSignalSession:
        """Insert or update a session, retaining its first durable key for a known stream."""
        _require_guild_id(session.guild_id)
        await self._connection.execute("BEGIN IMMEDIATE")
        saved_session = session
        try:
            if session.stream is not None:
                key_cursor = await self._connection.execute(
                    """
                    SELECT * FROM live_signal_sessions
                    WHERE guild_id = ? AND session_key = ?
                    """,
                    (session.guild_id, session.session_key),
                )
                stream_cursor = await self._connection.execute(
                    """
                    SELECT * FROM live_signal_sessions
                    WHERE guild_id = ? AND stream_id = ?
                    """,
                    (session.guild_id, session.stream.stream_id),
                )
                key_row = await key_cursor.fetchone()
                stream_row = await stream_cursor.fetchone()
                if (
                    key_row is not None
                    and stream_row is not None
                    and key_row["session_key"] != stream_row["session_key"]
                ):
                    saved_session = self._coalesce_live_sessions(
                        self._live_signal_session_from_stored_row(key_row),
                        self._live_signal_session_from_stored_row(stream_row),
                        session,
                    )
                    await self._connection.execute(
                        """
                        UPDATE live_signal_deliveries SET session_key = ?
                        WHERE guild_id = ? AND session_key = ?
                        """,
                        (saved_session.session_key, session.guild_id, stream_row["session_key"]),
                    )
                    await self._connection.execute(
                        """
                        UPDATE live_signal_checks SET session_key = ?
                        WHERE guild_id = ? AND session_key = ?
                        """,
                        (saved_session.session_key, session.guild_id, stream_row["session_key"]),
                    )
                    await self._connection.execute(
                        """
                        DELETE FROM live_signal_sessions
                        WHERE guild_id = ? AND session_key = ?
                        """,
                        (session.guild_id, stream_row["session_key"]),
                    )
            await self._upsert_live_session(saved_session)
            await self._connection.commit()
        except BaseException:
            await self._connection.rollback()
            raise
        session_key = saved_session.session_key
        if saved_session.stream is not None:
            cursor = await self._connection.execute(
                """
                SELECT session_key FROM live_signal_sessions
                WHERE guild_id = ? AND stream_id = ?
                """,
                (saved_session.guild_id, saved_session.stream.stream_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("saved live session stream could not be read")
            session_key = str(row["session_key"])
        saved = await self.get_live_session(saved_session.guild_id, session_key)
        if saved is None:
            raise RuntimeError("saved live session could not be read")
        return saved

    async def _upsert_live_session(self, session: LiveSignalSession) -> None:
        stream = session.stream
        await self._connection.execute(
            """
            INSERT INTO live_signal_sessions (
                guild_id, session_key, member_id, twitch_login, twitch_url, status,
                presence_started_at, detected_at, stream_id, stream_display_name,
                stream_title, stream_category, stream_started_at, thumbnail_url,
                announcement_channel_id, announcement_message_id, role_id,
                role_assigned_by_krubit, presence_active, missing_since, last_discord_at,
                last_twitch_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, session_key) DO UPDATE SET
                member_id = excluded.member_id,
                twitch_login = excluded.twitch_login,
                twitch_url = excluded.twitch_url,
                status = excluded.status,
                presence_started_at = excluded.presence_started_at,
                detected_at = excluded.detected_at,
                stream_id = excluded.stream_id,
                stream_display_name = excluded.stream_display_name,
                stream_title = excluded.stream_title,
                stream_category = excluded.stream_category,
                stream_started_at = excluded.stream_started_at,
                thumbnail_url = excluded.thumbnail_url,
                announcement_channel_id = excluded.announcement_channel_id,
                announcement_message_id = excluded.announcement_message_id,
                role_id = excluded.role_id,
                role_assigned_by_krubit = excluded.role_assigned_by_krubit,
                presence_active = excluded.presence_active,
                missing_since = excluded.missing_since,
                last_discord_at = excluded.last_discord_at,
                last_twitch_at = excluded.last_twitch_at,
                ended_at = excluded.ended_at
            ON CONFLICT(guild_id, stream_id) WHERE stream_id IS NOT NULL DO UPDATE SET
                member_id = excluded.member_id,
                twitch_login = excluded.twitch_login,
                twitch_url = excluded.twitch_url,
                status = excluded.status,
                presence_started_at = excluded.presence_started_at,
                detected_at = excluded.detected_at,
                stream_display_name = excluded.stream_display_name,
                stream_title = excluded.stream_title,
                stream_category = excluded.stream_category,
                stream_started_at = excluded.stream_started_at,
                thumbnail_url = excluded.thumbnail_url,
                announcement_channel_id = excluded.announcement_channel_id,
                announcement_message_id = excluded.announcement_message_id,
                role_id = excluded.role_id,
                role_assigned_by_krubit = excluded.role_assigned_by_krubit,
                presence_active = excluded.presence_active,
                missing_since = excluded.missing_since,
                last_discord_at = excluded.last_discord_at,
                last_twitch_at = excluded.last_twitch_at,
                ended_at = excluded.ended_at
            """,
            (
                session.guild_id,
                session.session_key,
                session.member_id,
                session.twitch_login,
                session.twitch_url,
                session.status.value,
                _stored_timestamp(session.presence_started_at),
                session.detected_at.isoformat(),
                stream.stream_id if stream is not None else None,
                stream.user_name if stream is not None else None,
                stream.title if stream is not None else None,
                stream.game_name if stream is not None else None,
                _stored_timestamp(stream.started_at) if stream is not None else None,
                stream.thumbnail_url if stream is not None else None,
                session.announcement_channel_id,
                session.announcement_message_id,
                session.role_id,
                int(session.role_assigned_by_krubit),
                int(session.presence_active),
                _stored_timestamp(session.missing_since),
                _stored_timestamp(session.last_discord_at or session.detected_at),
                _stored_timestamp(session.last_twitch_at),
                _stored_timestamp(session.ended_at),
            ),
        )

    @staticmethod
    def _coalesce_live_sessions(
        key_session: LiveSignalSession,
        stream_session: LiveSignalSession,
        incoming_session: LiveSignalSession,
    ) -> LiveSignalSession:
        """Keep the key identity while retaining the stream identity's public state."""
        announcement_source = next(
            (
                item
                for item in (stream_session, incoming_session, key_session)
                if item.announcement_channel_id is not None
                and item.announcement_message_id is not None
            ),
            key_session,
        )
        role_source = next(
            (
                item
                for item in (stream_session, incoming_session, key_session)
                if item.role_id is not None
            ),
            key_session,
        )
        role_id = role_source.role_id
        role_assigned_by_krubit = role_id is not None and any(
            item.role_id == role_id and item.role_assigned_by_krubit
            for item in (incoming_session, stream_session, key_session)
        )
        return LiveSignalSession(
            guild_id=incoming_session.guild_id,
            session_key=key_session.session_key,
            member_id=incoming_session.member_id,
            twitch_login=incoming_session.twitch_login,
            twitch_url=incoming_session.twitch_url,
            status=incoming_session.status,
            detected_at=incoming_session.detected_at,
            presence_started_at=incoming_session.presence_started_at,
            stream=incoming_session.stream,
            announcement_channel_id=announcement_source.announcement_channel_id,
            announcement_message_id=announcement_source.announcement_message_id,
            role_id=role_id,
            role_assigned_by_krubit=role_assigned_by_krubit,
            presence_active=incoming_session.presence_active,
            missing_since=incoming_session.missing_since,
            last_discord_at=incoming_session.last_discord_at,
            last_twitch_at=incoming_session.last_twitch_at,
            ended_at=incoming_session.ended_at,
        )

    async def open_live_session(
        self, guild_id: int, member_id: int, twitch_login: str
    ) -> LiveSignalSession | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM live_signal_sessions
            WHERE guild_id = ? AND member_id = ? AND twitch_login = ?
                AND status != ? AND ended_at IS NULL
            ORDER BY detected_at DESC, session_key DESC
            LIMIT 1
            """,
            (guild_id, member_id, twitch_login, LiveSignalStatus.ENDED.value),
        )
        return self._live_signal_session_from_row(await cursor.fetchone())

    async def get_live_session(
        self, guild_id: int, session_key: str
    ) -> LiveSignalSession | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM live_signal_sessions
            WHERE guild_id = ? AND session_key = ?
            """,
            (guild_id, session_key),
        )
        return self._live_signal_session_from_row(await cursor.fetchone())

    async def list_active_live_sessions(self, guild_id: int) -> list[LiveSignalSession]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM live_signal_sessions
            WHERE guild_id = ? AND status != ? AND ended_at IS NULL
            ORDER BY detected_at DESC, session_key DESC
            """,
            (guild_id, LiveSignalStatus.ENDED.value),
        )
        return [self._live_signal_session_from_stored_row(row) for row in await cursor.fetchall()]

    async def claim_live_delivery(
        self, guild_id: int, delivery_key: str, session_key: str
    ) -> bool:
        attempt = await self.claim_live_delivery_attempt(guild_id, delivery_key, session_key)
        return attempt is not None

    async def claim_live_delivery_attempt(
        self, guild_id: int, delivery_key: str, session_key: str
    ) -> int | None:
        """Claim a delivery and return its monotonically increasing attempt identity."""
        _require_guild_id(guild_id)
        now = datetime.now(UTC).isoformat()
        await self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._connection.execute(
                """
                SELECT status, attempt FROM live_signal_deliveries
                WHERE guild_id = ? AND delivery_key = ?
                """,
                (guild_id, delivery_key),
            )
            row = await cursor.fetchone()
            if row is None:
                await self._connection.execute(
                    """
                    INSERT INTO live_signal_deliveries (
                        guild_id, delivery_key, session_key, status, attempt, channel_id,
                        message_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'claimed', 1, NULL, NULL, ?, ?)
                    """,
                    (guild_id, delivery_key, session_key, now, now),
                )
                attempt = 1
            elif str(row["status"]) == "failed":
                attempt = int(row["attempt"]) + 1
                await self._connection.execute(
                    """
                    UPDATE live_signal_deliveries
                    SET session_key = ?, status = 'claimed', attempt = ?, updated_at = ?
                    WHERE guild_id = ? AND delivery_key = ?
                    """,
                    (session_key, attempt, now, guild_id, delivery_key),
                )
            else:
                attempt = None
            await self._connection.commit()
            return attempt
        except BaseException:
            await self._connection.rollback()
            raise

    async def get_live_delivery(
        self, guild_id: int, delivery_key: str
    ) -> LiveSignalDelivery | None:
        """Look up one guild-scoped delivery claim without exposing message content."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, delivery_key, session_key, status, attempt, channel_id, message_id
            FROM live_signal_deliveries
            WHERE guild_id = ? AND delivery_key = ?
            """,
            (guild_id, delivery_key),
        )
        return self._live_signal_delivery_from_row(await cursor.fetchone())

    async def merge_live_delivery_identity(
        self,
        guild_id: int,
        provisional_key: str,
        stream_key: str,
        session_key: str,
    ) -> None:
        """Atomically carry a provisional claim forward once Helix supplies a stream ID."""
        _require_guild_id(guild_id)
        if not provisional_key or not stream_key or not session_key:
            raise ValueError("delivery and session keys must not be blank")
        await self._connection.execute("BEGIN IMMEDIATE")
        try:
            source_cursor = await self._connection.execute(
                """
                SELECT guild_id, delivery_key, session_key, status, attempt, channel_id, message_id
                FROM live_signal_deliveries
                WHERE guild_id = ? AND delivery_key = ?
                """,
                (guild_id, provisional_key),
            )
            destination_cursor = await self._connection.execute(
                """
                SELECT guild_id, delivery_key, session_key, status, attempt, channel_id, message_id
                FROM live_signal_deliveries
                WHERE guild_id = ? AND delivery_key = ?
                """,
                (guild_id, stream_key),
            )
            source = self._live_signal_delivery_from_row(await source_cursor.fetchone())
            destination = self._live_signal_delivery_from_row(await destination_cursor.fetchone())
            if source is not None and provisional_key != stream_key and destination is None:
                await self._connection.execute(
                    """
                    UPDATE live_signal_deliveries
                    SET delivery_key = ?, session_key = ?, updated_at = ?
                    WHERE guild_id = ? AND delivery_key = ?
                    """,
                    (
                        stream_key,
                        session_key,
                        datetime.now(UTC).isoformat(),
                        guild_id,
                        provisional_key,
                    ),
                )
            elif source is not None and provisional_key != stream_key and destination is not None:
                retained = max((source, destination), key=self._delivery_priority)
                await self._connection.execute(
                    """
                    UPDATE live_signal_deliveries
                    SET session_key = ?, status = ?, attempt = ?, channel_id = ?, message_id = ?,
                        updated_at = ?
                    WHERE guild_id = ? AND delivery_key = ?
                    """,
                    (
                        session_key,
                        retained.status,
                        retained.attempt,
                        retained.channel_id,
                        retained.message_id,
                        datetime.now(UTC).isoformat(),
                        guild_id,
                        stream_key,
                    ),
                )
                await self._connection.execute(
                    """
                    DELETE FROM live_signal_deliveries
                    WHERE guild_id = ? AND delivery_key = ?
                    """,
                    (guild_id, provisional_key),
                )
            elif destination is not None:
                await self._connection.execute(
                    """
                    UPDATE live_signal_deliveries SET session_key = ?, updated_at = ?
                    WHERE guild_id = ? AND delivery_key = ?
                    """,
                    (session_key, datetime.now(UTC).isoformat(), guild_id, stream_key),
                )
            await self._connection.commit()
        except BaseException:
            await self._connection.rollback()
            raise

    async def complete_live_delivery(
        self,
        guild_id: int,
        delivery_key: str,
        *,
        status: str,
        channel_id: int | None,
        message_id: int | None,
        attempt: int | None = None,
    ) -> bool:
        _require_guild_id(guild_id)
        if attempt is not None and attempt <= 0:
            raise ValueError("attempt must be positive")
        query = """
            UPDATE live_signal_deliveries
            SET status = ?, channel_id = ?, message_id = ?, updated_at = ?
            WHERE guild_id = ? AND delivery_key = ?
        """
        params: tuple[object, ...] = (
            status,
            channel_id,
            message_id,
            datetime.now(UTC).isoformat(),
            guild_id,
            delivery_key,
        )
        if attempt is not None:
            query += " AND attempt = ?"
            params += (attempt,)
        cursor = await self._connection.execute(query, params)
        await self._connection.commit()
        return cursor.rowcount == 1

    async def record_live_check(
        self,
        guild_id: int,
        check_id: str,
        session_key: str,
        *,
        result: str,
        detail: dict[str, JSONValue],
        checked_at: datetime,
    ) -> None:
        _require_guild_id(guild_id)
        safe_detail = _json_object(redact(detail))
        await self._connection.execute(
            """
            INSERT INTO live_signal_checks (
                guild_id, check_id, session_key, result, detail_json, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, check_id) DO UPDATE SET
                session_key = excluded.session_key,
                result = excluded.result,
                detail_json = excluded.detail_json,
                checked_at = excluded.checked_at
            """,
            (
                guild_id,
                check_id,
                session_key,
                result,
                json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                checked_at.isoformat(),
            ),
        )
        await self._connection.commit()

    async def latest_live_check_result(self, guild_id: int) -> str | None:
        """Return only the newest redacted Twitch-check classification for a guild."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT result FROM live_signal_checks
            WHERE guild_id = ?
            ORDER BY checked_at DESC, check_id DESC
            LIMIT 1
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        return str(row["result"]) if row is not None else None

    @staticmethod
    def _live_signal_config_from_row(row: aiosqlite.Row | None) -> LiveSignalConfig | None:
        if row is None:
            return None
        return LiveSignalConfig(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            role_id=int(row["role_id"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _live_signal_delivery_from_row(
        row: aiosqlite.Row | None,
    ) -> LiveSignalDelivery | None:
        if row is None:
            return None
        return LiveSignalDelivery(
            guild_id=int(row["guild_id"]),
            delivery_key=str(row["delivery_key"]),
            session_key=str(row["session_key"]),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            channel_id=_optional_int(row["channel_id"]),
            message_id=_optional_int(row["message_id"]),
        )

    @staticmethod
    def _delivery_priority(delivery: LiveSignalDelivery) -> int:
        return {"failed": 0, "claimed": 1, "succeeded": 2}.get(delivery.status, 3)

    @staticmethod
    def _live_signal_session_from_row(row: aiosqlite.Row | None) -> LiveSignalSession | None:
        return SQLiteStore._live_signal_session_from_stored_row(row) if row is not None else None

    @staticmethod
    def _live_signal_session_from_stored_row(row: aiosqlite.Row) -> LiveSignalSession:
        stream_id = row["stream_id"]
        stream = None
        if stream_id is not None:
            stream = TwitchStream(
                stream_id=str(stream_id),
                user_login=str(row["twitch_login"]),
                user_name=str(row["stream_display_name"]),
                title=str(row["stream_title"]),
                game_name=str(row["stream_category"]),
                started_at=datetime.fromisoformat(str(row["stream_started_at"])),
                thumbnail_url=str(row["thumbnail_url"]),
            )
        return LiveSignalSession(
            guild_id=int(row["guild_id"]),
            session_key=str(row["session_key"]),
            member_id=int(row["member_id"]),
            twitch_login=str(row["twitch_login"]),
            twitch_url=str(row["twitch_url"]),
            status=LiveSignalStatus(str(row["status"])),
            detected_at=datetime.fromisoformat(str(row["detected_at"])),
            presence_started_at=_parsed_optional_timestamp(row["presence_started_at"]),
            stream=stream,
            announcement_channel_id=_optional_int(row["announcement_channel_id"]),
            announcement_message_id=_optional_int(row["announcement_message_id"]),
            role_id=_optional_int(row["role_id"]),
            role_assigned_by_krubit=bool(row["role_assigned_by_krubit"]),
            presence_active=bool(row["presence_active"]),
            missing_since=_parsed_optional_timestamp(row["missing_since"]),
            last_discord_at=_parsed_optional_timestamp(row["last_discord_at"]),
            last_twitch_at=_parsed_optional_timestamp(row["last_twitch_at"]),
            ended_at=_parsed_optional_timestamp(row["ended_at"]),
        )

    async def set_guild_enabled(self, guild_id: int, enabled: bool) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        await self._connection.execute(
            """
            INSERT INTO guild_config (guild_id, enabled, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, int(enabled)),
        )
        await self._connection.commit()

    async def guild_is_enabled(self, guild_id: int) -> bool:
        cursor = await self._connection.execute(
            "SELECT enabled FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return bool(row["enabled"]) if row is not None else False

    async def accept_event(self, event: GuildEvent) -> bool:
        payload = redact(dict(event.payload))
        cursor = await self._connection.execute(
            """
            INSERT OR IGNORE INTO guild_events
                (guild_id, event_id, event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.guild_id,
                event.event_id,
                event.event_type,
                event.occurred_at.isoformat(),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        await self._connection.commit()
        return cursor.rowcount == 1

    async def get_event(self, guild_id: int, event_id: str) -> GuildEvent | None:
        cursor = await self._connection.execute(
            """
            SELECT event_id, guild_id, event_type, occurred_at, payload_json
            FROM guild_events WHERE guild_id = ? AND event_id = ?
            """,
            (guild_id, event_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return GuildEvent(
            event_id=str(row["event_id"]),
            guild_id=int(row["guild_id"]),
            event_type=str(row["event_type"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            payload=_json_object(json.loads(str(row["payload_json"]))),
        )

    async def list_events(self, guild_id: int, limit: int = 25) -> list[GuildEvent]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT event_id, guild_id, event_type, occurred_at, payload_json
            FROM guild_events
            WHERE guild_id = ?
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            GuildEvent(
                event_id=str(row["event_id"]),
                guild_id=int(row["guild_id"]),
                event_type=str(row["event_type"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                payload=_json_object(json.loads(str(row["payload_json"]))),
            )
            for row in rows
        ]

    async def save_snapshot(
        self,
        guild_id: int,
        content: dict[str, JSONValue],
        coverage: tuple[CoverageIssue, ...],
        captured_at: datetime,
    ) -> SnapshotRecord:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        safe_content = _json_object(redact(content))
        coverage_payload = [
            {"section": item.section, "status": item.status, "detail": item.detail}
            for item in coverage
        ]
        content_json = json.dumps(safe_content, sort_keys=True, separators=(",", ":"))
        coverage_json = json.dumps(coverage_payload, sort_keys=True, separators=(",", ":"))
        digest = sha256(f"{content_json}\n{coverage_json}".encode()).hexdigest()
        existing = await self._snapshot_by_hash(guild_id, digest)
        if existing is not None:
            return existing
        cursor = await self._connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS version
            FROM configuration_snapshots WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("snapshot version query returned no row")
        snapshot_id = f"snapshot:{uuid4().hex}"
        version = int(row["version"])
        await self._connection.execute(
            """
            INSERT INTO configuration_snapshots
                (guild_id, snapshot_id, version, content_hash,
                 content_json, coverage_json, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                snapshot_id,
                version,
                digest,
                content_json,
                coverage_json,
                captured_at.isoformat(),
            ),
        )
        await self._connection.commit()
        snapshot = await self.get_snapshot(guild_id, snapshot_id)
        if snapshot is None:
            raise RuntimeError("saved snapshot could not be read")
        return snapshot

    async def _snapshot_by_hash(self, guild_id: int, content_hash: str) -> SnapshotRecord | None:
        cursor = await self._connection.execute(
            "SELECT * FROM configuration_snapshots WHERE guild_id = ? AND content_hash = ?",
            (guild_id, content_hash),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def get_snapshot(self, guild_id: int, snapshot_id: str) -> SnapshotRecord | None:
        cursor = await self._connection.execute(
            "SELECT * FROM configuration_snapshots WHERE guild_id = ? AND snapshot_id = ?",
            (guild_id, snapshot_id),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def latest_snapshot(self, guild_id: int) -> SnapshotRecord | None:
        cursor = await self._connection.execute(
            """
            SELECT * FROM configuration_snapshots
            WHERE guild_id = ? ORDER BY version DESC LIMIT 1
            """,
            (guild_id,),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    @staticmethod
    def _snapshot_from_row(row: aiosqlite.Row | None) -> SnapshotRecord | None:
        if row is None:
            return None
        raw_coverage = json.loads(str(row["coverage_json"]))
        if not isinstance(raw_coverage, list):
            raise ValueError("stored coverage JSON must be a list")
        coverage_items: list[CoverageIssue] = []
        for raw_item in cast(list[object], raw_coverage):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, object], raw_item)
            section = item.get("section")
            status = item.get("status")
            detail = item.get("detail")
            if not all(isinstance(value, str) for value in (section, status, detail)):
                raise ValueError("stored coverage item fields must be strings")
            coverage_items.append(
                CoverageIssue(
                    section=cast(str, section),
                    status=cast(str, status),
                    detail=cast(str, detail),
                )
            )
        return SnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            guild_id=int(row["guild_id"]),
            version=int(row["version"]),
            content_hash=str(row["content_hash"]),
            content=_json_object(json.loads(str(row["content_json"]))),
            coverage=tuple(coverage_items),
            captured_at=datetime.fromisoformat(str(row["captured_at"])),
        )

    async def record_receipt(self, receipt: ActionReceipt) -> None:
        detail = redact(dict(receipt.detail))
        await self._connection.execute(
            """
            INSERT INTO action_receipts
                (guild_id, receipt_id, action, status, actor_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.guild_id,
                receipt.receipt_id,
                receipt.action,
                receipt.status,
                receipt.actor_id,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
                receipt.created_at.isoformat(),
            ),
        )
        await self._connection.commit()

    async def list_receipts(self, guild_id: int, limit: int = 50) -> list[ActionReceipt]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT receipt_id, guild_id, action, status, actor_id, detail_json, created_at
            FROM action_receipts
            WHERE guild_id = ?
            ORDER BY created_at DESC, receipt_id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            ActionReceipt(
                receipt_id=str(row["receipt_id"]),
                guild_id=int(row["guild_id"]),
                action=str(row["action"]),
                status=str(row["status"]),
                actor_id=int(row["actor_id"]) if row["actor_id"] is not None else None,
                detail=_json_object(json.loads(str(row["detail_json"]))),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    async def counts(self, guild_id: int) -> tuple[int, int]:
        event_cursor = await self._connection.execute(
            "SELECT COUNT(*) AS total FROM guild_events WHERE guild_id = ?", (guild_id,)
        )
        receipt_cursor = await self._connection.execute(
            "SELECT COUNT(*) AS total FROM action_receipts WHERE guild_id = ?", (guild_id,)
        )
        event_row = await event_cursor.fetchone()
        receipt_row = await receipt_cursor.fetchone()
        if event_row is None or receipt_row is None:
            raise RuntimeError("SQLite COUNT query returned no row")
        return int(event_row["total"]), int(receipt_row["total"])

    async def claim_daily_summary(self, guild_id: int, summary_date: date) -> bool:
        cursor = await self._connection.execute(
            """
            INSERT OR IGNORE INTO daily_summaries
                (guild_id, summary_date, status, channel_id, created_at)
            VALUES (?, ?, 'claimed', NULL, ?)
            """,
            (guild_id, summary_date.isoformat(), datetime.now(UTC).isoformat()),
        )
        await self._connection.commit()
        return cursor.rowcount == 1

    async def set_daily_summary_status(
        self,
        guild_id: int,
        summary_date: date,
        status: str,
        channel_id: int | None,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO daily_summaries
                (guild_id, summary_date, status, channel_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, summary_date) DO UPDATE SET
                status = excluded.status,
                channel_id = excluded.channel_id
            """,
            (
                guild_id,
                summary_date.isoformat(),
                status,
                channel_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        await self._connection.commit()

    async def daily_summary_status(self, guild_id: int, summary_date: date) -> str | None:
        cursor = await self._connection.execute(
            "SELECT status FROM daily_summaries WHERE guild_id = ? AND summary_date = ?",
            (guild_id, summary_date.isoformat()),
        )
        row = await cursor.fetchone()
        return str(row["status"]) if row is not None else None
