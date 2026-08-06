"""Phase 4 activity-ledger extraction: pure Discord-object-to-`LedgerEvent` mapping.

Mirrors `krubit.discord.watchdog_events`'s established convention: every
`extract_*` function here takes already-fetched Discord-shaped objects (never
fetches anything itself), a caller-supplied clock reading (`now`), and returns a
domain value object or `None` for an excluded case. There is no allow/block-list
consultation here (channel exclusion is `krubit.services.activity_ingestion.
ActivityIngestionService`'s job, applied *after* extraction and *before* any
storage write — see that module) and no content is ever read, matching the design
doc's "factual participation events, not content" requirement: no function below
touches `message.content`, no reaction handler infers sentiment from an emoji
beyond its shape, and no voice function reads audio or a transcript.

## DM exclusion (double-gate discipline)

The design doc requires DMs be "structurally excluded from ingestion at every
entry point — the same double-gate discipline Phase 3 established for message
content (`guild is None` checked both at the dispatch site and again inside the
consuming service)." This module is the *second* gate: `extract_message_event`
and `extract_reaction_event` both return `None` whenever the underlying Discord
object carries no guild (a DM), exactly like `extract_join_signals`/
`extract_message_signals` never being invoked for DMs in the first place is the
*first* gate at the dispatch site (a later task's cog-wiring concern).
`extract_voice_session_event` and `extract_attendance_event` have no DM
equivalent — Discord does not deliver `GUILD_VOICE_STATES`/
`GUILD_SCHEDULED_EVENTS` gateway events for DM contexts a bot can act on — so
those two only guard against internally inconsistent input (see their
docstrings), not DMs specifically.

## Why `extract_voice_session_event` does not take raw `discord.VoiceState`

`discord.VoiceState` (the type `on_voice_state_update`'s `before`/`after`
parameters actually carry) has neither a member reference nor a timestamp on
its own — a bare gateway voice-state transition cannot, by itself, supply the
join instant a completed `VoiceSessionEvent` needs. This module deliberately
defines its own narrow `VoiceStateSubject` protocol instead (member/guild/
channel IDs plus an `occurred_at` reading) and expects the *caller* — a later
task's voice-session runtime — to pair a join-time snapshot with a leave-time
snapshot once a complete session is known, exactly the way this module never
performs its own state-tracking I/O. `extract_attendance_event` similarly does
not use a single official discord.py "raw" type: Discord delivers Scheduled
Event RSVP add/remove as two separate callbacks
(`on_scheduled_event_user_add`/`_remove`), each carrying a `discord.ScheduledEvent`
and a `discord.User`/`discord.Member`; the caller is responsible for combining
those into one `AttendancePayloadSubject`-shaped snapshot (including which
action fired) before calling this function, matching the reaction payload's
already-combined shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from krubit.domain.activity_ledger import (
    AttendanceAction,
    EventAttendanceEvent,
    MessageEvent,
    ReactionEvent,
    VoiceSessionEvent,
)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


class _IdentifiedSubject(Protocol):
    @property
    def id(self) -> int: ...


class _GuildSubject(Protocol):
    @property
    def id(self) -> int: ...


class _ChannelSubject(Protocol):
    @property
    def id(self) -> int: ...


@runtime_checkable
class _ThreadSubject(Protocol):
    """The extra shape a `discord.Thread` channel carries beyond `_ChannelSubject`.

    `parent_id` is present on `discord.Thread` and absent on an ordinary
    `discord.TextChannel`/`discord.VoiceChannel`, so a runtime `isinstance`
    check against this protocol is exactly "is this message's channel a
    thread" without needing a `discord`-specific import here.
    """

    @property
    def id(self) -> int: ...
    @property
    def parent_id(self) -> int | None: ...


class MessageSubject(Protocol):
    """Structural shape `extract_message_event` needs from a guild text message.

    Deliberately narrow, matching `watchdog_events.MessageSubject`'s convention:
    only fields already present on `discord.Message`. `guild` is `None` for DMs
    (the second DM gate — see module docstring); `content` is never named here at
    all, since this function must never be able to read it.
    """

    @property
    def guild(self) -> _GuildSubject | None: ...
    @property
    def author(self) -> _IdentifiedSubject: ...
    @property
    def channel(self) -> _ChannelSubject: ...


def extract_message_event(message: MessageSubject, now: datetime) -> MessageEvent | None:
    """Deterministically extract a `MessageEvent` from one guild message, or `None`
    for a DM. Pure: no I/O, no content read, no exclusion-list consultation.
    """
    _require_aware("now", now)
    if message.guild is None:
        return None

    channel = message.channel
    if isinstance(channel, _ThreadSubject) and channel.parent_id is not None:
        channel_id = channel.parent_id
        thread_id: int | None = channel.id
    else:
        channel_id = channel.id
        thread_id = None

    return MessageEvent(
        guild_id=message.guild.id,
        member_id=message.author.id,
        occurred_at=now,
        channel_id=channel_id,
        thread_id=thread_id,
    )


class ReactionPayloadSubject(Protocol):
    """Structural shape `extract_reaction_event` needs, matching the fields
    already present on `discord.RawReactionActionEvent` (no fetch of the
    reacted-to message or its content). `guild_id` is `None` for a DM reaction.
    """

    @property
    def guild_id(self) -> int | None: ...
    @property
    def user_id(self) -> int: ...
    @property
    def channel_id(self) -> int: ...
    @property
    def emoji(self) -> object: ...


def extract_reaction_event(payload: ReactionPayloadSubject, now: datetime) -> ReactionEvent | None:
    """Deterministically extract a `ReactionEvent` from one raw reaction payload, or
    `None` for a DM reaction. Records only the emoji's shape (`str(emoji)`), never
    any inferred sentiment.
    """
    _require_aware("now", now)
    if payload.guild_id is None:
        return None
    return ReactionEvent(
        guild_id=payload.guild_id,
        member_id=payload.user_id,
        occurred_at=now,
        channel_id=payload.channel_id,
        emoji=str(payload.emoji),
    )


class VoiceStateSubject(Protocol):
    """One member's voice-channel presence at a single instant.

    See the module docstring's "Why `extract_voice_session_event` does not take
    raw `discord.VoiceState`" section for why this is a bespoke snapshot shape
    rather than `discord.VoiceState` itself.
    """

    @property
    def guild_id(self) -> int: ...
    @property
    def member_id(self) -> int: ...
    @property
    def channel_id(self) -> int: ...
    @property
    def occurred_at(self) -> datetime: ...


def extract_voice_session_event(
    before: VoiceStateSubject, after: VoiceStateSubject, now: datetime
) -> VoiceSessionEvent | None:
    """Deterministically extract a completed `VoiceSessionEvent` from a paired
    join-time (`before`) and leave-time (`after`) snapshot of the same member in
    the same guild channel. Records only join/leave timestamps and the derived
    duration — never audio, never a transcript.

    Returns `None` for internally inconsistent input (mismatched member/guild,
    a channel change instead of a single session, or an `after` reading that
    precedes `before`) rather than raising — this function trusts the caller's
    pairing is usually correct but must never fabricate a session out of
    contradictory snapshots.
    """
    _require_aware("now", now)
    _require_aware("before.occurred_at", before.occurred_at)
    _require_aware("after.occurred_at", after.occurred_at)
    if before.guild_id != after.guild_id or before.member_id != after.member_id:
        return None
    if before.channel_id != after.channel_id:
        return None
    if after.occurred_at < before.occurred_at:
        return None
    return VoiceSessionEvent(
        guild_id=before.guild_id,
        member_id=before.member_id,
        occurred_at=before.occurred_at,
        left_at=after.occurred_at,
        channel_id=before.channel_id,
    )


class AttendancePayloadSubject(Protocol):
    """Structural shape `extract_attendance_event` needs, combining the fields
    Discord's `on_scheduled_event_user_add`/`_remove` callbacks already carry
    (`discord.ScheduledEvent`'s guild/event IDs and the RSVPing user's ID) plus
    which action fired. `guild_id` is `None` only for an internally inconsistent
    caller-assembled payload — a real Scheduled Event always belongs to a guild.
    """

    @property
    def guild_id(self) -> int | None: ...
    @property
    def user_id(self) -> int: ...
    @property
    def scheduled_event_id(self) -> int: ...
    @property
    def action(self) -> AttendanceAction: ...


def extract_attendance_event(
    payload: AttendancePayloadSubject, now: datetime
) -> EventAttendanceEvent | None:
    """Deterministically extract an `EventAttendanceEvent` from one RSVP payload,
    or `None` if the payload carries no guild.
    """
    _require_aware("now", now)
    if payload.guild_id is None:
        return None
    return EventAttendanceEvent(
        guild_id=payload.guild_id,
        member_id=payload.user_id,
        occurred_at=now,
        scheduled_event_id=payload.scheduled_event_id,
        action=payload.action,
    )
