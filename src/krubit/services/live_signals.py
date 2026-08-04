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

_INITIAL_LOOKUP_TIMEOUT_SECONDS = 5
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

    async def observe(
        self, observation: StreamingObservation, *, now: datetime
    ) -> LiveSignalPlan:
        """Observe a Discord streaming presence and persist its next idempotent actions."""
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
            return LiveSignalPlan(saved.guild_id, saved.session_key, (), saved.stream)

        lookup = await self._initial_lookup(observation.twitch_login)
        session = self._observed_session(existing, observation, lookup, now)
        saved = await self._store.save_live_session(session)
        if lookup.kind is TwitchLookupKind.OFFLINE:
            return LiveSignalPlan(saved.guild_id, saved.session_key, (), saved.stream)

        if existing is not None:
            retry_claimed = False
            if existing.stream is None and saved.stream is not None:
                await self._store.merge_live_delivery_identity(
                    saved.guild_id,
                    _delivery_key(existing),
                    _delivery_key(saved),
                    saved.session_key,
                )
                retry_claimed = await self._store.claim_live_delivery(
                    saved.guild_id, _delivery_key(saved), saved.session_key
                )
            actions: tuple[LiveSignalAction, ...] = ()
            if saved.announcement_message_id is not None and lookup.kind is TwitchLookupKind.LIVE:
                actions = (LiveSignalAction.EDIT_ANNOUNCEMENT,)
            elif retry_claimed:
                actions = (LiveSignalAction.ANNOUNCE,)
            return LiveSignalPlan(saved.guild_id, saved.session_key, actions, saved.stream)

        claimed = await self._store.claim_live_delivery(
            saved.guild_id, _delivery_key(saved), saved.session_key
        )
        actions = (LiveSignalAction.ENSURE_ROLE, LiveSignalAction.ANNOUNCE) if claimed else ()
        return LiveSignalPlan(saved.guild_id, saved.session_key, actions, saved.stream)

    async def presence_ended(
        self, guild_id: int, member_id: int, *, now: datetime
    ) -> LiveSignalPlan | None:
        """Mark Discord presence unavailable; Helix reconciliation decides the final end."""
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
        return LiveSignalPlan(saved.guild_id, saved.session_key, (), saved.stream)

    async def reconcile(self, guild_id: int, *, now: datetime) -> tuple[LiveSignalPlan, ...]:
        """Refresh every active session using caller-provided time and no sleeps."""
        _require_aware("now", now)
        plans: list[LiveSignalPlan] = []
        for session in await self._store.list_active_live_sessions(guild_id):
            lookup = await self._lookup(session.twitch_login)
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
                    and (
                        session.status is not LiveSignalStatus.LIVE
                        or session.stream != saved.stream
                    )
                ):
                    plans.append(
                        LiveSignalPlan(
                            saved.guild_id,
                            saved.session_key,
                            (LiveSignalAction.EDIT_ANNOUNCEMENT,),
                            saved.stream,
                        )
                    )
                continue
            if lookup.kind is TwitchLookupKind.OFFLINE:
                saved = await self._end(session, now)
                plan = self._remove_role_plan(saved)
                if plan is not None:
                    plans.append(plan)
                continue
            if session.presence_active:
                await self._store.save_live_session(session)
                continue
            missing_since = session.missing_since or now
            if now - missing_since < _MISSING_EVIDENCE_GRACE:
                await self._store.save_live_session(
                    replace(session, status=LiveSignalStatus.ENDING, missing_since=missing_since)
                )
                continue
            saved = await self._end(session, now)
            plan = self._remove_role_plan(saved)
            if plan is not None:
                plans.append(plan)
        return tuple(plans)

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
        _require_result_status(status)
        if role_id <= 0:
            raise ValueError("role_id must be positive")
        if type(assigned_by_krubit) is not bool:
            raise ValueError("assigned_by_krubit must be a bool")
        session = await self._required_session(guild_id, session_key)
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
    ) -> None:
        """Complete the claimed delivery and preserve the message identity for edits."""
        _require_result_status(status)
        if channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if message_id is not None and message_id <= 0:
            raise ValueError("message_id must be positive")
        if status == "succeeded" and message_id is None:
            raise ValueError("message_id is required for a succeeded delivery")
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
        if delivery.status == "succeeded" and status != "succeeded":
            raise ValueError("announcement delivery is already recorded")
        await self._store.complete_live_delivery(
            guild_id,
            _delivery_key(session),
            status=status,
            channel_id=channel_id,
            message_id=message_id,
        )
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
        sessions = await self.status(guild_id)
        return "limited" if any(item.last_twitch_at is None for item in sessions) else "healthy"

    async def _initial_lookup(self, login: str) -> TwitchLookup:
        try:
            async with asyncio.timeout(_INITIAL_LOOKUP_TIMEOUT_SECONDS):
                return await self._twitch.get_stream(login)
        except TimeoutError:
            return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="timeout")
        except Exception:
            return TwitchLookup(TwitchLookupKind.UNAVAILABLE, unavailable_reason="unavailable")

    async def _lookup(self, login: str) -> TwitchLookup:
        try:
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
    def _remove_role_plan(session: LiveSignalSession) -> LiveSignalPlan | None:
        if not session.role_assigned_by_krubit:
            return None
        return LiveSignalPlan(
            session.guild_id,
            session.session_key,
            (LiveSignalAction.REMOVE_ROLE,),
            session.stream,
        )

    async def _required_session(self, guild_id: int, session_key: str) -> LiveSignalSession:
        session = await self._store.get_live_session(guild_id, session_key)
        if session is None:
            raise ValueError("session does not exist")
        return session
