"""Unit tests for `krubit.discord.activity_events` extraction functions.

Pure-function tests: no Discord objects, no I/O, no storage — matching the
`test_message_signal_extraction.py`/`test_entry_sniff_extraction.py` convention
for `discord/*` extraction functions. Uses small local `Fake*`-shaped factories
rather than mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from krubit.discord.activity_events import (
    extract_attendance_event,
    extract_message_event,
    extract_reaction_event,
    extract_voice_session_event,
)
from krubit.domain.activity_ledger import AttendanceAction

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)


class FakeGuild:
    def __init__(self, guild_id: int = 111) -> None:
        self.id = guild_id


class FakeAuthor:
    def __init__(self, author_id: int = 222) -> None:
        self.id = author_id


class FakeChannel:
    def __init__(self, channel_id: int = 333) -> None:
        self.id = channel_id


class FakeThread:
    def __init__(self, thread_id: int = 777, parent_id: int = 333) -> None:
        self.id = thread_id
        self.parent_id = parent_id


class FakeMessage:
    def __init__(
        self,
        *,
        guild: FakeGuild | None,
        author: FakeAuthor,
        channel: FakeChannel | FakeThread,
        content: str = "hello there",
    ) -> None:
        self.guild = guild
        self.author = author
        self.channel = channel
        # Real `discord.Message` always carries `content`; kept here only so a
        # regression that starts reading it from the extraction function would
        # have something plausible to read (and get caught by the "carries no
        # content" test below), never because the extractor is meant to see it.
        self.content = content


def dm_message(*, content: str = "secret dm stuff") -> FakeMessage:
    return FakeMessage(guild=None, author=FakeAuthor(), channel=FakeChannel(), content=content)


def message(*, content: str = "hello there", thread: bool = False) -> FakeMessage:
    channel: FakeChannel | FakeThread = FakeThread() if thread else FakeChannel()
    return FakeMessage(guild=FakeGuild(), author=FakeAuthor(), channel=channel, content=content)


class FakeReactionPayload:
    def __init__(
        self,
        *,
        guild_id: int | None = 111,
        user_id: int = 222,
        channel_id: int = 333,
        emoji: object = "🎉",
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.emoji = emoji


class FakeVoiceState:
    def __init__(
        self,
        *,
        guild_id: int = 111,
        member_id: int = 222,
        channel_id: int = 444,
        occurred_at: datetime = NOW,
    ) -> None:
        self.guild_id = guild_id
        self.member_id = member_id
        self.channel_id = channel_id
        self.occurred_at = occurred_at


class FakeAttendancePayload:
    def __init__(
        self,
        *,
        guild_id: int | None = 111,
        user_id: int = 222,
        scheduled_event_id: int = 999,
        action: AttendanceAction = AttendanceAction.ADD,
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.scheduled_event_id = scheduled_event_id
        self.action = action


# -- extract_message_event -----------------------------------------------------


def test_extract_message_event_ignores_dms() -> None:
    assert extract_message_event(dm_message(), now=NOW) is None


def test_extract_message_event_carries_no_content() -> None:
    event = extract_message_event(message(content="secret stuff"), now=NOW)
    assert event is not None
    assert not hasattr(event, "content")


def test_extract_message_event_populates_guild_member_channel() -> None:
    event = extract_message_event(message(), now=NOW)
    assert event is not None
    assert event.guild_id == 111
    assert event.member_id == 222
    assert event.channel_id == 333
    assert event.thread_id is None
    assert event.occurred_at == NOW


def test_extract_message_event_in_a_thread_records_parent_channel_and_thread() -> None:
    event = extract_message_event(message(thread=True), now=NOW)
    assert event is not None
    assert event.channel_id == 333
    assert event.thread_id == 777


# -- extract_reaction_event -----------------------------------------------------


def test_extract_reaction_event_ignores_dms() -> None:
    assert extract_reaction_event(FakeReactionPayload(guild_id=None), now=NOW) is None


def test_extract_reaction_event_carries_no_message_content() -> None:
    event = extract_reaction_event(FakeReactionPayload(), now=NOW)
    assert event is not None
    assert not hasattr(event, "content")
    assert not hasattr(event, "message_content")


def test_extract_reaction_event_records_emoji_shape_only() -> None:
    event = extract_reaction_event(FakeReactionPayload(emoji="🔥"), now=NOW)
    assert event is not None
    assert event.emoji == "🔥"
    assert event.guild_id == 111
    assert event.member_id == 222
    assert event.channel_id == 333


# -- extract_voice_session_event -------------------------------------------------


def test_extract_voice_session_event_computes_join_leave_and_duration() -> None:
    before = FakeVoiceState(occurred_at=NOW)
    after = FakeVoiceState(occurred_at=LATER)
    event = extract_voice_session_event(before, after, now=LATER)
    assert event is not None
    assert event.occurred_at == NOW
    assert event.left_at == LATER
    assert event.channel_id == 444
    assert event.duration == (LATER - NOW)


def test_extract_voice_session_event_carries_no_audio_or_transcript() -> None:
    before = FakeVoiceState()
    after = FakeVoiceState(occurred_at=LATER)
    event = extract_voice_session_event(before, after, now=LATER)
    assert event is not None
    assert not hasattr(event, "audio")
    assert not hasattr(event, "transcript")


def test_extract_voice_session_event_returns_none_for_mismatched_member() -> None:
    before = FakeVoiceState(member_id=222, occurred_at=NOW)
    after = FakeVoiceState(member_id=333, occurred_at=LATER)
    assert extract_voice_session_event(before, after, now=LATER) is None


def test_extract_voice_session_event_returns_none_for_channel_change() -> None:
    before = FakeVoiceState(channel_id=444, occurred_at=NOW)
    after = FakeVoiceState(channel_id=555, occurred_at=LATER)
    assert extract_voice_session_event(before, after, now=LATER) is None


def test_extract_voice_session_event_returns_none_when_after_precedes_before() -> None:
    before = FakeVoiceState(occurred_at=LATER)
    after = FakeVoiceState(occurred_at=NOW)
    assert extract_voice_session_event(before, after, now=LATER) is None


# -- extract_attendance_event ----------------------------------------------------


def test_extract_attendance_event_ignores_payloads_without_a_guild() -> None:
    assert extract_attendance_event(FakeAttendancePayload(guild_id=None), now=NOW) is None


def test_extract_attendance_event_records_action_and_event_id() -> None:
    event = extract_attendance_event(
        FakeAttendancePayload(action=AttendanceAction.REMOVE, scheduled_event_id=888), now=NOW
    )
    assert event is not None
    assert event.guild_id == 111
    assert event.member_id == 222
    assert event.scheduled_event_id == 888
    assert event.action == AttendanceAction.REMOVE
    assert event.occurred_at == NOW
