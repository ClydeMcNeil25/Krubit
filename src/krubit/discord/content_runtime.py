"""Safe Discord execution for durable, platform-neutral content delivery plans.

`ContentRuntime.apply_plan` is the one place a `ContentPlan` produced by
`ContentSignalService.ingest_page` turns into a Discord action. It never resolves a
channel or a message by a mutable name after the account's route is configured — only
the `CreatorRoute.channel_id` and, once a delivery has a stored `discord_message_id`,
that exact message. Applying the same plan twice is always safe: a delivery already
carrying a stored message is *edited* to reflect the event's current state rather than
resent, a delivery with no stored message first does a bounded history scan for an
already-sent-but-unrecorded message (crash recovery) before ever sending fresh, and a
retracted delivery is permanently skipped. `recover_pending` sweeps the guild's still-
pending backlog through the same `apply_plan` path; `retry_delivery` and
`retract_delivery` are the staff-invoked actions for a failed or unwanted delivery,
addressed by an exact `delivery_id` rather than by re-deriving it from mutable state.

Per Task 5's delivery-policy design, the mention decision for every send always goes
through `NotificationPolicy.evaluate_and_claim` — never `decide_mention`/`evaluate`
paired with a separate budget claim — so a lost mention-budget race downgrades cleanly
to no mention without ever blocking or duplicating the underlying delivery.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast

import discord

from krubit.discord.content_cards import (
    ContentGroup,
    ContentGroupMember,
    RenderedCard,
    build_live_card,
    build_social_card,
)
from krubit.domain.creator_signals import (
    CapabilityState,
    ContentDelivery,
    ContentEvent,
    ContentKind,
    ContentPlan,
    CreatorAccount,
    CreatorRoute,
    Platform,
)
from krubit.integrations.base import Connector, ConnectorFailure, ConnectorFailureKind
from krubit.services.content_signals import ContentSignalService
from krubit.services.notification_policy import (
    DeliveryDecision,
    DeliveryDisposition,
    MentionBudgetState,
    MentionKind,
    NotificationPolicy,
)
from krubit.storage.sqlite import ContentScheduleState, SQLiteStore

_HISTORY_RECOVERY_LIMIT = 25
_RETRACTED_NOTICE = "⚠️ This announcement was retracted by a moderator."

PolicyFactory = Callable[[discord.Guild, CreatorRoute], NotificationPolicy]


def _default_policy_factory(_guild: discord.Guild, route: CreatorRoute) -> NotificationPolicy:
    """An unlimited-budget, no-quiet-hours policy used until guild config exists.

    Guild-level quiet-hours and mention-budget configuration is not yet persisted
    anywhere Task 6 can read from, so the default policy never suppresses or queues a
    delivery on those grounds; the `social_mention_role_id` still comes honestly from
    the account's own configured route.
    """
    return NotificationPolicy(
        quiet_hours=None,
        live_everyone_budget=MentionBudgetState(limit=None),
        social_role_budget=MentionBudgetState(limit=None),
        social_mention_role_id=route.mention_role_id,
        live_bypass_quiet_hours=True,
    )


class ContentRuntime:
    """Execute only the Discord actions a claimed `ContentPlan` already approved."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        policy_factory: PolicyFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy_factory = policy_factory or _default_policy_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    async def apply_plan(self, guild: discord.Guild, plan: ContentPlan) -> bool:
        """Apply one claimed delivery idempotently; edits in place on replay.

        The mention decision (and the budget claim inside it) is only made on the
        genuinely-fresh-send path. Replaying an already-delivered transition — a later
        state-transition plan, a `recover_pending` sweep, `retry_delivery` re-invoking
        this method — edits the existing message's embed/view to reflect the event's
        current state without re-deciding or re-consuming a mention: nothing new is
        being announced, so nothing should be re-claimed.
        """
        if guild.id != plan.event.guild_id or guild.id != plan.delivery.guild_id:
            return False
        if not await self._store.guild_is_enabled(guild.id):
            return False
        async with self._guild_lock(guild.id):
            route = await self._resolve_route(guild.id, plan.event)
            if route is None:
                return False
            channel = self._usable_channel(guild, route.channel_id)
            if channel is None:
                return False
            current = await self._store.get_content_delivery_by_seq(
                guild.id,
                plan.delivery.platform,
                plan.delivery.external_id,
                plan.delivery.transition_seq,
            )
            if current is None or current.status == "cancelled":
                return False
            group = await self._build_group(guild.id, plan.event, route)
            if current.discord_channel_id is not None and current.discord_message_id is not None:
                card = self._render(plan.event.content_kind, group, MentionKind.NONE)
                return await self._edit_existing(guild, channel, current, card)
            decision = await self._decide(guild, plan.event, route)
            if decision.disposition is DeliveryDisposition.QUEUE:
                return False
            card = self._render(plan.event.content_kind, group, decision.mention)
            return await self._deliver_fresh(guild, channel, current, card)

    async def apply_plans(self, guild: discord.Guild, plans: Iterable[ContentPlan]) -> int:
        """Apply every freshly claimed plan from one connector page's `IngestionResult`.

        The scheduler that polls or receives a push for a connector (for example
        `YouTubeConnector`) calls this with `IngestionResult.plans` right after
        `ContentSignalService.ingest_page`; each plan goes through the same idempotent
        `apply_plan` path `recover_pending` and `retry_delivery` already use, so a
        partial failure here never double-delivers on a later retry.
        """
        applied = 0
        for plan in plans:
            if await self.apply_plan(guild, plan):
                applied += 1
        return applied

    async def recover_pending(self, guild: discord.Guild) -> int:
        """Sweep this guild's still-pending deliveries back through `apply_plan`."""
        if not await self._store.guild_is_enabled(guild.id):
            return 0
        pending = await self._store.list_pending_content_deliveries(guild.id)
        applied = 0
        for delivery in pending:
            event = await self._store.get_content_event(
                guild.id, delivery.platform, delivery.external_id
            )
            if event is None:
                continue
            if await self.apply_plan(guild, ContentPlan(event=event, delivery=delivery)):
                applied += 1
        return applied

    async def retry_delivery(self, guild: discord.Guild, delivery_id: str) -> bool:
        """Re-attempt a previously failed delivery, bumping its attempt counter."""
        if not await self._store.guild_is_enabled(guild.id):
            return False
        platform, external_id, transition_seq = parse_delivery_id(delivery_id)
        delivery = await self._store.get_content_delivery_by_seq(
            guild.id, platform, external_id, transition_seq
        )
        if delivery is None or delivery.status != "failed":
            return False
        event = await self._store.get_content_event(guild.id, platform, external_id)
        if event is None:
            return False
        bumped = await self._store.update_content_delivery(
            guild_id=guild.id,
            platform=platform,
            external_id=external_id,
            transition_seq=transition_seq,
            status="pending",
            attempt=delivery.attempt + 1,
            channel_id=None,
            message_id=None,
            now=self._now(),
        )
        return await self.apply_plan(guild, ContentPlan(event=event, delivery=bumped))

    async def retract_delivery(self, guild: discord.Guild, delivery_id: str) -> bool:
        """Permanently withdraw a delivery. Callers must authorize this themselves.

        `ContentRuntime` performs only the mechanical retraction (editing the exact
        stored message and marking the delivery `cancelled`); a staff-permission check
        belongs to the command surface that calls this, not here.
        """
        if not await self._store.guild_is_enabled(guild.id):
            return False
        platform, external_id, transition_seq = parse_delivery_id(delivery_id)
        async with self._guild_lock(guild.id):
            delivery = await self._store.get_content_delivery_by_seq(
                guild.id, platform, external_id, transition_seq
            )
            if delivery is None or delivery.status == "cancelled":
                return False
            if delivery.discord_channel_id is not None and delivery.discord_message_id is not None:
                channel = self._usable_channel(guild, delivery.discord_channel_id)
                if channel is not None:
                    try:
                        message = await channel.fetch_message(delivery.discord_message_id)
                        await message.edit(content=_RETRACTED_NOTICE, embed=None, view=None)
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
            await self._store.update_content_delivery(
                guild_id=guild.id,
                platform=platform,
                external_id=external_id,
                transition_seq=transition_seq,
                status="cancelled",
                attempt=delivery.attempt,
                channel_id=delivery.discord_channel_id,
                message_id=delivery.discord_message_id,
                now=self._now(),
            )
        return True

    async def _resolve_route(self, guild_id: int, event: ContentEvent) -> CreatorRoute | None:
        routes = await self._store.list_creator_routes(guild_id, event.account_id)
        for route in routes:
            if route.content_kind is event.content_kind:
                return route
        return None

    def _usable_channel(self, guild: discord.Guild, channel_id: int) -> _TextChannel | None:
        channel = guild.get_channel(channel_id)
        if not _is_text_channel(channel):
            return None
        text_channel = cast(_TextChannel, channel)
        permissions = text_channel.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
            )
        ):
            return None
        return text_channel

    async def _decide(
        self, guild: discord.Guild, event: ContentEvent, route: CreatorRoute
    ) -> DeliveryDecision:
        policy = self._policy_factory(guild, route)
        at = self._now()
        is_live = event.content_kind is ContentKind.LIVE
        budget_kind = "live_everyone" if is_live else "social_role"
        limit = policy.live_everyone_budget.limit if is_live else policy.social_role_budget.limit

        async def claim_mention() -> bool:
            if limit is None:
                return True
            return await self._store.claim_mention_budget(
                guild_id=guild.id,
                budget_kind=budget_kind,
                period_key=at.date().isoformat(),
                limit=limit,
                now=at,
            )

        return await policy.evaluate_and_claim(event, at, claim_mention=claim_mention)

    async def _build_group(
        self, guild_id: int, event: ContentEvent, route: CreatorRoute
    ) -> ContentGroup:
        primary = await self._group_member(guild_id, event)
        members = [primary]
        group_id = await self._store.get_content_correlation_group(
            guild_id, event.platform, event.external_id
        )
        if group_id is not None:
            for platform, external_id in await self._store.list_content_correlation_members(
                guild_id, group_id
            ):
                if (platform, external_id) == (event.platform, event.external_id):
                    continue
                member_event = await self._store.get_content_event(guild_id, platform, external_id)
                if member_event is None:
                    continue
                members.append(await self._group_member(guild_id, member_event))
        return ContentGroup(
            content_kind=event.content_kind,
            members=tuple(members),
            mention_role_id=route.mention_role_id,
        )

    async def _group_member(self, guild_id: int, event: ContentEvent) -> ContentGroupMember:
        account = await self._store.get_creator_account(guild_id, event.account_id)
        display_name = account.handle if account is not None else event.account_id
        return ContentGroupMember(
            platform=event.platform,
            canonical_url=event.canonical_url,
            creator_display_name=display_name,
            title=event.title,
        )

    @staticmethod
    def _render(
        content_kind: ContentKind, group: ContentGroup, mention: MentionKind
    ) -> RenderedCard:
        if content_kind is ContentKind.LIVE:
            return build_live_card(group, mention=mention)
        return build_social_card(group, mention=mention)

    async def _edit_existing(
        self,
        guild: discord.Guild,
        channel: _TextChannel,
        current: ContentDelivery,
        card: RenderedCard,
    ) -> bool:
        """Edit the exact stored message's embed/view; content (mention text) is never
        touched here — replaying an already-delivered transition never re-announces."""
        if current.discord_message_id is None:
            return False
        try:
            message = await channel.fetch_message(current.discord_message_id)
            await message.edit(embed=card.embed, view=card.build_view())
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # Durable failure receipt: the stored delivery must not keep silently
            # claiming `delivered` against a message Discord can no longer serve (for
            # example, a moderator deleted it). Clearing the stored ids lets a later
            # `retry_delivery` send a fresh message instead of retrying a dead edit.
            await self._store.update_content_delivery(
                guild_id=guild.id,
                platform=current.platform,
                external_id=current.external_id,
                transition_seq=current.transition_seq,
                status="failed",
                attempt=current.attempt,
                channel_id=None,
                message_id=None,
                now=self._now(),
            )
            return False
        return True

    async def _deliver_fresh(
        self,
        guild: discord.Guild,
        channel: _TextChannel,
        current: ContentDelivery,
        card: RenderedCard,
    ) -> bool:
        platform = current.platform
        external_id = current.external_id
        transition_seq = current.transition_seq
        attempt = current.attempt
        nonce = _delivery_nonce(guild.id, platform, external_id, transition_seq, attempt)
        recovered = await _find_nonce_message(channel, guild.me, nonce)
        if recovered is not None:
            await self._store.update_content_delivery(
                guild_id=guild.id,
                platform=platform,
                external_id=external_id,
                transition_seq=transition_seq,
                status="delivered",
                attempt=attempt,
                channel_id=channel.id,
                message_id=recovered.id,
                now=self._now(),
            )
            return True
        try:
            message = await channel.send(
                content=card.content,
                embed=card.embed,
                view=card.build_view(),
                allowed_mentions=card.allowed_mentions,
                nonce=nonce,
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
            await self._store.update_content_delivery(
                guild_id=guild.id,
                platform=platform,
                external_id=external_id,
                transition_seq=transition_seq,
                status="failed",
                attempt=attempt,
                channel_id=None,
                message_id=None,
                now=self._now(),
            )
            return False
        await self._store.update_content_delivery(
            guild_id=guild.id,
            platform=platform,
            external_id=external_id,
            transition_seq=transition_seq,
            status="delivered",
            attempt=attempt,
            channel_id=channel.id,
            message_id=message.id,
            now=self._now(),
        )
        return True


def _is_text_channel(channel: object | None) -> bool:
    return (
        channel is not None
        and callable(getattr(channel, "send", None))
        and callable(getattr(channel, "permissions_for", None))
    )


class _TextChannel(Protocol):
    id: int

    def permissions_for(self, member: object) -> discord.Permissions: ...

    async def send(
        self,
        *,
        content: str,
        embed: discord.Embed,
        view: discord.ui.View,
        allowed_mentions: discord.AllowedMentions,
        nonce: str,
    ) -> discord.Message: ...

    async def fetch_message(self, message_id: int) -> discord.Message: ...

    def history(self, *, limit: int) -> AsyncIterator[discord.Message]: ...


def _delivery_nonce(
    guild_id: int, platform: Platform, external_id: str, transition_seq: int, attempt: int
) -> str:
    """A deterministic, Discord-compliant (<=25 char) nonce for one delivery attempt."""
    payload = f"{guild_id}:{platform.value}:{external_id}:{transition_seq}:{attempt}"
    return sha256(payload.encode()).hexdigest()[:25]


def parse_delivery_id(delivery_id: str) -> tuple[Platform, str, int]:
    """Parse the `platform:external_id:transition_seq` id `retry_delivery`/
    `retract_delivery` are addressed by. `external_id` may itself contain `:`, so the
    transition sequence and platform are peeled from the outside in.

    Public so command surfaces (`krubit.discord.content_commands`) can validate a
    staff-supplied `delivery_id` before ever calling `retry_delivery`/`retract_delivery`
    themselves, without duplicating this parsing rule.
    """
    remainder, _, seq_text = delivery_id.rpartition(":")
    platform_text, _, external_id = remainder.partition(":")
    try:
        return Platform(platform_text), external_id, int(seq_text)
    except ValueError as exc:
        raise ValueError(f"malformed delivery_id: {delivery_id!r}") from exc


async def _find_nonce_message(
    channel: _TextChannel, bot_member: object, nonce: str
) -> discord.Message | None:
    """Bound crash-recovery scan; discord.py exposes nonce but no enforce_nonce switch."""
    async for message in channel.history(limit=_HISTORY_RECOVERY_LIMIT):
        if (
            getattr(getattr(message, "author", None), "id", None) == getattr(bot_member, "id", None)
            and getattr(message, "nonce", None) == nonce
        ):
            return message
    return None


def delivery_id_for(platform: Platform, external_id: str, transition_seq: int) -> str:
    """The stable `delivery_id` used by `retry_delivery`/`retract_delivery`."""
    return f"{platform.value}:{external_id}:{transition_seq}"


# --- Connector polling scheduler ------------------------------------------------
#
# Default healthy fallback polling intervals. Fanbase is deliberately absent: its
# connector's declared capability is permanently `UNSUPPORTED` (Task 10 made
# `FanbaseConnector.fetch_page` always raise `UnsupportedConnectorError`), so the
# caller wiring `ConnectorScheduler` (`krubit.discord.bot`) never puts a `FANBASE`
# entry in `connectors` — with no entry, `run_cycle` has nothing to iterate for it,
# not even a skipped or disabled job.
_DEFAULT_POLL_INTERVAL_SECONDS: Mapping[Platform, int] = {
    Platform.YOUTUBE: 300,
    Platform.X: 900,
    Platform.BLUESKY: 120,
    Platform.INSTAGRAM: 300,
    Platform.FACEBOOK: 300,
    Platform.FACEBOOK_PAGE: 300,
    Platform.THREADS: 300,
    Platform.TIKTOK: 300,
}
_MAX_POLL_INTERVAL_SECONDS = 6 * 60 * 60
_DEFAULT_CONNECTOR_CONCURRENCY = 3
_BACKOFF_JITTER_FRACTION = 0.2

_SCHEDULER_FAILURE_STATE: Mapping[ConnectorFailureKind, CapabilityState] = {
    ConnectorFailureKind.AUTHORIZATION: CapabilityState.AUTHORIZATION_REQUIRED,
    ConnectorFailureKind.RATE_LIMITED: CapabilityState.DEGRADED,
    ConnectorFailureKind.QUOTA_EXCEEDED: CapabilityState.QUOTA_LIMITED,
    ConnectorFailureKind.UNAVAILABLE: CapabilityState.DEGRADED,
    ConnectorFailureKind.INVALID_RESPONSE: CapabilityState.DEGRADED,
    ConnectorFailureKind.TIMEOUT: CapabilityState.DEGRADED,
    ConnectorFailureKind.NOT_FOUND: CapabilityState.DEGRADED,
}


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    """One account's most recent poll outcome, as `ConnectorScheduler.result` reports it."""

    guild_id: int
    platform: Platform
    account_id: str | None
    state: CapabilityState
    detail: str | None = None


def _classify_scheduler_failure(
    exc: Exception,
) -> tuple[CapabilityState, str | None, float | None]:
    """Classify a connector job failure without depending on any one connector module.

    Every concrete connector raises its own `*ConnectorError` wrapping a classified
    `ConnectorFailure` (see `krubit.integrations.x.XConnectorError` and its siblings);
    rather than import each of those exception types here, this duck-types the shared
    shape (`.failure: ConnectorFailure`) so a future connector needs no scheduler
    change to report health correctly. `retry_after_seconds` is the forward-compatible
    hook a connector can set on its own exception (none currently do — no connector
    module surfaces a numeric rate-limit reset today) so a future one can hand the
    scheduler an exact resume time; `_next_interval` never schedules sooner than it.
    """
    failure = getattr(exc, "failure", None)
    retry_after = getattr(exc, "retry_after_seconds", None)
    retry_after_seconds = float(retry_after) if isinstance(retry_after, int | float) else None
    if isinstance(failure, ConnectorFailure):
        return (
            _SCHEDULER_FAILURE_STATE.get(failure.kind, CapabilityState.DEGRADED),
            failure.safe_detail,
            retry_after_seconds,
        )
    return CapabilityState.DEGRADED, "the connector raised an unexpected error", retry_after_seconds


class ConnectorScheduler:
    """Poll every enrolled, non-paused account's connector on a durable schedule.

    One bounded `asyncio.Semaphore` per platform (default 3, overridable via
    `concurrency_limits`) caps how many accounts *on that platform* are ever fetched
    concurrently; different platforms always run fully in parallel. Each account's poll
    is its own isolated job: `_poll_once`'s `try`/`except` classifies and records a
    connector failure without raising, and `_poll_account`'s outer `try`/`except` is a
    second isolation layer against anything else going wrong (for example the
    durable-schedule write itself). Because `run_cycle` gathers every job and no job
    ever raises out of its own coroutine, one guild's or one platform's failing
    connector can never cancel, delay, or otherwise affect any other guild's or
    platform's job in the same cycle — including other accounts on the very same
    platform, since each holds the shared semaphore only for its own fetch.

    `next_poll_at` is read from and written to `SQLiteStore.content_schedule` on every
    poll, so a fresh `ConnectorScheduler` built after a restart (a new process, a new
    instance in a test) honors exactly the backoff the previous instance computed
    rather than polling early just because in-memory state was lost.
    """

    def __init__(
        self,
        store: SQLiteStore,
        connectors: Mapping[Platform, Connector],
        *,
        guild_ids: Callable[[], Sequence[int]],
        signals: ContentSignalService | None = None,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
        default_intervals: Mapping[Platform, int] | None = None,
        concurrency_limits: Mapping[Platform, int] | None = None,
        on_plans: Callable[[int, tuple[ContentPlan, ...]], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._connectors = dict(connectors)
        self._guild_ids = guild_ids
        self._signals = signals or ContentSignalService(store)
        self._now = now or (lambda: datetime.now(UTC))
        self._jitter = jitter or (lambda: random.uniform(0.0, _BACKOFF_JITTER_FRACTION))
        self._intervals = {**_DEFAULT_POLL_INTERVAL_SECONDS, **(default_intervals or {})}
        self._semaphores = {
            platform: asyncio.Semaphore(
                (concurrency_limits or {}).get(platform, _DEFAULT_CONNECTOR_CONCURRENCY)
            )
            for platform in self._connectors
        }
        self._on_plans = on_plans
        self._results: dict[tuple[int, Platform], SchedulerResult] = {}

    def result(self, guild_id: int, platform: Platform) -> SchedulerResult | None:
        """The most recent poll outcome for one guild+platform, or `None` if never polled."""
        return self._results.get((guild_id, platform))

    async def run_cycle(self) -> None:
        """Poll every due, non-paused, connector-backed account exactly once."""
        jobs: list[Coroutine[object, object, None]] = []
        for guild_id in self._guild_ids():
            for account in await self._store.list_creator_accounts(guild_id):
                if account.paused or account.platform not in self._connectors:
                    continue
                jobs.append(self._poll_account(guild_id, account))
        if jobs:
            await asyncio.gather(*jobs)

    async def _poll_account(self, guild_id: int, account: CreatorAccount) -> None:
        async with self._semaphores[account.platform]:
            # Isolation boundary: nothing this job does (including the connector fetch
            # already isolated inside `_poll_once`) may ever propagate through
            # `asyncio.gather` and affect any other guild's or platform's job.
            with suppress(Exception):
                await self._poll_once(guild_id, account)

    async def _poll_once(self, guild_id: int, account: CreatorAccount) -> None:
        now = self._now()
        schedule = await self._store.get_content_schedule(
            guild_id, account.account_id, account.platform
        )
        if schedule is not None and schedule.next_poll_at > now:
            self._results[(guild_id, account.platform)] = SchedulerResult(
                guild_id,
                account.platform,
                account.account_id,
                schedule.last_state,
                schedule.last_detail,
            )
            return
        connector = self._connectors[account.platform]
        consecutive_failures = schedule.consecutive_failures if schedule is not None else 0
        retry_after: float | None = None
        try:
            cursor = await self._store.get_content_cursor(guild_id, account.account_id)
            page = await connector.fetch_page(account, cursor=cursor.value if cursor else None)
            ingestion = await self._signals.ingest_page(account, page, now=now)
            state, detail = CapabilityState.READY, None
            consecutive_failures = 0
            if self._on_plans is not None and ingestion.plans:
                await self._on_plans(guild_id, ingestion.plans)
        except Exception as exc:
            state, detail, retry_after = _classify_scheduler_failure(exc)
            consecutive_failures += 1
        interval = self._next_interval(account.platform, consecutive_failures, retry_after)
        next_poll_at = now + timedelta(seconds=interval)
        await self._store.save_content_schedule(
            ContentScheduleState(
                guild_id=guild_id,
                account_id=account.account_id,
                platform=account.platform,
                next_poll_at=next_poll_at,
                interval_seconds=interval,
                consecutive_failures=consecutive_failures,
                last_state=state,
                last_detail=detail,
                updated_at=now,
            )
        )
        self._results[(guild_id, account.platform)] = SchedulerResult(
            guild_id, account.platform, account.account_id, state, detail
        )

    def _next_interval(
        self, platform: Platform, consecutive_failures: int, retry_after: float | None
    ) -> int:
        """The next poll delay: exponential backoff with jitter, floored by
        whatever resume time the connector itself reported — this never produces a
        faster loop than the connector actually allows, even though today's
        connectors never report one (see `_classify_scheduler_failure`)."""
        base = self._intervals.get(platform, 300)
        backoff = (
            min(base * (2 ** min(consecutive_failures, 6)), _MAX_POLL_INTERVAL_SECONDS)
            if consecutive_failures > 0
            else base
        )
        if retry_after is not None:
            backoff = max(backoff, retry_after)
        return max(1, round(backoff * (1 + self._jitter())))
