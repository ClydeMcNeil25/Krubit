"""Once-per-day summary claims and durable delivery outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from krubit.storage.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class DailySummaryResult:
    guild_id: int
    summary_date: date
    status: str
    channel_id: int | None


class DailySummaryService:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def claim(self, guild_id: int, summary_date: date) -> bool:
        return await self._store.claim_daily_summary(guild_id, summary_date)

    async def record_outcome(
        self,
        guild_id: int,
        summary_date: date,
        *,
        status: str,
        channel_id: int | None,
    ) -> DailySummaryResult:
        await self._store.set_daily_summary_status(
            guild_id, summary_date, status, channel_id
        )
        return DailySummaryResult(guild_id, summary_date, status, channel_id)
