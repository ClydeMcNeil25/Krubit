"""SQLite persistence with guild scope built into every tenant operation."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from krubit.domain.models import ActionReceipt, GuildEvent, JSONValue
from krubit.security.redaction import redact


def _json_object(value: object) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError("stored JSON must be an object")
    return {str(key): item for key, item in value.items()}  # type: ignore[misc]


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
            """
        )
        await self._connection.commit()

    async def close(self) -> None:
        await self._connection.close()

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
        from datetime import datetime

        return GuildEvent(
            event_id=str(row["event_id"]),
            guild_id=int(row["guild_id"]),
            event_type=str(row["event_type"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            payload=_json_object(json.loads(str(row["payload_json"]))),
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
        from datetime import datetime

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
