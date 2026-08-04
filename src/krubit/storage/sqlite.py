"""SQLite persistence with guild scope built into every tenant operation."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite

from krubit.domain.companion import CoverageIssue, SnapshotRecord
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
