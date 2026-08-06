"""SQLite persistence with guild scope built into every tenant operation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite

from krubit.domain.activity_ledger import (
    ExclusionEntry,
    LedgerEvent,
    Milestone,
    RetentionPolicy,
)
from krubit.domain.companion import CoverageIssue, SnapshotRecord
from krubit.domain.creator_signals import (
    CapabilityState,
    ContentCursor,
    ContentDelivery,
    ContentEvent,
    ContentKind,
    ContentObservation,
    ContentPlan,
    ContentState,
    CreatorAccount,
    CreatorProfile,
    CreatorRoute,
    Platform,
    reaches_publish_or_live,
)
from krubit.domain.live_signals import (
    LiveSignalConfig,
    LiveSignalSession,
    LiveSignalStatus,
    TwitchStream,
)
from krubit.domain.models import ActionReceipt, GuildEvent, JSONValue
from krubit.domain.watchdog import (
    AllowBlockEntry,
    EntrySniffAssessment,
    Incident,
    WatchWindow,
    WatchWindowCloseReason,
)
from krubit.security.redaction import redact
from krubit.storage.activity_ledger_rows import (
    exclusion_entry_from_row,
    ledger_event_detail,
    ledger_event_from_row,
    milestone_from_row,
    retention_policy_from_row,
)
from krubit.storage.creator_rows import (
    content_cursor_from_row,
    content_delivery_from_row,
    content_event_from_row,
    creator_account_from_row,
    creator_profile_from_row,
    creator_route_from_row,
)
from krubit.storage.watchdog_rows import (
    allow_block_entry_from_row,
    entry_sniff_assessment_from_row,
    incident_from_row,
    watch_window_from_row,
)


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


@dataclass(frozen=True, slots=True)
class CreatorRegistryReceipt:
    """A guild-scoped, redacted audit record of a creator registry authority decision.

    Storage-only view type: every row here represents a change that already succeeded,
    so unlike `ActionReceipt` there is no `status` field to track a failed attempt.
    """

    guild_id: int
    receipt_id: str
    account_id: str | None
    action: str
    actor_member_id: int
    detail: dict[str, JSONValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContentReceipt:
    """A guild-scoped, redacted diagnostic record for the content ledger.

    Storage-only view type, matching the `CreatorRegistryReceipt` convention: every row
    documents an ingestion-time decision (most commonly a malformed connector item that
    was skipped rather than delivered), not a mutable domain entity.
    """

    guild_id: int
    receipt_id: str
    account_id: str | None
    platform: Platform | None
    external_id: str | None
    action: str
    detail: dict[str, JSONValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MentionBudgetReceipt:
    """A guild-scoped, redacted audit record of one mention-budget outcome.

    Every delivery decision that touches a mention budget — whether it consumed a unit,
    was suppressed by an exhausted budget, or bypassed budget accounting entirely (for
    example an unlimited budget) — is recorded here, matching the design's "every
    consumed, suppressed, or bypassed mention is recorded" rule.
    """

    guild_id: int
    receipt_id: str
    budget_kind: str
    period_key: str
    outcome: str
    platform: Platform | None
    external_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledEventMapping:
    """A guild-scoped, exact-ID owned link between one content item and a Discord
    Scheduled Event, matching the `content_deliveries` guild-scoped mapping
    convention.

    Identity is `(guild_id, platform, external_id)`. `discord_event_id` is the only
    field a `ScheduledEventSynchronizer` (Task 11) resolves the live Discord object
    by — never the event's mutable `name`. `owned_by_krubit` is the ownership receipt:
    once `False` (whether because Krubit never created this mapping's row, or the row
    was seeded directly by a caller) the synchronizer must refuse to mutate it.
    """

    guild_id: int
    account_id: str
    platform: Platform
    external_id: str
    discord_event_id: int | None
    discord_status: str
    owned_by_krubit: bool
    content_hash: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CreatorBootstrap:
    """One guild's once-resolved creator-notification resources.

    `creator_role_id` and `notification_channel_id` are resolved by exact Discord name
    a single time (see `krubit.discord.content_commands`) and then persisted here so
    every later lookup uses the stable ID rather than re-searching by a mutable name.
    """

    guild_id: int
    creator_role_id: int
    notification_channel_id: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContentScheduleState:
    """One account's durable next-poll bookkeeping for `ConnectorScheduler`.

    Identity is `(guild_id, account_id, platform)`, matching `content_cursors`.
    `next_poll_at` is the only field a scheduler restart needs to trust: a fresh
    `ConnectorScheduler` instance reads it back exactly as the previous process left
    it, so a restart never polls an account early just because in-memory state was
    lost. `consecutive_failures` and `last_state`/`last_detail` are the backoff and
    health-reporting bookkeeping `run_cycle` updates every time this account is polled.
    """

    guild_id: int
    account_id: str
    platform: Platform
    next_poll_at: datetime
    interval_seconds: int
    consecutive_failures: int
    last_state: CapabilityState
    last_detail: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SniffReceipt:
    """A guild-scoped, redacted audit record of one watchdog storage transition.

    Storage-only view type, matching the `CreatorRegistryReceipt`/`ContentReceipt`
    convention: append-only, mirroring every `entry_sniff_assessments` write,
    `watch_windows` transition, and incident evidence write per the design doc's
    Data Model section. `member_id` is `None` for guild-scoped events (for example a
    raid/spam-wave incident) that are not about a single member.
    """

    guild_id: int
    receipt_id: str
    member_id: int | None
    action: str
    detail: dict[str, JSONValue]
    created_at: datetime


_CONTENT_DELIVERY_STATUSES = frozenset({"pending", "delivered", "cancelled", "failed"})


class SQLiteStore:
    """A single database whose public APIs always require tenant scope."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        # aiosqlite serializes statements, not coroutine transaction boundaries.
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SQLiteStore:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA journal_mode = WAL")
        return cls(connection)

    @asynccontextmanager
    async def _write_transaction(self, *, immediate: bool = False) -> AsyncGenerator[None]:
        async with self._write_lock:
            try:
                if immediate:
                    await self._connection.execute("BEGIN IMMEDIATE")
                yield
                await self._connection.commit()
            except BaseException:
                with suppress(BaseException):
                    await self._connection.rollback()
                raise

    async def initialize(self) -> None:
        async with self._write_transaction():
            await self._initialize()

    async def _initialize(self) -> None:
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

            CREATE TABLE IF NOT EXISTS creator_profiles (
                guild_id INTEGER NOT NULL,
                owner_member_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, owner_member_id)
            );

            CREATE TABLE IF NOT EXISTS creator_accounts (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                owner_member_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                handle TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                external_id TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 1 CHECK (paused IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id),
                FOREIGN KEY (guild_id, owner_member_id)
                    REFERENCES creator_profiles (guild_id, owner_member_id)
            );

            CREATE INDEX IF NOT EXISTS idx_creator_accounts_guild_owner
                ON creator_accounts (guild_id, owner_member_id);

            CREATE TABLE IF NOT EXISTS creator_routes (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                mention_role_id INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id, content_kind),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS connector_authorizations (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                secret_ref TEXT,
                status TEXT NOT NULL,
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id, capability),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS creator_registry_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                account_id TEXT,
                action TEXT NOT NULL,
                actor_member_id INTEGER NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_creator_registry_receipts_guild_account
                ON creator_registry_receipts (guild_id, account_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS content_events (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                title TEXT,
                published_at TEXT,
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, platform, external_id),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_content_events_guild_account
                ON content_events (guild_id, account_id, last_observed_at DESC);

            CREATE TABLE IF NOT EXISTS content_cursors (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                cursor_value TEXT,
                baselined_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS content_deliveries (
                guild_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                transition_seq INTEGER NOT NULL CHECK (transition_seq > 0),
                account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
                discord_channel_id INTEGER,
                discord_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, platform, external_id, transition_seq),
                FOREIGN KEY (guild_id, platform, external_id)
                    REFERENCES content_events (guild_id, platform, external_id)
            );

            CREATE INDEX IF NOT EXISTS idx_content_deliveries_guild_status
                ON content_deliveries (guild_id, status, created_at ASC);

            CREATE TABLE IF NOT EXISTS content_delivery_attempts (
                guild_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                transition_seq INTEGER NOT NULL CHECK (transition_seq > 0),
                attempt INTEGER NOT NULL CHECK (attempt > 0),
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, platform, external_id, transition_seq, attempt),
                FOREIGN KEY (guild_id, platform, external_id, transition_seq)
                    REFERENCES content_deliveries (guild_id, platform, external_id, transition_seq)
            );

            CREATE TABLE IF NOT EXISTS content_correlations (
                guild_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                correlation_group TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, platform, external_id),
                FOREIGN KEY (guild_id, platform, external_id)
                    REFERENCES content_events (guild_id, platform, external_id)
            );

            CREATE TABLE IF NOT EXISTS mention_budget_state (
                guild_id INTEGER NOT NULL,
                budget_kind TEXT NOT NULL,
                period_key TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, budget_kind, period_key)
            );

            CREATE TABLE IF NOT EXISTS mention_budget_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                budget_kind TEXT NOT NULL,
                period_key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                platform TEXT,
                external_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_mention_budget_receipts_guild_created
                ON mention_budget_receipts (guild_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS content_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                account_id TEXT,
                platform TEXT,
                external_id TEXT,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_content_receipts_guild_created
                ON content_receipts (guild_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS scheduled_event_mappings (
                guild_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                discord_event_id INTEGER,
                discord_status TEXT NOT NULL,
                owned_by_krubit INTEGER NOT NULL DEFAULT 0 CHECK (owned_by_krubit IN (0, 1)),
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, platform, external_id),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS creator_bootstrap (
                guild_id INTEGER PRIMARY KEY,
                creator_role_id INTEGER NOT NULL,
                notification_channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS content_schedule (
                guild_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                next_poll_at TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
                consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                last_state TEXT NOT NULL DEFAULT 'unconfigured',
                last_detail TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id, platform),
                FOREIGN KEY (guild_id, account_id)
                    REFERENCES creator_accounts (guild_id, account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_content_schedule_next_poll
                ON content_schedule (next_poll_at ASC);

            CREATE TABLE IF NOT EXISTS entry_sniff_assessments (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                band TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                explanation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, member_id, joined_at)
            );

            CREATE INDEX IF NOT EXISTS idx_entry_sniff_assessments_guild_member_joined
                ON entry_sniff_assessments (guild_id, member_id, joined_at DESC);

            CREATE TABLE IF NOT EXISTS watch_windows (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                band TEXT NOT NULL,
                closed_at TEXT,
                close_reason TEXT,
                PRIMARY KEY (guild_id, member_id)
            );

            CREATE INDEX IF NOT EXISTS idx_watch_windows_guild_open
                ON watch_windows (guild_id, closed_at);

            CREATE TABLE IF NOT EXISTS incidents (
                guild_id INTEGER NOT NULL,
                incident_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                band TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                evidence_packet_id TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                acknowledged_by INTEGER,
                PRIMARY KEY (guild_id, incident_id)
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_guild_opened
                ON incidents (guild_id, opened_at DESC);

            CREATE TABLE IF NOT EXISTS guild_allow_block_lists (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                list_kind TEXT NOT NULL CHECK (list_kind IN ('allow', 'block')),
                reason TEXT NOT NULL,
                set_by INTEGER NOT NULL,
                set_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id)
            );

            CREATE TABLE IF NOT EXISTS sniff_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                member_id INTEGER,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sniff_receipts_guild_created
                ON sniff_receipts (guild_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS ledger_events (
                guild_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                member_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, event_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ledger_events_guild_member_occurred
                ON ledger_events (guild_id, member_id, occurred_at DESC);

            CREATE TABLE IF NOT EXISTS milestones (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                reached_at TEXT NOT NULL,
                detail TEXT NOT NULL,
                PRIMARY KEY (guild_id, member_id, kind, reached_at)
            );

            CREATE INDEX IF NOT EXISTS idx_milestones_guild_member
                ON milestones (guild_id, member_id, reached_at DESC);

            CREATE TABLE IF NOT EXISTS channel_exclusions (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                excluded_by INTEGER NOT NULL,
                reason TEXT NOT NULL,
                excluded_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS retention_policies (
                guild_id INTEGER PRIMARY KEY,
                max_age_days INTEGER NOT NULL CHECK (max_age_days > 0),
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                member_id INTEGER,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_activity_receipts_guild_created
                ON activity_receipts (guild_id, created_at DESC);
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
        await self._connection.execute(
            """
            UPDATE live_signal_deliveries
            SET status = 'cancelled'
            WHERE status = 'claimed'
                AND EXISTS (
                    SELECT 1
                    FROM live_signal_sessions
                    WHERE live_signal_sessions.guild_id = live_signal_deliveries.guild_id
                        AND live_signal_sessions.session_key = live_signal_deliveries.session_key
                        AND (
                            live_signal_sessions.status = ?
                            OR live_signal_sessions.ended_at IS NOT NULL
                        )
                )
            """,
            (LiveSignalStatus.ENDED.value,),
        )

    async def close(self) -> None:
        await self._connection.close()

    async def set_live_signal_config(self, config: LiveSignalConfig) -> None:
        """Persist the configured notification resources by their stable Discord IDs."""
        _require_guild_id(config.guild_id)
        async with self._write_transaction():
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

    async def get_live_signal_config(self, guild_id: int) -> LiveSignalConfig | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM live_signal_config WHERE guild_id = ?", (guild_id,)
        )
        return self._live_signal_config_from_row(await cursor.fetchone())

    async def save_live_session(self, session: LiveSignalSession) -> LiveSignalSession:
        """Insert or update a session, retaining its first durable key for a known stream."""
        _require_guild_id(session.guild_id)
        async with self._write_transaction(immediate=True):
            saved_session = await self._save_live_session_in_transaction(session)
        return await self._read_saved_live_session(saved_session)

    async def save_terminal_live_session(self, session: LiveSignalSession) -> LiveSignalSession:
        """Persist terminal session state and retire its claimed deliveries atomically."""
        _require_guild_id(session.guild_id)
        async with self._write_transaction(immediate=True):
            saved_session = await self._save_live_session_in_transaction(session)
            await self._connection.execute(
                """
                UPDATE live_signal_deliveries
                SET status = 'cancelled'
                WHERE guild_id = ? AND session_key = ? AND status = 'claimed'
                """,
                (saved_session.guild_id, saved_session.session_key),
            )
        return await self._read_saved_live_session(saved_session)

    async def _save_live_session_in_transaction(
        self, session: LiveSignalSession
    ) -> LiveSignalSession:
        saved_session = session
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
        return await self._read_saved_live_session(saved_session)

    async def _read_saved_live_session(self, saved_session: LiveSignalSession) -> LiveSignalSession:
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

    async def get_live_session(self, guild_id: int, session_key: str) -> LiveSignalSession | None:
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

    async def list_terminal_live_sessions_with_owned_roles(
        self, guild_id: int
    ) -> list[LiveSignalSession]:
        """Return ended sessions whose Krubit-owned role still needs cleanup."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM live_signal_sessions
            WHERE guild_id = ?
              AND status = ?
              AND ended_at IS NOT NULL
              AND role_assigned_by_krubit = 1
            ORDER BY ended_at ASC, session_key ASC
            """,
            (guild_id, LiveSignalStatus.ENDED.value),
        )
        return [self._live_signal_session_from_stored_row(row) for row in await cursor.fetchall()]

    async def list_member_live_sessions(
        self, guild_id: int, member_id: int
    ) -> list[LiveSignalSession]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM live_signal_sessions WHERE guild_id = ? AND member_id = ?",
            (guild_id, member_id),
        )
        return [self._live_signal_session_from_stored_row(row) for row in await cursor.fetchall()]

    async def claim_live_delivery(self, guild_id: int, delivery_key: str, session_key: str) -> bool:
        attempt = await self.claim_live_delivery_attempt(guild_id, delivery_key, session_key)
        return attempt is not None

    async def claim_live_delivery_attempt(
        self, guild_id: int, delivery_key: str, session_key: str
    ) -> int | None:
        """Claim a delivery and return its monotonically increasing attempt identity."""
        _require_guild_id(guild_id)
        now = datetime.now(UTC).isoformat()
        async with self._write_transaction(immediate=True):
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
            return attempt

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

    async def list_claimed_live_deliveries(self, guild_id: int) -> list[LiveSignalDelivery]:
        """Return only durable delivery claims that need runtime recovery."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, delivery_key, session_key, status, attempt, channel_id, message_id
            FROM live_signal_deliveries WHERE guild_id = ? AND status = 'claimed'
            """,
            (guild_id,),
        )
        deliveries = [self._live_signal_delivery_from_row(row) for row in await cursor.fetchall()]
        return [delivery for delivery in deliveries if delivery is not None]

    async def cancel_claimed_live_deliveries(self, guild_id: int, session_key: str) -> None:
        """Retire pending execution for a terminal session without deleting audit history."""
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                UPDATE live_signal_deliveries SET status = 'cancelled', updated_at = ?
                WHERE guild_id = ? AND session_key = ? AND status = 'claimed'
                """,
                (datetime.now(UTC).isoformat(), guild_id, session_key),
            )

    async def merge_live_delivery_identity(
        self,
        guild_id: int,
        provisional_key: str,
        stream_key: str,
        session_key: str,
    ) -> int | None:
        """Atomically carry a provisional claim forward once Helix supplies a stream ID."""
        _require_guild_id(guild_id)
        if not provisional_key or not stream_key or not session_key:
            raise ValueError("delivery and session keys must not be blank")
        async with self._write_transaction(immediate=True):
            fresh_attempt: int | None = None
            source_cursor = await self._connection.execute(
                """
                SELECT guild_id, delivery_key, session_key, status, attempt,
                       channel_id, message_id
                FROM live_signal_deliveries
                WHERE guild_id = ? AND delivery_key = ?
                """,
                (guild_id, provisional_key),
            )
            destination_cursor = await self._connection.execute(
                """
                SELECT guild_id, delivery_key, session_key, status, attempt,
                       channel_id, message_id
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
                attempt = max(source.attempt, destination.attempt)
                if retained.status == "claimed":
                    attempt += 1
                    fresh_attempt = attempt
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
                        attempt,
                        None if fresh_attempt is not None else retained.channel_id,
                        None if fresh_attempt is not None else retained.message_id,
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
            return fresh_attempt

    async def complete_live_delivery(
        self,
        guild_id: int,
        delivery_key: str,
        *,
        status: str,
        channel_id: int | None,
        message_id: int | None,
        attempt: int,
    ) -> bool:
        _require_guild_id(guild_id)
        if attempt <= 0:
            raise ValueError("attempt must be positive")
        query = """
            UPDATE live_signal_deliveries
            SET status = ?, channel_id = ?, message_id = ?, updated_at = ?
            WHERE guild_id = ? AND delivery_key = ? AND status = 'claimed' AND attempt = ?
        """
        params: tuple[object, ...] = (
            status,
            channel_id,
            message_id,
            datetime.now(UTC).isoformat(),
            guild_id,
            delivery_key,
            attempt,
        )
        async with self._write_transaction():
            cursor = await self._connection.execute(query, params)
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
        async with self._write_transaction():
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
        async with self._write_transaction():
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

    async def guild_is_enabled(self, guild_id: int) -> bool:
        cursor = await self._connection.execute(
            "SELECT enabled FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return bool(row["enabled"]) if row is not None else False

    async def accept_event(self, event: GuildEvent) -> bool:
        payload = redact(dict(event.payload))
        async with self._write_transaction():
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
        async with self._write_transaction():
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
        async with self._write_transaction():
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
        async with self._write_transaction():
            cursor = await self._connection.execute(
                """
                INSERT OR IGNORE INTO daily_summaries
                    (guild_id, summary_date, status, channel_id, created_at)
                VALUES (?, ?, 'claimed', NULL, ?)
                """,
                (guild_id, summary_date.isoformat(), datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    async def set_daily_summary_status(
        self,
        guild_id: int,
        summary_date: date,
        status: str,
        channel_id: int | None,
    ) -> None:
        async with self._write_transaction():
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

    async def daily_summary_status(self, guild_id: int, summary_date: date) -> str | None:
        cursor = await self._connection.execute(
            "SELECT status FROM daily_summaries WHERE guild_id = ? AND summary_date = ?",
            (guild_id, summary_date.isoformat()),
        )
        row = await cursor.fetchone()
        return str(row["status"]) if row is not None else None

    async def save_creator_account(self, account: CreatorAccount) -> CreatorAccount:
        """Insert or update a creator account, guarding against a silent owner change.

        A platform identity (`account_id`) may exist at most once per guild. Saving an
        account whose `account_id` is already registered to a different owner in this
        guild raises `ValueError`; changing the owner requires `transfer_creator_account`.
        """
        _require_guild_id(account.guild_id)
        async with self._write_transaction(immediate=True):
            cursor = await self._connection.execute(
                """
                SELECT owner_member_id FROM creator_accounts
                WHERE guild_id = ? AND account_id = ?
                """,
                (account.guild_id, account.account_id),
            )
            existing = await cursor.fetchone()
            if existing is not None and int(existing["owner_member_id"]) != account.owner_member_id:
                raise ValueError(
                    f"{account.platform.value} account {account.handle!r} is already "
                    "registered to a different owner in this guild"
                )
            await self._connection.execute(
                """
                INSERT INTO creator_profiles (guild_id, owner_member_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, owner_member_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    account.guild_id,
                    account.owner_member_id,
                    account.updated_at.isoformat(),
                    account.updated_at.isoformat(),
                ),
            )
            await self._connection.execute(
                """
                INSERT INTO creator_accounts (
                    guild_id, account_id, owner_member_id, platform, handle, canonical_url,
                    external_id, paused, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id) DO UPDATE SET
                    owner_member_id = excluded.owner_member_id,
                    platform = excluded.platform,
                    handle = excluded.handle,
                    canonical_url = excluded.canonical_url,
                    external_id = excluded.external_id,
                    paused = excluded.paused,
                    updated_at = excluded.updated_at
                """,
                (
                    account.guild_id,
                    account.account_id,
                    account.owner_member_id,
                    account.platform.value,
                    account.handle,
                    account.canonical_url,
                    account.external_id,
                    int(account.paused),
                    account.created_at.isoformat(),
                    account.updated_at.isoformat(),
                ),
            )
        saved = await self.get_creator_account(account.guild_id, account.account_id)
        if saved is None:
            raise RuntimeError("saved creator account could not be read")
        return saved

    async def save_migrated_creator_account(
        self, account: CreatorAccount
    ) -> CreatorAccount | None:
        """Idempotently link one Phase 2A-derived creator account without ever
        clobbering an operator's later decisions about it.

        Used only by `krubit.services.live_signals.migrate_twitch_content` (and, via
        `_link_twitch_content`, the live Twitch delivery-result path it shares). Unlike
        `save_creator_account`:

        - An owner conflict (the `(guild_id, account_id)` row already belongs to a
          different member — for example after a legitimate `/fetch creator transfer`)
          never raises. It returns `None` so the caller can skip and log this one
          session rather than crash the whole migration (and, upstream, `krubit run`'s
          entire boot sequence).
        - `paused` is never overwritten on an existing row. The migration's own
          `paused=True` only applies the first time this identity is created; a
          subsequent `/fetch creator resume` an operator has already applied survives
          every later boot's migration replay untouched.
        """
        _require_guild_id(account.guild_id)
        async with self._write_transaction(immediate=True):
            cursor = await self._connection.execute(
                """
                SELECT owner_member_id FROM creator_accounts
                WHERE guild_id = ? AND account_id = ?
                """,
                (account.guild_id, account.account_id),
            )
            existing = await cursor.fetchone()
            if existing is not None and int(existing["owner_member_id"]) != account.owner_member_id:
                return None
            await self._connection.execute(
                """
                INSERT INTO creator_profiles (guild_id, owner_member_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, owner_member_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    account.guild_id,
                    account.owner_member_id,
                    account.updated_at.isoformat(),
                    account.updated_at.isoformat(),
                ),
            )
            await self._connection.execute(
                """
                INSERT INTO creator_accounts (
                    guild_id, account_id, owner_member_id, platform, handle, canonical_url,
                    external_id, paused, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id) DO UPDATE SET
                    handle = excluded.handle,
                    canonical_url = excluded.canonical_url,
                    external_id = excluded.external_id,
                    updated_at = excluded.updated_at
                """,
                (
                    account.guild_id,
                    account.account_id,
                    account.owner_member_id,
                    account.platform.value,
                    account.handle,
                    account.canonical_url,
                    account.external_id,
                    int(account.paused),
                    account.created_at.isoformat(),
                    account.updated_at.isoformat(),
                ),
            )
        return await self.get_creator_account(account.guild_id, account.account_id)

    async def transfer_creator_account(
        self, guild_id: int, account_id: str, new_owner_member_id: int, now: datetime
    ) -> CreatorAccount:
        """Reassign an existing account's owner.

        The only path that may change `owner_member_id` for an existing account, kept
        separate from `save_creator_account` so ownership changes are always an explicit,
        auditable action rather than a side effect of an ordinary upsert.
        """
        _require_guild_id(guild_id)
        if new_owner_member_id <= 0:
            raise ValueError("new_owner_member_id must be positive")
        async with self._write_transaction(immediate=True):
            cursor = await self._connection.execute(
                "SELECT 1 FROM creator_accounts WHERE guild_id = ? AND account_id = ?",
                (guild_id, account_id),
            )
            if await cursor.fetchone() is None:
                raise ValueError(f"creator account {account_id!r} was not found in this guild")
            await self._connection.execute(
                """
                INSERT INTO creator_profiles (guild_id, owner_member_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, owner_member_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (guild_id, new_owner_member_id, now.isoformat(), now.isoformat()),
            )
            await self._connection.execute(
                """
                UPDATE creator_accounts SET owner_member_id = ?, updated_at = ?
                WHERE guild_id = ? AND account_id = ?
                """,
                (new_owner_member_id, now.isoformat(), guild_id, account_id),
            )
        saved = await self.get_creator_account(guild_id, account_id)
        if saved is None:
            raise RuntimeError("transferred creator account could not be read")
        return saved

    async def get_creator_account(self, guild_id: int, account_id: str) -> CreatorAccount | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM creator_accounts WHERE guild_id = ? AND account_id = ?",
            (guild_id, account_id),
        )
        return creator_account_from_row(await cursor.fetchone())

    async def list_creator_accounts(self, guild_id: int) -> list[CreatorAccount]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM creator_accounts WHERE guild_id = ?
            ORDER BY created_at ASC, account_id ASC
            """,
            (guild_id,),
        )
        accounts = [creator_account_from_row(row) for row in await cursor.fetchall()]
        return [account for account in accounts if account is not None]

    async def list_creator_accounts_for_owner(
        self, guild_id: int, owner_member_id: int
    ) -> list[CreatorAccount]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM creator_accounts
            WHERE guild_id = ? AND owner_member_id = ?
            ORDER BY created_at ASC, account_id ASC
            """,
            (guild_id, owner_member_id),
        )
        accounts = [creator_account_from_row(row) for row in await cursor.fetchall()]
        return [account for account in accounts if account is not None]

    async def get_creator_profile(
        self, guild_id: int, owner_member_id: int
    ) -> CreatorProfile | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM creator_profiles WHERE guild_id = ? AND owner_member_id = ?",
            (guild_id, owner_member_id),
        )
        return creator_profile_from_row(await cursor.fetchone())

    async def save_creator_route(self, route: CreatorRoute) -> CreatorRoute:
        _require_guild_id(route.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO creator_routes (
                    guild_id, account_id, content_kind, channel_id, mention_role_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id, content_kind) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    mention_role_id = excluded.mention_role_id,
                    updated_at = excluded.updated_at
                """,
                (
                    route.guild_id,
                    route.account_id,
                    route.content_kind.value,
                    route.channel_id,
                    route.mention_role_id,
                    route.updated_at.isoformat(),
                ),
            )
        cursor = await self._connection.execute(
            """
            SELECT * FROM creator_routes
            WHERE guild_id = ? AND account_id = ? AND content_kind = ?
            """,
            (route.guild_id, route.account_id, route.content_kind.value),
        )
        saved = creator_route_from_row(await cursor.fetchone())
        if saved is None:
            raise RuntimeError("saved creator route could not be read")
        return saved

    async def list_creator_routes(self, guild_id: int, account_id: str) -> list[CreatorRoute]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM creator_routes
            WHERE guild_id = ? AND account_id = ?
            ORDER BY content_kind ASC
            """,
            (guild_id, account_id),
        )
        routes = [creator_route_from_row(row) for row in await cursor.fetchall()]
        return [route for route in routes if route is not None]

    async def record_creator_registry_receipt(
        self,
        *,
        guild_id: int,
        receipt_id: str,
        account_id: str | None,
        action: str,
        actor_member_id: int,
        detail: dict[str, JSONValue],
        created_at: datetime,
    ) -> None:
        """Record a redacted audit receipt for a creator registry authority decision."""
        _require_guild_id(guild_id)
        safe_detail = _json_object(redact(detail))
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO creator_registry_receipts (
                    guild_id, receipt_id, account_id, action, actor_member_id, detail_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt_id,
                    account_id,
                    action,
                    actor_member_id,
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                    created_at.isoformat(),
                ),
            )

    async def list_creator_registry_receipts(
        self, guild_id: int, account_id: str | None = None, limit: int = 50
    ) -> list[CreatorRegistryReceipt]:
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if account_id is None:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, account_id, action, actor_member_id, detail_json,
                       created_at
                FROM creator_registry_receipts
                WHERE guild_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, limit),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, account_id, action, actor_member_id, detail_json,
                       created_at
                FROM creator_registry_receipts
                WHERE guild_id = ? AND account_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, account_id, limit),
            )
        rows = await cursor.fetchall()
        return [self._creator_registry_receipt_from_row(row) for row in rows]

    @staticmethod
    def _creator_registry_receipt_from_row(row: aiosqlite.Row) -> CreatorRegistryReceipt:
        stored_account_id = row["account_id"]
        return CreatorRegistryReceipt(
            guild_id=int(row["guild_id"]),
            receipt_id=str(row["receipt_id"]),
            account_id=str(stored_account_id) if stored_account_id is not None else None,
            action=str(row["action"]),
            actor_member_id=int(row["actor_member_id"]),
            detail=_json_object(json.loads(str(row["detail_json"]))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    async def get_content_cursor(self, guild_id: int, account_id: str) -> ContentCursor | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM content_cursors WHERE guild_id = ? AND account_id = ?",
            (guild_id, account_id),
        )
        return content_cursor_from_row(await cursor.fetchone())

    async def get_content_event(
        self, guild_id: int, platform: Platform, external_id: str
    ) -> ContentEvent | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_events
            WHERE guild_id = ? AND platform = ? AND external_id = ?
            """,
            (guild_id, platform.value, external_id),
        )
        return content_event_from_row(await cursor.fetchone())

    async def get_content_delivery(
        self, guild_id: int, platform: Platform, external_id: str
    ) -> ContentDelivery | None:
        """Return the most recently claimed delivery for one content item, if any.

        A content item can accumulate more than one delivery over its lifetime — one
        per genuine publish/live transition (see `record_content_observations`) — so
        this returns the highest `transition_seq` row. Use `list_content_deliveries`
        for the full claim history.
        """
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_deliveries
            WHERE guild_id = ? AND platform = ? AND external_id = ?
            ORDER BY transition_seq DESC
            LIMIT 1
            """,
            (guild_id, platform.value, external_id),
        )
        return content_delivery_from_row(await cursor.fetchone())

    async def get_content_delivery_by_seq(
        self, guild_id: int, platform: Platform, external_id: str, transition_seq: int
    ) -> ContentDelivery | None:
        """Return the exact delivery row for one claimed transition, if any.

        Unlike `get_content_delivery` (highest `transition_seq`), this fetches a
        specific, already-known transition — what `ContentRuntime` needs to act on the
        exact delivery a `ContentPlan` or a staff-supplied `delivery_id` names.
        """
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_deliveries
            WHERE guild_id = ? AND platform = ? AND external_id = ? AND transition_seq = ?
            """,
            (guild_id, platform.value, external_id, transition_seq),
        )
        return content_delivery_from_row(await cursor.fetchone())

    async def update_content_delivery(
        self,
        *,
        guild_id: int,
        platform: Platform,
        external_id: str,
        transition_seq: int,
        status: str,
        attempt: int,
        channel_id: int | None,
        message_id: int | None,
        now: datetime,
    ) -> ContentDelivery:
        """Record the outcome of one Discord delivery attempt for a claimed transition.

        Used by `ContentRuntime` after every send/edit/retry/retract attempt. Also
        appends a durable `content_delivery_attempts` audit row for this `attempt`
        number (ignored if that attempt was already recorded), matching the audit
        trail `record_content_observations` starts at attempt 1.
        """
        _require_guild_id(guild_id)
        if status not in _CONTENT_DELIVERY_STATUSES:
            raise ValueError(f"status must be one of {sorted(_CONTENT_DELIVERY_STATUSES)}")
        if attempt <= 0:
            raise ValueError("attempt must be positive")
        async with self._write_transaction():
            await self._connection.execute(
                """
                UPDATE content_deliveries
                SET status = ?, attempt = ?, discord_channel_id = ?, discord_message_id = ?,
                    updated_at = ?
                WHERE guild_id = ? AND platform = ? AND external_id = ? AND transition_seq = ?
                """,
                (
                    status,
                    attempt,
                    channel_id,
                    message_id,
                    now.isoformat(),
                    guild_id,
                    platform.value,
                    external_id,
                    transition_seq,
                ),
            )
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO content_delivery_attempts (
                    guild_id, platform, external_id, transition_seq, attempt, status,
                    detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    guild_id,
                    platform.value,
                    external_id,
                    transition_seq,
                    attempt,
                    status,
                    now.isoformat(),
                ),
            )
        updated = await self.get_content_delivery_by_seq(
            guild_id, platform, external_id, transition_seq
        )
        if updated is None:
            raise RuntimeError("content delivery could not be read back after update")
        return updated

    async def list_content_deliveries(
        self, guild_id: int, platform: Platform, external_id: str
    ) -> list[ContentDelivery]:
        """Return every delivery claimed for one content item, oldest transition first."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_deliveries
            WHERE guild_id = ? AND platform = ? AND external_id = ?
            ORDER BY transition_seq ASC
            """,
            (guild_id, platform.value, external_id),
        )
        deliveries = [content_delivery_from_row(row) for row in await cursor.fetchall()]
        return [delivery for delivery in deliveries if delivery is not None]

    async def list_pending_content_deliveries(self, guild_id: int) -> list[ContentDelivery]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_deliveries
            WHERE guild_id = ? AND status = 'pending'
            ORDER BY created_at ASC, platform ASC, external_id ASC
            """,
            (guild_id,),
        )
        deliveries = [content_delivery_from_row(row) for row in await cursor.fetchall()]
        return [delivery for delivery in deliveries if delivery is not None]

    async def record_content_observations(
        self,
        *,
        guild_id: int,
        account_id: str,
        platform: Platform,
        observations: tuple[ContentObservation, ...],
        cursor_value: str | None,
        now: datetime,
    ) -> tuple[ContentCursor, tuple[ContentPlan, ...]]:
        """Atomically upsert one connector page's items and claim new deliveries.

        The first successful call for `(guild_id, account_id)` is the baseline page:
        every observation is stored as an identity but nothing is ever claimed. Every
        later call upserts each observation's lifecycle state and claims exactly one
        fresh delivery per item that newly reaches `PUBLISHED` or `LIVE` (that is, its
        previously stored state — if any — had not already reached one of those
        states). A content item can be claimed more than once over its lifetime — for
        example a stream that goes `live`, `ended`, then `live` again claims a second,
        independent delivery — each claim gets the next `transition_seq` for that
        `(guild_id, platform, external_id)`. The whole page is one write transaction,
        so concurrent or duplicate calls observing the *same* transition can never
        double-claim it: the delivery claim is an `INSERT OR IGNORE` against
        `content_deliveries`'s `(guild_id, platform, external_id, transition_seq)`
        primary key.
        """
        _require_guild_id(guild_id)
        async with self._write_transaction(immediate=True):
            cursor_row_cursor = await self._connection.execute(
                "SELECT baselined_at FROM content_cursors WHERE guild_id = ? AND account_id = ?",
                (guild_id, account_id),
            )
            cursor_row = await cursor_row_cursor.fetchone()
            is_baseline_page = cursor_row is None
            existing_baselined_at = (
                str(cursor_row["baselined_at"])
                if cursor_row is not None and cursor_row["baselined_at"] is not None
                else None
            )
            plans: list[ContentPlan] = []
            for observation in observations:
                event_row_cursor = await self._connection.execute(
                    """
                    SELECT state, first_observed_at FROM content_events
                    WHERE guild_id = ? AND platform = ? AND external_id = ?
                    """,
                    (guild_id, platform.value, observation.external_id),
                )
                event_row = await event_row_cursor.fetchone()
                previously_reached = event_row is not None and reaches_publish_or_live(
                    ContentState(str(event_row["state"]))
                )
                first_observed_at = (
                    str(event_row["first_observed_at"])
                    if event_row is not None
                    else now.isoformat()
                )
                await self._connection.execute(
                    """
                    INSERT INTO content_events (
                        guild_id, account_id, platform, external_id, content_kind, state,
                        canonical_url, title, published_at, first_observed_at, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, platform, external_id) DO UPDATE SET
                        account_id = excluded.account_id,
                        content_kind = excluded.content_kind,
                        state = excluded.state,
                        canonical_url = excluded.canonical_url,
                        title = excluded.title,
                        published_at = excluded.published_at,
                        last_observed_at = excluded.last_observed_at
                    """,
                    (
                        guild_id,
                        account_id,
                        platform.value,
                        observation.external_id,
                        observation.content_kind.value,
                        observation.state.value,
                        observation.canonical_url,
                        observation.title,
                        _stored_timestamp(observation.published_at),
                        first_observed_at,
                        now.isoformat(),
                    ),
                )
                if (
                    not is_baseline_page
                    and reaches_publish_or_live(observation.state)
                    and not previously_reached
                ):
                    next_seq_cursor = await self._connection.execute(
                        """
                        SELECT COALESCE(MAX(transition_seq), 0) + 1 AS next_seq
                        FROM content_deliveries
                        WHERE guild_id = ? AND platform = ? AND external_id = ?
                        """,
                        (guild_id, platform.value, observation.external_id),
                    )
                    next_seq_row = await next_seq_cursor.fetchone()
                    if next_seq_row is None:
                        raise RuntimeError("next transition_seq query returned no row")
                    next_seq = int(next_seq_row["next_seq"])
                    claim_cursor = await self._connection.execute(
                        """
                        INSERT OR IGNORE INTO content_deliveries (
                            guild_id, platform, external_id, transition_seq, account_id,
                            status, attempt, discord_channel_id, discord_message_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', 1, NULL, NULL, ?, ?)
                        """,
                        (
                            guild_id,
                            platform.value,
                            observation.external_id,
                            next_seq,
                            account_id,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    if claim_cursor.rowcount == 1:
                        await self._connection.execute(
                            """
                            INSERT INTO content_delivery_attempts (
                                guild_id, platform, external_id, transition_seq, attempt,
                                status, detail_json, created_at
                            ) VALUES (?, ?, ?, ?, 1, 'pending', '{}', ?)
                            """,
                            (
                                guild_id,
                                platform.value,
                                observation.external_id,
                                next_seq,
                                now.isoformat(),
                            ),
                        )
                        claimed_event = await self.get_content_event(
                            guild_id, platform, observation.external_id
                        )
                        claimed_delivery_cursor = await self._connection.execute(
                            """
                            SELECT * FROM content_deliveries
                            WHERE guild_id = ? AND platform = ? AND external_id = ?
                                AND transition_seq = ?
                            """,
                            (guild_id, platform.value, observation.external_id, next_seq),
                        )
                        claimed_delivery = content_delivery_from_row(
                            await claimed_delivery_cursor.fetchone()
                        )
                        if claimed_event is None or claimed_delivery is None:
                            raise RuntimeError("claimed content delivery could not be read back")
                        plans.append(ContentPlan(event=claimed_event, delivery=claimed_delivery))
            resolved_baselined_at = existing_baselined_at or now.isoformat()
            await self._connection.execute(
                """
                INSERT INTO content_cursors (
                    guild_id, account_id, platform, cursor_value, baselined_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id) DO UPDATE SET
                    platform = excluded.platform,
                    cursor_value = excluded.cursor_value,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    account_id,
                    platform.value,
                    cursor_value,
                    resolved_baselined_at,
                    now.isoformat(),
                ),
            )
        saved_cursor = await self.get_content_cursor(guild_id, account_id)
        if saved_cursor is None:
            raise RuntimeError("content cursor could not be read back")
        return saved_cursor, tuple(plans)

    async def record_content_receipt(
        self,
        *,
        guild_id: int,
        receipt_id: str,
        account_id: str | None,
        platform: Platform | None,
        external_id: str | None,
        action: str,
        detail: dict[str, JSONValue],
        created_at: datetime,
    ) -> None:
        """Record a redacted diagnostic receipt for a skipped or malformed content item."""
        _require_guild_id(guild_id)
        safe_detail = _json_object(redact(detail))
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO content_receipts (
                    guild_id, receipt_id, account_id, platform, external_id, action,
                    detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt_id,
                    account_id,
                    platform.value if platform is not None else None,
                    external_id,
                    action,
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                    created_at.isoformat(),
                ),
            )

    async def list_content_receipts(
        self, guild_id: int, account_id: str | None = None, limit: int = 50
    ) -> list[ContentReceipt]:
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if account_id is None:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, account_id, platform, external_id, action,
                       detail_json, created_at
                FROM content_receipts
                WHERE guild_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, limit),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, account_id, platform, external_id, action,
                       detail_json, created_at
                FROM content_receipts
                WHERE guild_id = ? AND account_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, account_id, limit),
            )
        rows = await cursor.fetchall()
        return [self._content_receipt_from_row(row) for row in rows]

    @staticmethod
    def _content_receipt_from_row(row: aiosqlite.Row) -> ContentReceipt:
        stored_account_id = row["account_id"]
        stored_platform = row["platform"]
        stored_external_id = row["external_id"]
        return ContentReceipt(
            guild_id=int(row["guild_id"]),
            receipt_id=str(row["receipt_id"]),
            account_id=str(stored_account_id) if stored_account_id is not None else None,
            platform=Platform(str(stored_platform)) if stored_platform is not None else None,
            external_id=str(stored_external_id) if stored_external_id is not None else None,
            action=str(row["action"]),
            detail=_json_object(json.loads(str(row["detail_json"]))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    async def claim_mention_budget(
        self,
        *,
        guild_id: int,
        budget_kind: str,
        period_key: str,
        limit: int,
        now: datetime,
    ) -> bool:
        """Atomically consume one unit of a mention budget.

        Returns `True` if this call claimed the unit, `False` if the budget was already
        exhausted. The read-modify-write happens inside one immediate write transaction,
        so concurrent callers racing for the last unit of a budget can never all
        believe they won it — see `test_claim_mention_budget_is_atomic_under_concurrency`.
        """
        _require_guild_id(guild_id)
        if limit <= 0:
            return False
        async with self._write_transaction(immediate=True):
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO mention_budget_state (
                    guild_id, budget_kind, period_key, consumed, updated_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (guild_id, budget_kind, period_key, now.isoformat()),
            )
            cursor = await self._connection.execute(
                """
                UPDATE mention_budget_state
                SET consumed = consumed + 1, updated_at = ?
                WHERE guild_id = ? AND budget_kind = ? AND period_key = ? AND consumed < ?
                """,
                (now.isoformat(), guild_id, budget_kind, period_key, limit),
            )
            return cursor.rowcount == 1

    async def mention_budget_consumed(
        self, guild_id: int, budget_kind: str, period_key: str
    ) -> int:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT consumed FROM mention_budget_state
            WHERE guild_id = ? AND budget_kind = ? AND period_key = ?
            """,
            (guild_id, budget_kind, period_key),
        )
        row = await cursor.fetchone()
        return int(row["consumed"]) if row is not None else 0

    async def record_mention_receipt(
        self,
        *,
        guild_id: int,
        receipt_id: str,
        budget_kind: str,
        period_key: str,
        outcome: str,
        platform: Platform | None,
        external_id: str | None,
        created_at: datetime,
    ) -> None:
        """Record a redacted audit receipt for one mention-budget outcome."""
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO mention_budget_receipts (
                    guild_id, receipt_id, budget_kind, period_key, outcome, platform,
                    external_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt_id,
                    budget_kind,
                    period_key,
                    outcome,
                    platform.value if platform is not None else None,
                    external_id,
                    created_at.isoformat(),
                ),
            )

    async def list_mention_budget_receipts(
        self, guild_id: int, limit: int = 50
    ) -> list[MentionBudgetReceipt]:
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT guild_id, receipt_id, budget_kind, period_key, outcome, platform,
                   external_id, created_at
            FROM mention_budget_receipts
            WHERE guild_id = ?
            ORDER BY created_at DESC, receipt_id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._mention_budget_receipt_from_row(row) for row in rows]

    @staticmethod
    def _mention_budget_receipt_from_row(row: aiosqlite.Row) -> MentionBudgetReceipt:
        stored_platform = row["platform"]
        stored_external_id = row["external_id"]
        return MentionBudgetReceipt(
            guild_id=int(row["guild_id"]),
            receipt_id=str(row["receipt_id"]),
            budget_kind=str(row["budget_kind"]),
            period_key=str(row["period_key"]),
            outcome=str(row["outcome"]),
            platform=Platform(str(stored_platform)) if stored_platform is not None else None,
            external_id=str(stored_external_id) if stored_external_id is not None else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    async def record_content_correlation(
        self,
        *,
        guild_id: int,
        platform: Platform,
        external_id: str,
        correlation_group: str,
        reason: str,
        created_at: datetime,
    ) -> None:
        """Persist which correlation group one content item was merged into, if any."""
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO content_correlations (
                    guild_id, platform, external_id, correlation_group, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, platform, external_id) DO UPDATE SET
                    correlation_group = excluded.correlation_group,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (
                    guild_id,
                    platform.value,
                    external_id,
                    correlation_group,
                    reason,
                    created_at.isoformat(),
                ),
            )

    async def get_content_correlation_group(
        self, guild_id: int, platform: Platform, external_id: str
    ) -> str | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT correlation_group FROM content_correlations
            WHERE guild_id = ? AND platform = ? AND external_id = ?
            """,
            (guild_id, platform.value, external_id),
        )
        row = await cursor.fetchone()
        return str(row["correlation_group"]) if row is not None else None

    async def list_content_correlation_members(
        self, guild_id: int, correlation_group: str
    ) -> list[tuple[Platform, str]]:
        """Return every `(platform, external_id)` merged into `correlation_group`."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT platform, external_id FROM content_correlations
            WHERE guild_id = ? AND correlation_group = ?
            ORDER BY platform ASC, external_id ASC
            """,
            (guild_id, correlation_group),
        )
        rows = await cursor.fetchall()
        return [(Platform(str(row["platform"])), str(row["external_id"])) for row in rows]

    async def save_scheduled_event_mapping(
        self, mapping: ScheduledEventMapping
    ) -> ScheduledEventMapping:
        """Insert or overwrite one exact-ID Scheduled Event mapping row.

        Used both by `ScheduledEventSynchronizer` (Task 11) after every create/update
        and directly by tests/staff tooling to seed a mapping's `owned_by_krubit`
        state ahead of a sync attempt.
        """
        _require_guild_id(mapping.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO scheduled_event_mappings (
                    guild_id, platform, external_id, account_id, discord_event_id,
                    discord_status, owned_by_krubit, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, platform, external_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    discord_event_id = excluded.discord_event_id,
                    discord_status = excluded.discord_status,
                    owned_by_krubit = excluded.owned_by_krubit,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    mapping.guild_id,
                    mapping.platform.value,
                    mapping.external_id,
                    mapping.account_id,
                    mapping.discord_event_id,
                    mapping.discord_status,
                    int(mapping.owned_by_krubit),
                    mapping.content_hash,
                    mapping.created_at.isoformat(),
                    mapping.updated_at.isoformat(),
                ),
            )
        saved = await self.get_scheduled_event_mapping(
            mapping.guild_id, mapping.platform, mapping.external_id
        )
        if saved is None:
            raise RuntimeError("saved scheduled event mapping could not be read")
        return saved

    async def get_scheduled_event_mapping(
        self, guild_id: int, platform: Platform, external_id: str
    ) -> ScheduledEventMapping | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM scheduled_event_mappings
            WHERE guild_id = ? AND platform = ? AND external_id = ?
            """,
            (guild_id, platform.value, external_id),
        )
        return self._scheduled_event_mapping_from_row(await cursor.fetchone())

    async def list_owned_scheduled_event_mappings(
        self, guild_id: int
    ) -> list[ScheduledEventMapping]:
        """Return every Krubit-owned mapping for restart-time reconciliation sweeps."""
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM scheduled_event_mappings
            WHERE guild_id = ? AND owned_by_krubit = 1
            ORDER BY created_at ASC, platform ASC, external_id ASC
            """,
            (guild_id,),
        )
        mappings = [self._scheduled_event_mapping_from_row(row) for row in await cursor.fetchall()]
        return [mapping for mapping in mappings if mapping is not None]

    @staticmethod
    def _scheduled_event_mapping_from_row(
        row: aiosqlite.Row | None,
    ) -> ScheduledEventMapping | None:
        if row is None:
            return None
        return ScheduledEventMapping(
            guild_id=int(row["guild_id"]),
            account_id=str(row["account_id"]),
            platform=Platform(str(row["platform"])),
            external_id=str(row["external_id"]),
            discord_event_id=_optional_int(row["discord_event_id"]),
            discord_status=str(row["discord_status"]),
            owned_by_krubit=bool(row["owned_by_krubit"]),
            content_hash=str(row["content_hash"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    async def list_recent_content_events(
        self, guild_id: int, account_id: str, *, limit: int = 10
    ) -> list[ContentEvent]:
        """The account's most recently observed content items, newest first."""
        _require_guild_id(guild_id)
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_events
            WHERE guild_id = ? AND account_id = ?
            ORDER BY last_observed_at DESC
            LIMIT ?
            """,
            (guild_id, account_id, limit),
        )
        events = [content_event_from_row(row) for row in await cursor.fetchall()]
        return [event for event in events if event is not None]

    async def list_content_deliveries_for_account(
        self, guild_id: int, account_id: str
    ) -> list[ContentDelivery]:
        """Every delivery ever claimed for one creator account, oldest first.

        Unlike `list_content_deliveries` (one content item's claim history), this spans
        every content item the account has ever published/gone live for — the basis for
        `CreatorAnalyticsService`'s factual delivery counts and latency.
        """
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_deliveries
            WHERE guild_id = ? AND account_id = ?
            ORDER BY created_at ASC, platform ASC, external_id ASC, transition_seq ASC
            """,
            (guild_id, account_id),
        )
        deliveries = [content_delivery_from_row(row) for row in await cursor.fetchall()]
        return [delivery for delivery in deliveries if delivery is not None]

    async def save_creator_bootstrap(
        self,
        *,
        guild_id: int,
        creator_role_id: int,
        notification_channel_id: int,
        now: datetime,
    ) -> CreatorBootstrap:
        """Persist a guild's once-resolved creator role/channel IDs.

        Resolution by exact Discord name happens exactly once per guild; every later
        call reuses the stored IDs (see `get_creator_bootstrap`) rather than searching
        again, so a same-named role or channel created later never silently swaps in.
        """
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO creator_bootstrap (
                    guild_id, creator_role_id, notification_channel_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    creator_role_id = excluded.creator_role_id,
                    notification_channel_id = excluded.notification_channel_id,
                    updated_at = excluded.updated_at
                """,
                (
                    guild_id,
                    creator_role_id,
                    notification_channel_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        saved = await self.get_creator_bootstrap(guild_id)
        if saved is None:
            raise RuntimeError("saved creator bootstrap could not be read")
        return saved

    async def get_creator_bootstrap(self, guild_id: int) -> CreatorBootstrap | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT * FROM creator_bootstrap WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CreatorBootstrap(
            guild_id=int(row["guild_id"]),
            creator_role_id=int(row["creator_role_id"]),
            notification_channel_id=int(row["notification_channel_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    async def get_content_schedule(
        self, guild_id: int, account_id: str, platform: Platform
    ) -> ContentScheduleState | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM content_schedule
            WHERE guild_id = ? AND account_id = ? AND platform = ?
            """,
            (guild_id, account_id, platform.value),
        )
        return self._content_schedule_from_row(await cursor.fetchone())

    async def save_content_schedule(self, state: ContentScheduleState) -> ContentScheduleState:
        """Persist one account's durable next-poll bookkeeping.

        `ConnectorScheduler.run_cycle` calls this after every poll attempt (success or
        failure) so a restart's fresh scheduler instance reads back exactly the backoff
        and `next_poll_at` the previous process computed, never polling early just
        because in-memory scheduler state was lost.
        """
        _require_guild_id(state.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO content_schedule (
                    guild_id, account_id, platform, next_poll_at, interval_seconds,
                    consecutive_failures, last_state, last_detail, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, account_id, platform) DO UPDATE SET
                    next_poll_at = excluded.next_poll_at,
                    interval_seconds = excluded.interval_seconds,
                    consecutive_failures = excluded.consecutive_failures,
                    last_state = excluded.last_state,
                    last_detail = excluded.last_detail,
                    updated_at = excluded.updated_at
                """,
                (
                    state.guild_id,
                    state.account_id,
                    state.platform.value,
                    state.next_poll_at.isoformat(),
                    state.interval_seconds,
                    state.consecutive_failures,
                    state.last_state.value,
                    state.last_detail,
                    state.updated_at.isoformat(),
                ),
            )
        saved = await self.get_content_schedule(state.guild_id, state.account_id, state.platform)
        if saved is None:
            raise RuntimeError("saved content schedule could not be read")
        return saved

    @staticmethod
    def _content_schedule_from_row(row: aiosqlite.Row | None) -> ContentScheduleState | None:
        if row is None:
            return None
        last_detail = row["last_detail"]
        return ContentScheduleState(
            guild_id=int(row["guild_id"]),
            account_id=str(row["account_id"]),
            platform=Platform(str(row["platform"])),
            next_poll_at=datetime.fromisoformat(str(row["next_poll_at"])),
            interval_seconds=int(row["interval_seconds"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_state=CapabilityState(str(row["last_state"])),
            last_detail=str(last_detail) if last_detail is not None else None,
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    async def list_live_signal_guild_ids(self) -> list[int]:
        """Return every guild with a configured Phase 2A live-signal channel/role.

        Startup migration (`krubit.services.live_signals.migrate_all_twitch_content`)
        uses this to discover which guilds might have Phase 2A history to link, without
        requiring a live Discord connection or guild membership list.
        """
        cursor = await self._connection.execute(
            "SELECT guild_id FROM live_signal_config ORDER BY guild_id ASC"
        )
        return [int(row["guild_id"]) for row in await cursor.fetchall()]

    async def list_live_sessions_with_stream(self, guild_id: int) -> list[LiveSignalSession]:
        """Return every session (any status, terminal included) with a known stream_id.

        Migration's source of truth: only a session that reached a real Twitch stream
        identity ever had a chance to claim and deliver an announcement, so this is the
        complete candidate set `migrate_twitch_content` walks. Unlike
        `list_active_live_sessions`, terminal (ended) sessions are included — Phase 2A
        history to link is exactly the kind of session that has since ended.
        """
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT * FROM live_signal_sessions
            WHERE guild_id = ? AND stream_id IS NOT NULL
            ORDER BY detected_at ASC, session_key ASC
            """,
            (guild_id,),
        )
        return [self._live_signal_session_from_stored_row(row) for row in await cursor.fetchall()]

    async def migrate_content_identity(
        self,
        *,
        guild_id: int,
        account_id: str,
        platform: Platform,
        external_id: str,
        content_kind: ContentKind,
        state: ContentState,
        canonical_url: str,
        title: str | None,
        published_at: datetime | None,
        first_observed_at: datetime,
        last_observed_at: datetime,
        channel_id: int,
        message_id: int,
    ) -> None:
        """Idempotently link one already-delivered item into the unified content ledger.

        Used by both the one-time Twitch migration (Phase 2A's `live_signal_sessions`/
        `live_signal_deliveries`) and the live mirror `LiveSignalService` performs on
        every future successful Twitch announcement, so both paths produce identical
        `content_events`/`content_deliveries` rows. `content_events` is a plain upsert
        (safe to refresh state/timestamps on replay); `content_deliveries` is an
        `INSERT OR IGNORE` at `transition_seq = 1` already marked `delivered` with the
        real Discord ids — replaying this call, or racing the historical migration
        against a live mirror for the same identity, can never insert a second delivery
        row or change an already-recorded message id, so nothing is ever re-announced.
        """
        _require_guild_id(guild_id)
        async with self._write_transaction(immediate=True):
            await self._connection.execute(
                """
                INSERT INTO content_events (
                    guild_id, account_id, platform, external_id, content_kind, state,
                    canonical_url, title, published_at, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, platform, external_id) DO UPDATE SET
                    state = excluded.state,
                    title = excluded.title,
                    last_observed_at = excluded.last_observed_at
                """,
                (
                    guild_id,
                    account_id,
                    platform.value,
                    external_id,
                    content_kind.value,
                    state.value,
                    canonical_url,
                    title,
                    _stored_timestamp(published_at),
                    first_observed_at.isoformat(),
                    last_observed_at.isoformat(),
                ),
            )
            now_text = last_observed_at.isoformat()
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO content_deliveries (
                    guild_id, platform, external_id, transition_seq, account_id, status,
                    attempt, discord_channel_id, discord_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, 'delivered', 1, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    platform.value,
                    external_id,
                    account_id,
                    channel_id,
                    message_id,
                    now_text,
                    now_text,
                ),
            )

    # -- Watchdog (Phase 3) ---------------------------------------------------

    async def save_entry_sniff_assessment(
        self, assessment: EntrySniffAssessment
    ) -> EntrySniffAssessment:
        """Insert or replace the one durable assessment for a single member join.

        Identity is `(guild_id, member_id, joined_at)`, matching `EntrySniffAssessment`
        — a rejoin gets its own row rather than overwriting the prior join's record.
        Per-signal detail is redacted before storage, matching the design doc's Data
        Model note that the stored breakdown is "JSON, redacted".
        """
        _require_guild_id(assessment.guild_id)
        signals_payload: list[JSONValue] = [
            {
                "name": signal.name,
                "weight": signal.weight,
                "detail": signal.detail,
                "confidence": signal.confidence,
            }
            for signal in assessment.signals
        ]
        safe_signals = redact(signals_payload)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO entry_sniff_assessments (
                    guild_id, member_id, joined_at, band, signals_json, explanation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, member_id, joined_at) DO UPDATE SET
                    band = excluded.band,
                    signals_json = excluded.signals_json,
                    explanation = excluded.explanation,
                    created_at = excluded.created_at
                """,
                (
                    assessment.guild_id,
                    assessment.member_id,
                    assessment.joined_at.isoformat(),
                    assessment.band.value,
                    json.dumps(safe_signals, sort_keys=True, separators=(",", ":")),
                    assessment.explanation,
                    assessment.created_at.isoformat(),
                ),
            )
        saved = await self.get_entry_sniff_assessment(
            assessment.guild_id, assessment.member_id, joined_at=assessment.joined_at
        )
        if saved is None:
            raise RuntimeError("saved entry sniff assessment could not be read")
        return saved

    async def get_entry_sniff_assessment(
        self, guild_id: int, member_id: int, *, joined_at: datetime | None = None
    ) -> EntrySniffAssessment | None:
        """Return one member's assessment: the exact `joined_at` row, or (when omitted)
        the current/most recent one, matching `/fetch sniff <member>`'s "current or most
        recent assessment" behavior.
        """
        _require_guild_id(guild_id)
        if joined_at is not None:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, member_id, joined_at, band, signals_json, explanation, created_at
                FROM entry_sniff_assessments
                WHERE guild_id = ? AND member_id = ? AND joined_at = ?
                """,
                (guild_id, member_id, joined_at.isoformat()),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, member_id, joined_at, band, signals_json, explanation, created_at
                FROM entry_sniff_assessments
                WHERE guild_id = ? AND member_id = ?
                ORDER BY joined_at DESC
                LIMIT 1
                """,
                (guild_id, member_id),
            )
        row = await cursor.fetchone()
        return entry_sniff_assessment_from_row(row)

    async def list_recent_entry_sniff_assessments(
        self, guild_id: int, *, since: datetime, until: datetime, limit: int = 500
    ) -> tuple[EntrySniffAssessment, ...]:
        """Return every assessment in `guild_id` with `joined_at` in `[since, until]`.

        The only cross-member `entry_sniff_assessments` query in this phase's storage
        layer — `get_entry_sniff_assessment` above is scoped to one member (matching
        `/fetch sniff <member>` and `EntrySniffService`'s own read-after-write), but
        `RaidDetector` (Task 5) needs to see every recent join across a guild at once
        to detect a join-velocity/cluster spike, so this adds the one genuinely new
        query this task requires rather than duplicating join tracking elsewhere.
        """
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT guild_id, member_id, joined_at, band, signals_json, explanation, created_at
            FROM entry_sniff_assessments
            WHERE guild_id = ? AND joined_at >= ? AND joined_at <= ?
            ORDER BY joined_at DESC
            LIMIT ?
            """,
            (guild_id, since.isoformat(), until.isoformat(), limit),
        )
        rows = await cursor.fetchall()
        return tuple(
            cast(EntrySniffAssessment, entry_sniff_assessment_from_row(row)) for row in rows
        )

    async def _get_watch_window(self, guild_id: int, member_id: int) -> WatchWindow | None:
        cursor = await self._connection.execute(
            """
            SELECT guild_id, member_id, opened_at, expires_at, band, closed_at, close_reason
            FROM watch_windows
            WHERE guild_id = ? AND member_id = ?
            """,
            (guild_id, member_id),
        )
        row = await cursor.fetchone()
        return watch_window_from_row(row)

    async def open_watch_window(self, window: WatchWindow) -> WatchWindow:
        """Insert or replace the one open-or-closed watch window for a member.

        Identity is `(guild_id, member_id)`, matching `WatchWindow` — reopening a
        watch window for a member (for example a fresh incoming-signal escalation
        after a prior window already closed) replaces that member's single row rather
        than accumulating history; durable history of the transition belongs to
        `sniff_receipts`, not this table.
        """
        _require_guild_id(window.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO watch_windows (
                    guild_id, member_id, opened_at, expires_at, band, closed_at, close_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, member_id) DO UPDATE SET
                    opened_at = excluded.opened_at,
                    expires_at = excluded.expires_at,
                    band = excluded.band,
                    closed_at = excluded.closed_at,
                    close_reason = excluded.close_reason
                """,
                (
                    window.guild_id,
                    window.member_id,
                    window.opened_at.isoformat(),
                    window.expires_at.isoformat(),
                    window.band.value,
                    _stored_timestamp(window.closed_at),
                    window.close_reason.value if window.close_reason is not None else None,
                ),
            )
        saved = await self._get_watch_window(window.guild_id, window.member_id)
        if saved is None:
            raise RuntimeError("opened watch window could not be read")
        return saved

    async def close_watch_window(
        self,
        guild_id: int,
        member_id: int,
        *,
        reason: WatchWindowCloseReason,
        now: datetime,
    ) -> WatchWindow | None:
        """Close a member's open watch window, or do nothing if it is already closed.

        Genuinely idempotent: the `WHERE closed_at IS NULL` guard means a second call
        (whatever `reason`/`now` it passes) matches zero rows, never raises, and never
        overwrites the first close's `closed_at`/`close_reason` — matching the design
        doc's "clean members age out of watch state automatically" guarantee without
        letting a race between an expiry sweep and a staff override corrupt history.
        Returns `None` if no watch window exists for this member at all.
        """
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                UPDATE watch_windows
                SET closed_at = ?, close_reason = ?
                WHERE guild_id = ? AND member_id = ? AND closed_at IS NULL
                """,
                (now.isoformat(), reason.value, guild_id, member_id),
            )
        return await self._get_watch_window(guild_id, member_id)

    async def list_open_watch_windows(self, guild_id: int) -> tuple[WatchWindow, ...]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, member_id, opened_at, expires_at, band, closed_at, close_reason
            FROM watch_windows
            WHERE guild_id = ? AND closed_at IS NULL
            ORDER BY opened_at ASC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return tuple(cast(WatchWindow, watch_window_from_row(row)) for row in rows)

    async def record_incident(self, incident: Incident) -> Incident:
        """Insert or replace one incident-band evidence record.

        Identity is `(guild_id, incident_id)`, matching `Incident`. An upsert (rather
        than an append-only insert) so `acknowledged_by` can be set by a later staff
        action without a separate "acknowledge" table; every state transition is still
        durably mirrored to `sniff_receipts` by the caller.
        """
        _require_guild_id(incident.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO incidents (
                    guild_id, incident_id, kind, band, opened_at, evidence_packet_id,
                    recommended_action, acknowledged_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, incident_id) DO UPDATE SET
                    kind = excluded.kind,
                    band = excluded.band,
                    opened_at = excluded.opened_at,
                    evidence_packet_id = excluded.evidence_packet_id,
                    recommended_action = excluded.recommended_action,
                    acknowledged_by = excluded.acknowledged_by
                """,
                (
                    incident.guild_id,
                    incident.incident_id,
                    incident.kind.value,
                    incident.band.value,
                    incident.opened_at.isoformat(),
                    incident.evidence_packet_id,
                    incident.recommended_action,
                    incident.acknowledged_by,
                ),
            )
        saved = await self.get_incident(incident.guild_id, incident.incident_id)
        if saved is None:
            raise RuntimeError("recorded incident could not be read")
        return saved

    async def get_incident(self, guild_id: int, incident_id: str) -> Incident | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, incident_id, kind, band, opened_at, evidence_packet_id,
                   recommended_action, acknowledged_by
            FROM incidents
            WHERE guild_id = ? AND incident_id = ?
            """,
            (guild_id, incident_id),
        )
        row = await cursor.fetchone()
        return incident_from_row(row)

    async def list_recent_incidents(self, guild_id: int, limit: int = 50) -> tuple[Incident, ...]:
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT guild_id, incident_id, kind, band, opened_at, evidence_packet_id,
                   recommended_action, acknowledged_by
            FROM incidents
            WHERE guild_id = ?
            ORDER BY opened_at DESC, incident_id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return tuple(cast(Incident, incident_from_row(row)) for row in rows)

    async def save_allow_block_entry(self, entry: AllowBlockEntry) -> AllowBlockEntry:
        """Insert or replace one guild-configured allow/block entry for a Discord user.

        Identity is `(guild_id, discord_user_id)`, matching `AllowBlockEntry` — a user
        can hold at most one `list_kind` per guild at a time; re-setting the entry
        (including to the opposite `list_kind`) replaces it rather than accumulating
        history, matching the "staff-set facts" description in the design doc.
        """
        _require_guild_id(entry.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO guild_allow_block_lists (
                    guild_id, discord_user_id, list_kind, reason, set_by, set_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, discord_user_id) DO UPDATE SET
                    list_kind = excluded.list_kind,
                    reason = excluded.reason,
                    set_by = excluded.set_by,
                    set_at = excluded.set_at
                """,
                (
                    entry.guild_id,
                    entry.discord_user_id,
                    entry.list_kind,
                    entry.reason,
                    entry.set_by,
                    entry.set_at.isoformat(),
                ),
            )
        cursor = await self._connection.execute(
            """
            SELECT guild_id, discord_user_id, list_kind, reason, set_by, set_at
            FROM guild_allow_block_lists
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (entry.guild_id, entry.discord_user_id),
        )
        saved = allow_block_entry_from_row(await cursor.fetchone())
        if saved is None:
            raise RuntimeError("saved allow/block entry could not be read")
        return saved

    async def list_allow_block_entries(
        self, guild_id: int, list_kind: str | None = None
    ) -> tuple[AllowBlockEntry, ...]:
        _require_guild_id(guild_id)
        if list_kind is None:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, discord_user_id, list_kind, reason, set_by, set_at
                FROM guild_allow_block_lists
                WHERE guild_id = ?
                ORDER BY set_at DESC
                """,
                (guild_id,),
            )
        else:
            if list_kind not in ("allow", "block"):
                raise ValueError("list_kind must be 'allow' or 'block'")
            cursor = await self._connection.execute(
                """
                SELECT guild_id, discord_user_id, list_kind, reason, set_by, set_at
                FROM guild_allow_block_lists
                WHERE guild_id = ? AND list_kind = ?
                ORDER BY set_at DESC
                """,
                (guild_id, list_kind),
            )
        rows = await cursor.fetchall()
        return tuple(cast(AllowBlockEntry, allow_block_entry_from_row(row)) for row in rows)

    async def record_sniff_receipt(
        self,
        *,
        guild_id: int,
        receipt_id: str,
        member_id: int | None,
        action: str,
        detail: dict[str, JSONValue],
        created_at: datetime,
    ) -> None:
        """Record a redacted, append-only audit receipt for one watchdog storage
        transition, matching the `creator_registry_receipts`/`content_receipts`
        append-only receipt convention.
        """
        _require_guild_id(guild_id)
        safe_detail = _json_object(redact(detail))
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO sniff_receipts (
                    guild_id, receipt_id, member_id, action, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt_id,
                    member_id,
                    action,
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                    created_at.isoformat(),
                ),
            )

    async def list_sniff_receipts(
        self, guild_id: int, member_id: int | None = None, limit: int = 50
    ) -> tuple[SniffReceipt, ...]:
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if member_id is None:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, member_id, action, detail_json, created_at
                FROM sniff_receipts
                WHERE guild_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, limit),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT guild_id, receipt_id, member_id, action, detail_json, created_at
                FROM sniff_receipts
                WHERE guild_id = ? AND member_id = ?
                ORDER BY created_at DESC, receipt_id DESC
                LIMIT ?
                """,
                (guild_id, member_id, limit),
            )
        rows = await cursor.fetchall()
        return tuple(self._sniff_receipt_from_row(row) for row in rows)

    @staticmethod
    def _sniff_receipt_from_row(row: aiosqlite.Row) -> SniffReceipt:
        stored_member_id = row["member_id"]
        return SniffReceipt(
            guild_id=int(row["guild_id"]),
            receipt_id=str(row["receipt_id"]),
            member_id=int(stored_member_id) if stored_member_id is not None else None,
            action=str(row["action"]),
            detail=_json_object(json.loads(str(row["detail_json"]))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    # -- Activity Ledger (Phase 4) ---------------------------------------------

    async def record_ledger_event(self, event: LedgerEvent) -> None:
        """Append one raw ledger event, matching the append-only `guild_events`/
        `sniff_receipts` convention: never updated, never upserted. Storage shape is
        the single polymorphic `ledger_events` table (see
        `storage/activity_ledger_rows.py`'s module docstring for why); `event_id` is
        generated here since domain `LedgerEvent` value objects carry no identifier of
        their own.
        """
        _require_guild_id(event.guild_id)
        safe_detail = _json_object(redact(cast(JSONValue, ledger_event_detail(event))))
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO ledger_events (
                    guild_id, event_id, member_id, kind, occurred_at, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.guild_id,
                    str(uuid4()),
                    event.member_id,
                    event.kind.value,
                    event.occurred_at.isoformat(),
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                ),
            )

    async def list_ledger_events(
        self, guild_id: int, *, member_id: int, limit: int = 500
    ) -> tuple[LedgerEvent, ...]:
        """Return one member's raw ledger events, most recent first."""
        _require_guild_id(guild_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._connection.execute(
            """
            SELECT guild_id, member_id, kind, occurred_at, detail_json
            FROM ledger_events
            WHERE guild_id = ? AND member_id = ?
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (guild_id, member_id, limit),
        )
        rows = await cursor.fetchall()
        return tuple(cast(LedgerEvent, ledger_event_from_row(row)) for row in rows)

    async def save_milestone(self, milestone: Milestone) -> Milestone:
        """Insert or replace one milestone record.

        Identity is `(guild_id, member_id, kind, reached_at)`, matching `Milestone` —
        re-saving the same member/kind/timestamp (for example a re-computed `detail`
        wording) replaces that row rather than accumulating duplicates.
        """
        _require_guild_id(milestone.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO milestones (guild_id, member_id, kind, reached_at, detail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, member_id, kind, reached_at) DO UPDATE SET
                    detail = excluded.detail
                """,
                (
                    milestone.guild_id,
                    milestone.member_id,
                    milestone.kind.value,
                    milestone.reached_at.isoformat(),
                    milestone.detail,
                ),
            )
        stored = await self.list_milestones(milestone.guild_id, member_id=milestone.member_id)
        for candidate in stored:
            if candidate.kind == milestone.kind and candidate.reached_at == milestone.reached_at:
                return candidate
        raise RuntimeError("saved milestone could not be read")

    async def list_milestones(
        self, guild_id: int, *, member_id: int
    ) -> tuple[Milestone, ...]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, member_id, kind, reached_at, detail
            FROM milestones
            WHERE guild_id = ? AND member_id = ?
            ORDER BY reached_at DESC
            """,
            (guild_id, member_id),
        )
        rows = await cursor.fetchall()
        return tuple(cast(Milestone, milestone_from_row(row)) for row in rows)

    async def save_exclusion_entry(self, entry: ExclusionEntry) -> ExclusionEntry:
        """Insert or replace one guild-configured excluded channel.

        Identity is `(guild_id, channel_id)`, matching `ExclusionEntry` — re-excluding
        an already-excluded channel (for example to update `reason`) replaces the row
        rather than accumulating history.
        """
        _require_guild_id(entry.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO channel_exclusions (
                    guild_id, channel_id, excluded_by, reason, excluded_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    excluded_by = excluded.excluded_by,
                    reason = excluded.reason,
                    excluded_at = excluded.excluded_at
                """,
                (
                    entry.guild_id,
                    entry.channel_id,
                    entry.excluded_by,
                    entry.reason,
                    entry.excluded_at.isoformat(),
                ),
            )
        cursor = await self._connection.execute(
            """
            SELECT guild_id, channel_id, excluded_by, reason, excluded_at
            FROM channel_exclusions
            WHERE guild_id = ? AND channel_id = ?
            """,
            (entry.guild_id, entry.channel_id),
        )
        saved = exclusion_entry_from_row(await cursor.fetchone())
        if saved is None:
            raise RuntimeError("saved exclusion entry could not be read")
        return saved

    async def list_exclusion_entries(self, guild_id: int) -> tuple[ExclusionEntry, ...]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, channel_id, excluded_by, reason, excluded_at
            FROM channel_exclusions
            WHERE guild_id = ?
            ORDER BY excluded_at DESC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return tuple(cast(ExclusionEntry, exclusion_entry_from_row(row)) for row in rows)

    async def save_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        """Insert or replace the one retention policy for a guild.

        Identity is `guild_id` alone, matching `RetentionPolicy` — a guild holds at
        most one configured retention window at a time.
        """
        _require_guild_id(policy.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO retention_policies (
                    guild_id, max_age_days, updated_by, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    max_age_days = excluded.max_age_days,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.guild_id,
                    policy.max_age_days,
                    policy.updated_by,
                    policy.updated_at.isoformat(),
                ),
            )
        saved = await self.get_retention_policy(policy.guild_id)
        if saved is None:
            raise RuntimeError("saved retention policy could not be read")
        return saved

    async def get_retention_policy(self, guild_id: int) -> RetentionPolicy | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, max_age_days, updated_by, updated_at
            FROM retention_policies
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        return retention_policy_from_row(row)

    async def record_activity_receipt(
        self,
        *,
        guild_id: int,
        receipt_id: str,
        member_id: int | None,
        action: str,
        detail: dict[str, JSONValue],
        created_at: datetime,
    ) -> None:
        """Record a redacted, append-only audit receipt for one activity-ledger
        storage action, matching the `sniff_receipts`/`creator_registry_receipts`
        append-only receipt convention.

        This method is generic and reusable across every activity-ledger action, not
        restricted to deletion receipts — `detail` is fully caller-controlled. Because
        of that, a row here referencing a `member_id` is genuine member-scoped ledger
        data, not merely an inert "an action happened" marker, so it is deleted by
        `delete_member_ledger_data` exactly like `ledger_events`/`milestones` rows.
        See that method's docstring for the reasoning.
        """
        _require_guild_id(guild_id)
        safe_detail = _json_object(redact(cast(JSONValue, detail)))
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO activity_receipts (
                    guild_id, receipt_id, member_id, action, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt_id,
                    member_id,
                    action,
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")),
                    created_at.isoformat(),
                ),
            )

    async def delete_member_ledger_data(self, guild_id: int, member_id: int) -> None:
        """Delete every member-scoped activity ledger row for one member, in a single
        transaction.

        Tables this method must touch — every table this task creates that stores
        member-scoped ledger rows (keep this list in sync with the schema in
        `_initialize` above):
          - `ledger_events`      (raw per-kind events, keyed by (guild_id, event_id))
          - `milestones`         (materialized milestone facts)
          - `activity_receipts`  (member-referencing audit rows)

        `activity_receipts` is included deliberately, not exempted: `record_activity_receipt`
        is a generic, reusable method whose `detail` is fully caller-controlled, so a
        row referencing this `member_id` may hold genuine member-scoped content (for
        example a rich milestone- or role-change receipt) — exactly the kind of
        "retained copy of otherwise-deleted data" the design doc's Privacy Controls
        "Deletion" section warns against. If a narrower, permanent "deletion occurred"
        audit trail is ever wanted, that calls for a separate, deliberately minimal
        mechanism (storing only `member_id` + a fixed action name + a timestamp,
        written by the deletion caller as an explicit step *after* this method
        returns) — not a carve-out inside this generic receipts table.

        Deliberately NOT touched here (also created by this task, but never
        member-scoped data — neither table has a `member_id` column at all):
          - `channel_exclusions`  — guild-level configuration, keyed by channel.
          - `retention_policies`  — guild-level configuration, one row per guild.

        Idempotent: deleting a member with no rows in any of the three tables above is
        a no-op, not an error.
        """
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                "DELETE FROM ledger_events WHERE guild_id = ? AND member_id = ?",
                (guild_id, member_id),
            )
            await self._connection.execute(
                "DELETE FROM milestones WHERE guild_id = ? AND member_id = ?",
                (guild_id, member_id),
            )
            await self._connection.execute(
                "DELETE FROM activity_receipts WHERE guild_id = ? AND member_id = ?",
                (guild_id, member_id),
            )
