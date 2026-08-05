"""Safe Discord execution for durable live-stream signal plans."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast

import discord

from krubit.discord.live_signals import (
    build_live_view,
    extract_twitch_observation,
    live_allowed_mentions,
    render_live_content,
    render_live_embed,
)
from krubit.domain.live_signals import (
    LiveSignalAction,
    LiveSignalConfig,
    LiveSignalPlan,
    StreamingObservation,
)
from krubit.integrations.youtube import normalize_youtube_video_url
from krubit.services.foundation import FoundationService
from krubit.services.live_signals import LiveSignalService
from krubit.storage.sqlite import SQLiteStore

LIVE_CHANNEL_NAME = "live-notifications"
STREAMING_ROLE_NAME = "Streaming Now"


@dataclass(frozen=True, slots=True)
class YouTubeStreamingPresence:
    """Presence-only detection of a validated YouTube live/watch streaming activity.

    Deliberately lighter-weight than Twitch's `StreamingObservation`: no durable
    session, role, or announcement pipeline consumes this yet — YouTube live delivery
    is carried entirely by the content ledger (`ContentSignalService`/`ContentRuntime`)
    fed by `YouTubeConnector`, not by this module's Twitch-specific session machinery.
    This type exists so `extract_streaming_observation` can honestly report "yes, this
    member's Rich Presence is a recognized YouTube stream" as groundwork for a future
    YouTube presence-session pipeline, without disturbing the existing Twitch one.
    """

    guild_id: int
    member_id: int
    video_id: str
    url: str
    activity_started_at: datetime | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if self.member_id <= 0:
            raise ValueError("member_id must be positive")
        if not self.video_id.strip():
            raise ValueError("video_id must not be blank")
        if not self.url.startswith("https://"):
            raise ValueError("url must use https")
        for timestamp in (self.activity_started_at, self.observed_at):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("timestamps must include a timezone")


def extract_streaming_observation(
    member: discord.Member, observed_at: datetime | None = None
) -> StreamingObservation | YouTubeStreamingPresence | None:
    """Detect a validated Twitch or YouTube streaming presence for `member`.

    Twitch detection delegates unchanged to `extract_twitch_observation` — the
    existing Twitch session/role/announcement pipeline in this module keeps calling
    that function directly and is completely unaffected by this wrapper. YouTube
    detection validates the streaming activity's URL as a canonical
    `youtube.com/watch?v=...` or `youtube.com/live/...` link and returns a
    `YouTubeStreamingPresence` — presence-only groundwork, not a durable session.
    """
    at = observed_at if observed_at is not None else datetime.now(UTC)
    twitch = extract_twitch_observation(member, observed_at=at)
    if twitch is not None:
        return twitch
    if member.bot:
        return None
    for activity in member.activities:
        if activity.type is not discord.ActivityType.streaming:
            continue
        url = getattr(activity, "url", None)
        if not isinstance(url, str):
            continue
        video_id = normalize_youtube_video_url(url)
        if video_id is None:
            continue
        started = getattr(activity, "start", None)
        started_at = (
            started
            if isinstance(started, datetime) and started.tzinfo is not None
            else None
        )
        return YouTubeStreamingPresence(
            guild_id=member.guild.id,
            member_id=member.id,
            video_id=video_id,
            url=url,
            activity_started_at=started_at,
            observed_at=at,
        )
    return None


class LiveSignalRuntime:
    """Execute only the Discord actions already approved by the signal service."""

    def __init__(
        self,
        store: SQLiteStore,
        signals: LiveSignalService | None,
        *,
        receipts: FoundationService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._signals = signals
        self._receipts = receipts
        self._now = now or (lambda: datetime.now(UTC))
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    async def configure_guild(self, guild: discord.Guild) -> LiveSignalConfig | None:
        """Find the exact bootstrap resources once, then trust persisted IDs."""
        if self._signals is None or not await self._store.guild_is_enabled(guild.id):
            return None
        async with self._guild_lock(guild.id):
            existing = await self._store.get_live_signal_config(guild.id)
            if existing is not None:
                return existing if self._configured_resources_are_usable(guild, existing) else None

            channel = next(
                (
                    candidate
                    for candidate in guild.channels
                    if getattr(candidate, "name", None) == LIVE_CHANNEL_NAME
                    and _is_text_channel(candidate)
                ),
                None,
            )
            role = next(
                (candidate for candidate in guild.roles if candidate.name == STREAMING_ROLE_NAME),
                None,
            )
            if channel is None or role is None:
                return None
            config = LiveSignalConfig(guild.id, channel.id, role.id, self._now())
            if not self._configured_resources_are_usable(guild, config):
                return None
            await self._store.set_live_signal_config(config)
            return config

    async def handle_presence(self, before: discord.Member, after: discord.Member) -> None:
        """Convert presence transitions into durable plans and execute them once."""
        if self._signals is None or before.guild.id != after.guild.id:
            return
        guild = after.guild
        if not await self._store.guild_is_enabled(guild.id):
            return
        if await self.configure_guild(guild) is None:
            return
        now = self._now()
        observation = extract_twitch_observation(after, observed_at=now)
        if observation is not None:
            if not await self.apply_plan(
                guild, await self._signals.begin_presence(observation, now=now)
            ):
                return
            await self.apply_plan(guild, await self._signals.enrich_presence(observation, now=now))
            return
        if extract_twitch_observation(before, observed_at=now) is not None:
            ending = await self._signals.presence_ended(guild.id, after.id, now=now)
            if ending is not None:
                await self.apply_plan(guild, ending)

    async def apply_plan(self, guild: discord.Guild, plan: LiveSignalPlan) -> bool:
        """Apply a plan idempotently; never infer resources from mutable names."""
        if self._signals is None or guild.id != plan.guild_id:
            return False
        if not await self._store.guild_is_enabled(guild.id):
            return False
        async with self._guild_lock(guild.id):
            config = await self._store.get_live_signal_config(guild.id)
            if config is None or not self._configured_resources_are_usable(guild, config):
                return False
            if LiveSignalAction.ENSURE_ROLE in plan.actions and not await self._ensure_role(
                guild, config, plan
            ):
                return False
            if LiveSignalAction.ANNOUNCE in plan.actions:
                await self._announce(guild, config, plan)
            if LiveSignalAction.EDIT_ANNOUNCEMENT in plan.actions:
                await self._edit_announcement(guild, config, plan)
            if LiveSignalAction.REMOVE_ROLE in plan.actions:
                await self._remove_owned_role(guild, config, plan)
        return True

    async def reconcile_guild(self, guild: discord.Guild) -> int:
        """Refresh one configured guild and execute its resulting plans."""
        if self._signals is None or not await self._store.guild_is_enabled(guild.id):
            return 0
        if await self.configure_guild(guild) is None:
            return 0
        recovered = await self._signals.recover_pending(guild.id)
        reconciled = await self._signals.reconcile(guild.id, now=self._now())
        plans = tuple(sorted((*recovered, *reconciled), key=_plan_order))
        blocked_sessions: set[str] = set()
        applied_count = 0
        for plan in plans:
            if plan.session_key in blocked_sessions:
                continue
            applied = await self.apply_plan(guild, plan)
            if applied:
                applied_count += 1
            if LiveSignalAction.ENSURE_ROLE in plan.actions and not applied:
                blocked_sessions.add(plan.session_key)
        return applied_count

    async def reconcile_all(self, guilds: Iterable[discord.Guild]) -> int:
        """Reconcile available guilds without creating independent background tasks."""
        count = 0
        for guild in guilds:
            count += await self.reconcile_guild(guild)
        return count

    async def bootstrap_guild(self, guild: discord.Guild) -> int:
        """Discover cached current streaming presences after an offline interval."""
        if await self.configure_guild(guild) is None:
            return 0
        for member in guild.members:
            await self.handle_presence(member, member)
        return await self.reconcile_guild(guild)

    async def handle_member_leave(self, member: discord.Member) -> None:
        """End member sessions without trying to mutate a departed Discord member."""
        if self._signals is None or not await self._store.guild_is_enabled(member.guild.id):
            return
        await self._signals.member_left(member.guild.id, member.id, now=self._now())

    def _configured_resources_are_usable(
        self, guild: discord.Guild, config: LiveSignalConfig
    ) -> bool:
        channel = guild.get_channel(config.channel_id)
        role = guild.get_role(config.role_id)
        if not _is_text_channel(channel) or role is None:
            return False
        text_channel = cast(_TextChannel, channel)
        permissions = text_channel.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
                getattr(guild.me.guild_permissions, "manage_roles", False),
            )
        ):
            return False
        if getattr(role, "managed", False):
            return False
        return _role_position(role) < _role_position(guild.me.top_role)

    async def _ensure_role(
        self, guild: discord.Guild, config: LiveSignalConfig, plan: LiveSignalPlan
    ) -> bool:
        if plan.member_id is None:
            return False
        signals = self._signals
        if signals is None:
            return False
        session = await self._store.get_live_session(guild.id, plan.session_key)
        if session is None:
            return False
        # A replayed durable plan must not turn an owned assignment into a
        # pre-existing role merely because the first execution already added it.
        if session.role_id == config.role_id:
            return True
        role = guild.get_role(config.role_id)
        member = guild.get_member(plan.member_id)
        if role is None or member is None or not self._role_is_usable(guild, role):
            return False
        member_roles = getattr(member, "roles", ())
        owned = not any(getattr(item, "id", None) == role.id for item in member_roles)
        try:
            if owned:
                await member.add_roles(role, reason=_reason(plan.session_key))
            await signals.record_role_result(
                guild.id,
                plan.session_key,
                role_id=role.id,
                assigned_by_krubit=owned,
                status="succeeded",
            )
        except (
            discord.HTTPException,
            discord.Forbidden,
            discord.NotFound,
            PermissionError,
            ValueError,
        ):
            with suppress(ValueError):
                await signals.record_role_result(
                    guild.id,
                    plan.session_key,
                    role_id=config.role_id,
                    assigned_by_krubit=False,
                    status="failed",
                )
            return False
        return True

    async def _remove_owned_role(
        self, guild: discord.Guild, config: LiveSignalConfig, plan: LiveSignalPlan
    ) -> None:
        if plan.member_id is None or plan.role_id != config.role_id:
            return
        signals = self._signals
        if signals is None:
            return
        session = await self._store.get_live_session(guild.id, plan.session_key)
        role = guild.get_role(config.role_id)
        member = guild.get_member(plan.member_id)
        if (
            session is None
            or not session.role_assigned_by_krubit
            or session.role_id != config.role_id
            or role is None
            or member is None
            or not self._role_is_usable(guild, role)
        ):
            return
        if not any(getattr(item, "id", None) == role.id for item in getattr(member, "roles", ())):
            await signals.record_role_result(
                guild.id,
                plan.session_key,
                role_id=role.id,
                assigned_by_krubit=False,
                status="succeeded",
            )
            return
        try:
            await member.remove_roles(role, reason=_reason(plan.session_key))
            await signals.record_role_result(
                guild.id,
                plan.session_key,
                role_id=role.id,
                assigned_by_krubit=False,
                status="succeeded",
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
            return

    async def _announce(
        self, guild: discord.Guild, config: LiveSignalConfig, plan: LiveSignalPlan
    ) -> None:
        if plan.delivery_attempt is None:
            return
        signals = self._signals
        if signals is None:
            return
        session = await self._store.get_live_session(guild.id, plan.session_key)
        channel = guild.get_channel(config.channel_id)
        if (
            session is None
            or session.announcement_message_id is not None
            or not _is_text_channel(channel)
        ):
            return
        text_channel = cast(_TextChannel, channel)
        delivery_key = (
            f"stream:{session.stream.stream_id}"
            if session.stream is not None
            else f"provisional:{session.session_key}"
        )
        delivery = await self._store.get_live_delivery(guild.id, delivery_key)
        if (
            delivery is None
            or delivery.status != "claimed"
            or delivery.attempt != plan.delivery_attempt
            or delivery.session_key != plan.session_key
        ):
            return
        permissions = text_channel.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
            )
        ):
            await self._record_delivery_failure(config, plan)
            return
        nonce = _delivery_nonce(guild.id, delivery_key, plan.delivery_attempt)
        if plan.recovery:
            try:
                recovered_message = await _find_nonce_message(text_channel, guild.me, nonce)
            except (discord.Forbidden, discord.HTTPException):
                return
            if recovered_message is not None:
                try:
                    await signals.record_delivery_result(
                        guild.id,
                        plan.session_key,
                        status="succeeded",
                        channel_id=config.channel_id,
                        message_id=recovered_message.id,
                        attempt=plan.delivery_attempt,
                    )
                except ValueError:
                    return
                return
        member = guild.get_member(session.member_id)
        display_name = getattr(member, "display_name", session.twitch_login)
        if not isinstance(display_name, str):
            display_name = session.twitch_login
        observation = StreamingObservation(
            guild_id=guild.id,
            member_id=session.member_id,
            twitch_login=session.twitch_login,
            twitch_url=session.twitch_url,
            activity_started_at=session.presence_started_at,
            observed_at=self._now(),
        )
        degraded = not getattr(permissions, "mention_everyone", False)
        mentions = discord.AllowedMentions.none() if degraded else live_allowed_mentions()
        try:
            message = await text_channel.send(
                content=render_live_content(display_name),
                embed=render_live_embed(observation, plan.stream),
                view=build_live_view(session.twitch_url),
                allowed_mentions=mentions,
                nonce=nonce,
            )
            await signals.record_delivery_result(
                guild.id,
                plan.session_key,
                status="succeeded",
                channel_id=config.channel_id,
                message_id=message.id,
                attempt=plan.delivery_attempt,
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
            await self._record_delivery_failure(config, plan)
            return
        if degraded and self._receipts is not None:
            await self._receipts.record_action(
                guild.id,
                action="live_signal_announcement",
                status="degraded",
                actor_id=None,
                detail={"channel_id": config.channel_id, "reason": "mention_everyone_missing"},
            )

    async def _record_delivery_failure(
        self, config: LiveSignalConfig, plan: LiveSignalPlan
    ) -> None:
        if plan.delivery_attempt is None:
            return
        signals = self._signals
        if signals is None:
            return
        try:
            await signals.record_delivery_result(
                plan.guild_id,
                plan.session_key,
                status="failed",
                channel_id=config.channel_id,
                message_id=None,
                attempt=plan.delivery_attempt,
            )
        except ValueError:
            return

    async def _edit_announcement(
        self, guild: discord.Guild, config: LiveSignalConfig, plan: LiveSignalPlan
    ) -> None:
        if (
            plan.announcement_channel_id != config.channel_id
            or plan.announcement_message_id is None
        ):
            return
        session = await self._store.get_live_session(guild.id, plan.session_key)
        channel = guild.get_channel(config.channel_id)
        if session is None or not _is_text_channel(channel):
            return
        text_channel = cast(_TextChannel, channel)
        observation = StreamingObservation(
            guild_id=guild.id,
            member_id=session.member_id,
            twitch_login=session.twitch_login,
            twitch_url=session.twitch_url,
            activity_started_at=session.presence_started_at,
            observed_at=self._now(),
        )
        try:
            message = await text_channel.fetch_message(plan.announcement_message_id)
            await message.edit(
                embed=render_live_embed(observation, plan.stream),
                view=build_live_view(session.twitch_url),
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, KeyError):
            return

    @staticmethod
    def _role_is_usable(guild: discord.Guild, role: discord.Role) -> bool:
        return not getattr(role, "managed", False) and _role_position(role) < _role_position(
            guild.me.top_role
        ) and bool(getattr(guild.me.guild_permissions, "manage_roles", False))


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


def _role_position(role: object) -> int:
    position = getattr(role, "position", -1)
    return position if isinstance(position, int) else -1


def _reason(session_key: str) -> str:
    """Keep audit reasons non-secret and bounded to a durable session identity."""
    return f"Krubit live signal {session_key[:64]}"


def _delivery_nonce(guild_id: int, delivery_key: str, attempt: int) -> str:
    return sha256(f"{guild_id}:{delivery_key}:{attempt}".encode()).hexdigest()[:25]


def _plan_order(plan: LiveSignalPlan) -> tuple[str, int]:
    priority = 0 if LiveSignalAction.ENSURE_ROLE in plan.actions else 1
    return plan.session_key, priority


async def _find_nonce_message(
    channel: _TextChannel, bot_member: object, nonce: str
) -> discord.Message | None:
    """Bound recovery scan; discord.py exposes nonce but no enforce_nonce switch."""
    async for message in channel.history(limit=25):
        if (
            getattr(getattr(message, "author", None), "id", None) == getattr(bot_member, "id", None)
            and getattr(message, "nonce", None) == nonce
        ):
            return message
    return None
