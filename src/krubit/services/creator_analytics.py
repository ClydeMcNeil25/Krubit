"""Factual creator analytics: counts, latency, state, quota history, suppression.

`CreatorAnalyticsService` reports only durable facts already recorded by the content
ledger (`ContentSignalService`), the delivery runtime (`ContentRuntime`), and the
mention-budget ledger (`SQLiteStore.claim_mention_budget`/`record_mention_receipt`).
It never ranks, scores, or editorializes a creator's content — no "best performing"
platform, no sentiment, no recommendation. Every field here is either a count, a
duration, a stored state, or a short list of already-redacted receipt reasons.
"""

from __future__ import annotations

from dataclasses import dataclass

from krubit.domain.creator_signals import ContentCursor, ContentDelivery, CreatorAccount
from krubit.storage.sqlite import MentionBudgetReceipt, SQLiteStore

_DEFAULT_RECEIPT_LIMIT = 10
_DEFAULT_QUOTA_LIMIT = 10


@dataclass(frozen=True, slots=True)
class DeliveryCounts:
    """Factual delivery-status counts for one creator account."""

    delivered: int
    pending: int
    failed: int
    cancelled: int

    @property
    def total(self) -> int:
        return self.delivered + self.pending + self.failed + self.cancelled


@dataclass(frozen=True, slots=True)
class AccountAnalytics:
    """Every factual metric `/fetch creator show` and `/fetch notifications status`
    surface for one creator account. `suppression_reasons` is drawn from already
    redacted `content_receipts` detail (`SQLiteStore.record_content_receipt` redacts
    at write time), so nothing here can carry a secret regardless of what a connector
    originally reported."""

    account: CreatorAccount
    cursor: ContentCursor | None
    delivery_counts: DeliveryCounts
    average_delivery_latency_seconds: float | None
    suppression_reasons: tuple[str, ...]


def _delivery_latency_seconds(delivery: ContentDelivery) -> float | None:
    if delivery.status != "delivered":
        return None
    return (delivery.updated_at - delivery.created_at).total_seconds()


class CreatorAnalyticsService:
    """Read-only aggregation over durable storage; performs no mutation of its own."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def delivery_counts(self, guild_id: int, account_id: str) -> DeliveryCounts:
        deliveries = await self._store.list_content_deliveries_for_account(guild_id, account_id)
        delivered = sum(1 for item in deliveries if item.status == "delivered")
        pending = sum(1 for item in deliveries if item.status == "pending")
        failed = sum(1 for item in deliveries if item.status == "failed")
        cancelled = sum(1 for item in deliveries if item.status == "cancelled")
        return DeliveryCounts(
            delivered=delivered, pending=pending, failed=failed, cancelled=cancelled
        )

    async def average_delivery_latency_seconds(
        self, guild_id: int, account_id: str
    ) -> float | None:
        """Mean seconds between a delivery's claim (`created_at`) and its first
        successful send (`updated_at` while `status == "delivered"`). `None` when no
        delivery for this account has ever succeeded."""
        deliveries = await self._store.list_content_deliveries_for_account(guild_id, account_id)
        latencies = [
            latency
            for latency in (_delivery_latency_seconds(item) for item in deliveries)
            if latency is not None
        ]
        if not latencies:
            return None
        return sum(latencies) / len(latencies)

    async def suppression_reasons(
        self, guild_id: int, account_id: str, *, limit: int = _DEFAULT_RECEIPT_LIMIT
    ) -> tuple[str, ...]:
        """The most recent redacted diagnostic actions recorded for this account —
        for example a malformed connector item that was skipped rather than
        delivered. Every value here already passed through `redact()` at write time
        (`SQLiteStore.record_content_receipt`)."""
        receipts = await self._store.list_content_receipts(guild_id, account_id, limit=limit)
        return tuple(receipt.action for receipt in receipts)

    async def quota_history(
        self, guild_id: int, *, limit: int = _DEFAULT_QUOTA_LIMIT
    ) -> tuple[MentionBudgetReceipt, ...]:
        """The guild's most recent mention-budget outcomes. Mention budgets are a
        per-guild resource (not per-account), matching `NotificationPolicy`."""
        receipts = await self._store.list_mention_budget_receipts(guild_id, limit=limit)
        return tuple(receipts)

    async def account_report(self, guild_id: int, account_id: str) -> AccountAnalytics | None:
        """Every factual metric bundled for one account, or `None` if it is not
        registered in this guild."""
        account = await self._store.get_creator_account(guild_id, account_id)
        if account is None:
            return None
        cursor = await self._store.get_content_cursor(guild_id, account_id)
        counts = await self.delivery_counts(guild_id, account_id)
        latency = await self.average_delivery_latency_seconds(guild_id, account_id)
        reasons = await self.suppression_reasons(guild_id, account_id)
        return AccountAnalytics(
            account=account,
            cursor=cursor,
            delivery_counts=counts,
            average_delivery_latency_seconds=latency,
            suppression_reasons=reasons,
        )

    async def own_profile(
        self, guild_id: int, owner_member_id: int
    ) -> tuple[CreatorAccount, ...]:
        """The exact set of accounts a member owns in this guild — the factual basis
        for "add my own profile" authority checks and self-service listings."""
        accounts = await self._store.list_creator_accounts_for_owner(guild_id, owner_member_id)
        return tuple(accounts)
