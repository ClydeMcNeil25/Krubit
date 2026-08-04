"""Durable, framework-independent live-stream state reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from krubit.domain.live_signals import (
    LiveSignalAction,
    LiveSignalPlan,
    LiveSignalSession,
    LiveSignalStatus,
    StreamingObservation,
    TwitchLookup,
    TwitchLookupKind,
    provisional_session_key,
)
from krubit.domain.models import JSONValue
from krubit.integrations.twitch import TwitchClient
from krubit.storage.sqlite import SQLiteStore

_TWITCH_LOOKUP_TIMEOUT_SECONDS = 5
_MISSING_EVIDENCE_GRACE = timedelta(minutes=5)
_RESULT_STATUSES = frozenset({"succeeded", "failed"})


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _require_result_status(status: str) -> None:
    if status not in _RESULT_STATUSES:
        raise ValueError("status must be succeeded or failed")


def _delivery_key(session: LiveSignalSession) -> str:
    if session.stream is not None:
        return f"stream:{session.stream.stream_id}"
    return f"provisional:{session.session_key}"


class LiveSignalService:
    """Own the state machine while Discord adapters execute its durable plans."""

    def __init__(self, store: SQLiteStore, twitch: TwitchClient) -> None:
        self._store = store
        self._twitch = twitch
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    async def observe(
        self, observation: StreamingObservation, *, now: datetime
    ) -> LiveSignalPlan:
        """Compatibility helper that begins durable state before enrichment."""
        begun = await self.begin_presence(observation, now=now)
        enriched = await self.enrich_presence(observation, now=now)
        enrichment_actions = tuple(
            action for action in enriched.actions if action not in begun.actions
        )
        actions = begun.actions + enrichment_actions
        return LiveSignalPlan(
            guild_id=enriched.guild_id,
            session_key=enriched.session_key,
            actions=actions,
            stream=enriched.stream,
            member_id=enriched.member_id,
            role_id=enriched.role_id,
            announcement_channel_id=enriched.announcement_channel_id,
            announcement_message_id=enriched.announcement_message_id,
            delivery_attempt=enriched.delivery_attempt,
        )

    async def begin_presence(
        self, observation: StreamingObservation, *, now: datetime
    ) -> LiveSignalPlan:
        """Persist a provisional session and role plan without waiting for Twitch."""
        async with self._guild_lock(observation.guild_id):
            _require_aware("now", now)
            existing = await self._store.open_live_session(
                observation.guild_id, observation.member_id, observation.twitch_login
            )
            if existing is not None:
                saved = await self._store.save_live_session(
                    replace(existing, presence_active=True, missing_since=None, last_discord_at=now)
                )
                return self._plan(saved, ())
            transferred_role_id: int | None = None
            transferred_owned = False
            for active in await self._store.list_member_live_sessions(
                observation.guild_id, observation.member_id
            ):
                if active.twitch_login == observation.twitch_login:
                    continue
                if active.role_assigned_by_krubit:
                    transferred_role_id = active.role_id
                    transferred_owned = True
                await self._store.save_live_session(
                    replace(
                        active,
                        status=LiveSignalStatus.ENDED,
                        presence_active=False,
                        role_assigned_by_krubit=False,
                        ended_at=now,
                    )
                )
            provisional = LiveSignalSession(
                guild_id=observation.guild_id,
                session_key=provisional_session_key(observation),
                member_id=observation.member_id,
                twitch_login=observation.twitch_login,
                twitch_url=observation.twitch_url,
                status=LiveSignalStatus.DETECTED,
                detected_at=now,
                presence_started_at=observation.activity_started_at,
                role_id=transferred_role_id,
                role_assigned_by_krubit=transferred_owned,
                presence_active=True,
                last_discord_at=now,
            )
            saved = await self._store.save_live_session(provisional)
            return self._plan(
                saved,
                () if saved.role_assigned_by_krubit else (LiveSignalAction.ENSURE_ROLE,),
            )

    async def enrich_presence(
        self, observation: StreamingObservation, *, now: datetime
    ) -> LiveSignalPlan:
        """Enrich a previously durable presence with bounded Twitch evidence."""
        async with self._guild_lock(observation.guild_id):
            return await self._observe(observation, now=now)

    async def _observe(
        self, observation: StreamingObservation, *, now: datetime
    ) -> LiveSignalPlan:
        _require_aware("now", now)
        existing = await self._store.open_live_session(
            observation.guild_id, observation.member_id, observation.twitch_login
        )
        if existing is not None and existing.stream is not None:
            saved = await self._store.save_live_session(
                replace(
                    existing,
                    presence_active=True,
                    missing_since=None,
                    last_discord_at=now,
                )
            )
            return self._plan(saved, ())

        lookup = await self._initial_lookup(observation.twitch_login)
        session = self._observed_session(existing, observation, lookup, now)
        saved = await self._store.save_live_session(session)
        await self._record_lookup(saved, lookup, now)
        if lookup.kind is TwitchLookupKind.OFFLINE:
            return self._remove_role_plan(saved) or self._plan(saved, ())

        if existing is not None:
            retry_attempt: int | None = None
            if existing.stream is None and saved.stream is not None:
                retry_attempt = await self._store.merge_live_delivery_identity(
                    saved.guild_id,
                    _delivery_key(existing),
                    _delivery_key(saved),
                    saved.session_key,
                )
                if retry_attempt is None:
                    retry_attempt = await self._store.claim_live_delivery_attempt(
                        saved.guild_id, _delivery_key(saved), saved.session_key
                    )
            elif saved.stream is None:
                retry_attempt = await self._store.claim_live_delivery_attempt(
                    saved.guild_id, _delivery_key(saved), saved.session_key
                )
            actions: tuple[LiveSignalAction, ...] = ()
            if saved.announcement_message_id is not None and lookup.kind is TwitchLookupKind.LIVE:
                actions = (LiveSignalAction.EDIT_ANNOUNCEMENT,)
            elif retry_attempt is not None:
                actions = (LiveSignalAction.ANNOUNCE,)
            return self._plan(saved, actions, delivery_attempt=retry_attempt)

        attempt = await self._store.claim_live_delivery_attempt(
            saved.guild_id, _delivery_key(saved), saved.session_key
        )
        actions = (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE) if attempt else ()
        return self._plan(saved, actions, delivery_attempt=attempt)

    async def presence_ended(
        self, guild_id: int, member_id: int, *, now: datetime
    ) -> LiveSignalPlan | None:
        """Mark Discord presence unavailable; Helix reconciliation decides the final end."""
        async with self._guild_lock(guild_id):
            return await self._presence_ended(guild_id, member_id, now=now)

    async def member_left(self, guild_id: int, member_id: int, *, now: datetime) -> None:
        """Force-close sessions for a departed member without a Discord role action."""
        _require_aware("now", now)
        async with self._guild_lock(guild_id):
            for session in await self._store.list_active_live_sessions(guild_id):
                if session.member_id == member_id:
                    await self._store.save_live_session(
                        replace(
                            session,
                            status=LiveSignalStatus.ENDED,
                            presence_active=False,
                            role_assigned_by_krubit=False,
                            ended_at=now,
                        )
                    )

    async def recover_pending(self, guild_id: int) -> tuple[LiveSignalPlan, ...]:
        """Rebuild runtime-only execution plans from durable claimed deliveries."""
        async with self._guild_lock(guild_id):
            plans: list[LiveSignalPlan] = []
            for session in await self._store.list_active_live_sessions(guild_id):
                if session.stream is None and session.announcement_message_id is None:
                    actions = (
                        (LiveSignalAction.ENSURE_ROLE,)
                        if session.role_id is None
                        else ()
                    )
                    if actions:
                        plans.append(self._plan(session, actions, recovery=True))
            for delivery in await self._store.list_claimed_live_deliveries(guild_id):
                session = await self._store.get_live_session(guild_id, delivery.session_key)
                if (
                    session is None
                    or session.status is LiveSignalStatus.ENDED
                    or session.ended_at is not None
                ):
                    continue
                actions: tuple[LiveSignalAction, ...] = (LiveSignalAction.ANNOUNCE,)
                if session.role_id is None:
                    actions = (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE)
                plans.append(
                    self._plan(session, actions, delivery_attempt=delivery.attempt, recovery=True)
                )
            return tuple(plans)

    async def _presence_ended(
        self, guild_id: int, member_id: int, *, now: datetime
    ) -> LiveSignalPlan | None:
        _require_aware("now", now)
        sessions = await self._store.list_active_live_sessions(guild_id)
        session = next((item for item in sessions if item.member_id == member_id), None)
        if session is None:
            return None
        saved = await self._store.save_live_session(
            replace(
                session,
                status=LiveSignalStatus.ENDING,
                presence_active=False,
                missing_since=session.missing_since or now,
            )
        )
        return self._plan(saved, ())

    async def reconcile(self, guild_id: int, *, now: datetime) -> tuple[LiveSignalPlan, ...]:
        """Refresh active sessions without holding the guild lock across Twitch I/O."""
        _require_aware("now", now)
        async with self._guild_lock(guild_id):
            snapshots = tuple(await self._store.list_active_live_sessions(guild_id))
        plans: list[LiveSignalPlan] = []
        for snapshot in snapshots:
            lookup = await self._lookup(snapshot.twitch_login)
            async with self._guild_lock(guild_id):
                current = await self._store.get_live_session(guild_id, snapshot.session_key)
                if (
                    current is None
                    or current.status is LiveSignalStatus.ENDED
                    or current.ended_at is not None
                    or current.twitch_login != snapshot.twitch_login
                ):
                    continue
                plan = await self._apply_reconciliation_lookup(current, lookup, now)
                if plan is not None:
                    plans.append(plan)
        return tuple(plans)

    async def _apply_reconciliation_lookup(
        self, session: LiveSignalSession, lookup: TwitchLookup, now: datetime
    ) -> LiveSignalPlan | None:
        await self._record_lookup(session, lookup, now)
        if lookup.kind is TwitchLookupKind.LIVE:
            saved = await self._store.save_live_session(
                replace(
                    session,
                    status=LiveSignalStatus.LIVE,
                    stream=lookup.stream,
                    missing_since=None,
                    last_twitch_at=now,
                )
            )
            if (
                saved.announcement_message_id is not None
                and (session.status is not LiveSignalStatus.LIVE or session.stream != saved.stream)
            ):
                return self._plan(saved, (LiveSignalAction.EDIT_ANNOUNCEMENT,))
            if saved.announcement_message_id is None:
                attempt = await self._store.claim_live_delivery_attempt(
                    saved.guild_id, _delivery_key(saved), saved.session_key
                )
                if attempt is not None:
                    return self._plan(saved, (LiveSignalAction.ANNOUNCE,), delivery_attempt=attempt)
            return None
        if lookup.kind is TwitchLookupKind.OFFLINE:
            return self._remove_role_plan(await self._end(session, now))
        if session.presence_active:
            await self._store.save_live_session(session)
            return None
        missing_since = session.missing_since or now
        if now - missing_since < _MISSING_EVIDENCE_GRACE:
            await self._store.save_live_session(
                replace(session, status=LiveSignalStatus.ENDING, missing_since=missing_since)
            )
            return None
        return self._remove_role_plan(await self._end(session, now))

    async def record_role_result(
        self,
        guild_id: int,
        session_key: str,
        *,
        role_id: int,
        assigned_by_krubit: bool,
        status: str,
    ) -> None:
        """Durably retain role ownership so only Krubit-owned roles may be removed."""
        async with self._guild_lock(guild_id):
            await self._record_role_result(
                guild_id,
                session_key,
                role_id=role_id,
                assigned_by_krubit=assigned_by_krubit,
                status=status,
            )

    async def _record_role_result(
        self,
        guild_id: int,
        session_key: str,
        *,
        role_id: int,
        assigned_by_krubit: bool,
        status: str,
    ) -> None:
        _require_result_status(status)
        if role_id <= 0:
            raise ValueError("role_id must be positive")
        if type(assigned_by_krubit) is not bool:
            raise ValueError("assigned_by_krubit must be a bool")
        config = await self._store.get_live_signal_config(guild_id)
        if config is None or config.role_id != role_id:
            raise ValueError("role_id must match the configured streaming role")
        session = await self._required_session(guild_id, session_key)
        if session.status is LiveSignalStatus.ENDED or session.ended_at is not None:
            return
        if status == "failed":
            await self._store.save_live_session(replace(session, status=LiveSignalStatus.FAILED))
            return
        await self._store.save_live_session(
            replace(
                session,
                role_id=role_id,
                role_assigned_by_krubit=assigned_by_krubit,
            )
        )

    async def record_delivery_result(
        self,
        guild_id: int,
        session_key: str,
        *,
        status: str,
        channel_id: int,
        message_id: int | None,
        attempt: int,
    ) -> None:
        """Complete the claimed delivery and preserve the message identity for edits."""
        async with self._guild_lock(guild_id):
            await self._record_delivery_result(
                guild_id,
                session_key,
                status=status,
                channel_id=channel_id,
                message_id=message_id,
                attempt=attempt,
            )

    async def _record_delivery_result(
        self,
        guild_id: int,
        session_key: str,
        *,
        status: str,
        channel_id: int,
        message_id: int | None,
        attempt: int,
    ) -> None:
        _require_result_status(status)
        if channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if message_id is not None and message_id <= 0:
            raise ValueError("message_id must be positive")
        if status == "succeeded" and message_id is None:
            raise ValueError("message_id is required for a succeeded delivery")
        if attempt <= 0:
            raise ValueError("attempt must be positive")
        config = await self._store.get_live_signal_config(guild_id)
        if config is None or config.channel_id != channel_id:
            raise ValueError("channel_id must match the configured notification channel")
        session = await self._required_session(guild_id, session_key)
        if (
            status == "succeeded"
            and session.announcement_message_id is not None
            and session.announcement_message_id != message_id
        ):
            raise ValueError("announcement delivery is already recorded")
        delivery = await self._store.get_live_delivery(guild_id, _delivery_key(session))
        if delivery is None:
            raise ValueError("delivery claim does not exist")
        if delivery.attempt != attempt:
            raise ValueError("stale delivery attempt")
        if delivery.status == "succeeded" and status != "succeeded":
            raise ValueError("announcement delivery is already recorded")
        completed = await self._store.complete_live_delivery(
            guild_id,
            _delivery_key(session),
            status=status,
            channel_id=channel_id,
            message_id=message_id,
            attempt=attempt,
        )
        if not completed:
            raise ValueError("stale delivery attempt")
        if status == "succeeded":
            await self._store.save_live_session(
                replace(
                    session,
                    announcement_channel_id=channel_id,
                    announcement_message_id=message_id,
                )
            )

    async def status(self, guild_id: int) -> tuple[LiveSignalSession, ...]:
        """Return guild-scoped active sessions for factual staff status rendering."""
        return tuple(await self._store.list_active_live_sessions(guild_id))

    async def integration_health(self, guild_id: int) -> str:
        """Classify Twitch evidence without guessing at user-facing remediation."""
        latest = await self._store.latest_live_check_result(guild_id)
        if latest == TwitchLookupKind.UNAVAILABLE.value:
            return "limited"
        sessions = await self.status(guild_id)
        no_successful_check = latest is None and any(
            item.last_twitch_at is None for item in sessions
        )
        return "limited" if no_successful_check else "healthy"

    async def _initial_lookup(self, login: str) -> TwitchLookup:
        try:
            async with asyncio.timeout(_TWITCH_LOOKUP_TIMEOUT_SECONDS):
                return await self._twitch.get_stream(login)
        except TimeoutError:
            return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
        except Exception:
            return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="unavailable")

    async def _lookup(self, login: str) -> TwitchLookup:
        try:
            async with asyncio.timeout(_TWITCH_LOOKUP_TIMEOUT_SECONDS):
                return await self._twitch.get_stream(login)
        except Exception:
            return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="unavailable")

    @staticmethod
    def _observed_session(
        existing: LiveSignalSession | None,
        observation: StreamingObservation,
        lookup: TwitchLookup,
        now: datetime,
    ) -> LiveSignalSession:
        if lookup.kind is TwitchLookupKind.OFFLINE:
            status = LiveSignalStatus.ENDED
            ended_at = now
        elif lookup.kind is TwitchLookupKind.LIVE:
            status = LiveSignalStatus.LIVE
            ended_at = None
        else:
            status = LiveSignalStatus.DETECTED
            ended_at = None
        if existing is None:
            return LiveSignalSession(
                guild_id=observation.guild_id,
                session_key=provisional_session_key(observation),
                member_id=observation.member_id,
                twitch_login=observation.twitch_login,
                twitch_url=observation.twitch_url,
                status=status,
                detected_at=now,
                presence_started_at=observation.activity_started_at,
                stream=lookup.stream,
                presence_active=True,
                last_discord_at=now,
                last_twitch_at=now if lookup.kind is TwitchLookupKind.LIVE else None,
                ended_at=ended_at,
            )
        return replace(
            existing,
            status=status,
            stream=lookup.stream,
            presence_active=True,
            missing_since=None,
            last_discord_at=now,
            last_twitch_at=now if lookup.kind is TwitchLookupKind.LIVE else existing.last_twitch_at,
            ended_at=ended_at,
        )

    async def _record_lookup(
        self, session: LiveSignalSession, lookup: TwitchLookup, now: datetime
    ) -> None:
        detail: dict[str, JSONValue] = (
            {"reason": lookup.unavailable_reason} if lookup.unavailable_reason else {}
        )
        await self._store.record_live_check(
            session.guild_id,
            f"twitch:{session.session_key}:{now.isoformat()}",
            session.session_key,
            result=lookup.kind.value,
            detail=detail,
            checked_at=now,
        )

    async def _end(self, session: LiveSignalSession, now: datetime) -> LiveSignalSession:
        return await self._store.save_live_session(
            replace(
                session,
                status=LiveSignalStatus.ENDED,
                presence_active=False,
                ended_at=now,
            )
        )

    @staticmethod
    def _plan(
        session: LiveSignalSession,
        actions: tuple[LiveSignalAction, ...],
        *,
        delivery_attempt: int | None = None,
        recovery: bool = False,
    ) -> LiveSignalPlan:
        return LiveSignalPlan(
            guild_id=session.guild_id,
            session_key=session.session_key,
            actions=actions,
            stream=session.stream,
            member_id=session.member_id,
            role_id=session.role_id,
            announcement_channel_id=session.announcement_channel_id,
            announcement_message_id=session.announcement_message_id,
            delivery_attempt=delivery_attempt,
            recovery=recovery,
        )

    @staticmethod
    def _remove_role_plan(session: LiveSignalSession) -> LiveSignalPlan | None:
        if not session.role_assigned_by_krubit:
            return None
        return LiveSignalService._plan(session, (LiveSignalAction.REMOVE_ROLE,))

    async def _required_session(self, guild_id: int, session_key: str) -> LiveSignalSession:
        session = await self._store.get_live_session(guild_id, session_key)
        if session is None:
            raise ValueError("session does not exist")
        return session
