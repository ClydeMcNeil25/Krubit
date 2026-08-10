"""Unit tests for `krubit.discord.membership_announcements`.

Uses lightweight fake Discord objects (no real discord.py network calls),
matching `tests/test_live_signal_runtime.py`'s established convention for
this codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from krubit.discord.membership_announcements import (
    STAFF_NOTES_CHANNEL_NAME,
    WELCOME_CHANNEL_NAME,
    MembershipAnnouncementRuntime,
)

CREATED_AT = datetime(2020, 1, 1, tzinfo=UTC)
JOINED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, *, fail: bool = False) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[dict[str, object]] = []
        self._fail = fail

    async def send(self, **kwargs: object) -> None:
        if self._fail:
            raise discord.Forbidden(
                cast(Any, SimpleNamespace(status=403, reason="forbidden", headers={})),
                "no permission",
            )
        self.sent.append(kwargs)

    def permissions_for(self, member: object) -> object:
        return object()


class FakeVoiceChannel:
    """A same-named non-text channel -- must never be treated as a target."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class FakeGuild:
    def __init__(self, channels: list[object]) -> None:
        self.id = 111
        self.channels = channels


class FakeMember:
    def __init__(
        self, member_id: int, guild: FakeGuild, *, joined_at: datetime | None = JOINED_AT
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.created_at = CREATED_AT
        self.joined_at = joined_at


@pytest.mark.asyncio
async def test_on_member_join_posts_to_both_channels_when_present() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([welcome, staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert welcome.sent == [{"content": "<@42> has joined the server."}]
    assert staff_notes.sent == [
        {"content": "<@42> joined. Account created: 2020-01-01T00:00:00+00:00."}
    ]


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_welcome_channel_but_still_posts_staff_notes() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_staff_notes_channel_but_still_posts_welcome() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(welcome.sent) == 1


@pytest.mark.asyncio
async def test_on_member_join_ignores_a_same_named_non_text_channel() -> None:
    fake_voice = FakeVoiceChannel(1, WELCOME_CHANNEL_NAME)
    guild = FakeGuild([fake_voice])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    # Must not raise (e.g. AttributeError from calling .send on the voice
    # channel double, which has none) and must not be treated as a target.
    await runtime.on_member_join(member)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_on_member_join_absorbs_a_send_failure_without_raising() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME, fail=True)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type] -- must not raise


@pytest.mark.asyncio
async def test_on_member_remove_posts_join_and_leave_timestamps() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=JOINED_AT)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1
    content = str(staff_notes.sent[0]["content"])
    assert content.startswith("<@42> left. Joined: 2026-08-10T12:00:00+00:00. Left: ")


@pytest.mark.asyncio
async def test_on_member_remove_handles_missing_joined_at() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=None)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    content = str(staff_notes.sent[0]["content"])
    assert "Joined: unknown." in content


@pytest.mark.asyncio
async def test_on_member_remove_skips_silently_when_staff_notes_channel_absent() -> None:
    guild = FakeGuild([])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type] -- must not raise
