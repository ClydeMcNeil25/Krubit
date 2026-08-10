"""Unit tests for `krubit.discord.membership_announcements`.

Uses lightweight fake Discord objects (no real discord.py network calls)
plus a real `SQLiteStore`, matching `tests/test_live_signal_runtime.py`'s
established convention for this codebase.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import create_autospec

import discord
import pytest

from krubit.discord.membership_announcements import (
    _MEMBERSHIP_ALLOWED_MENTIONS,
    STAFF_NOTES_CHANNEL_NAME,
    WELCOME_CHANNEL_NAME,
    MembershipAnnouncementRuntime,
)
from krubit.storage.sqlite import SQLiteStore

CREATED_AT = datetime(2020, 1, 1, tzinfo=UTC)
JOINED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
GUILD_ID = 111


class FakePermissions:
    def __init__(self, *, view_channel: bool = True, send_messages: bool = True) -> None:
        self.view_channel = view_channel
        self.send_messages = send_messages


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int,
        name: str,
        *,
        fail: bool = False,
        permissions: FakePermissions | None = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[dict[str, object]] = []
        self._fail = fail
        self._permissions = permissions if permissions is not None else FakePermissions()

    async def send(self, **kwargs: object) -> None:
        if self._fail:
            raise discord.Forbidden(
                cast(Any, SimpleNamespace(status=403, reason="forbidden", headers={})),
                "no permission",
            )
        self.sent.append(kwargs)

    def permissions_for(self, member: object) -> object:
        return self._permissions


class NeverSendChannel:
    """A channel whose `.send` must never be invoked (permission pre-check)."""

    def __init__(self, channel_id: int, name: str, *, permissions: FakePermissions) -> None:
        self.id = channel_id
        self.name = name
        self._permissions = permissions

    async def send(self, **kwargs: object) -> None:
        raise AssertionError("should not be called")

    def permissions_for(self, member: object) -> object:
        return self._permissions


def make_autospec_voice_channel(channel_id: int, name: str) -> discord.VoiceChannel:
    """A real `isinstance`-recognizable `discord.VoiceChannel` double that
    also duck-types as valid (callable `.send`/`.permissions_for`) -- proves
    the `isinstance` exclusion in `_is_text_channel` does the actual work,
    not just the absence of a `.send` attribute."""
    channel = create_autospec(discord.VoiceChannel, instance=True)
    channel.id = channel_id
    channel.name = name
    channel.permissions_for.return_value = FakePermissions()
    return cast(discord.VoiceChannel, channel)


class FakeNoSendChannel:
    """A same-named object with no `.send` at all."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class FakeGuild:
    def __init__(self, channels: list[object], *, guild_id: int = GUILD_ID) -> None:
        self.id = guild_id
        self.channels = channels
        self.me = object()


class FakeMember:
    def __init__(
        self,
        member_id: int,
        guild: FakeGuild,
        *,
        joined_at: datetime | None = JOINED_AT,
        bot: bool = False,
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.created_at = CREATED_AT
        self.joined_at = joined_at
        self.bot = bot


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    opened = await SQLiteStore.open(tmp_path / "krubit.db")
    await opened.initialize()
    await opened.set_guild_enabled(GUILD_ID, True)
    yield opened


@pytest.mark.asyncio
async def test_on_member_join_posts_to_both_channels_when_present(store: SQLiteStore) -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([welcome, staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert welcome.sent == [
        {
            "content": "<@42> has joined the server.",
            "allowed_mentions": _MEMBERSHIP_ALLOWED_MENTIONS,
        }
    ]
    assert staff_notes.sent == [
        {
            "content": "<@42> joined. Account created: 2020-01-01T00:00:00+00:00.",
            "allowed_mentions": _MEMBERSHIP_ALLOWED_MENTIONS,
        }
    ]


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_welcome_channel_but_still_posts_staff_notes(
    store: SQLiteStore,
) -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_staff_notes_channel_but_still_posts_welcome(
    store: SQLiteStore,
) -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(welcome.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_factory",
    [
        lambda: FakeNoSendChannel(1, WELCOME_CHANNEL_NAME),
        lambda: make_autospec_voice_channel(1, WELCOME_CHANNEL_NAME),
    ],
    ids=["no_send_attribute", "real_shaped_voice_channel"],
)
async def test_on_member_join_ignores_a_same_named_voice_channel(
    store: SQLiteStore, channel_factory: Any
) -> None:
    fake_channel = channel_factory()
    guild = FakeGuild([fake_channel])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    # Must not raise and must not be treated as a target -- neither an
    # object missing `.send` entirely, nor a duck-typed-valid but
    # `isinstance`-recognized voice channel, may receive a message.
    await runtime.on_member_join(member)  # type: ignore[arg-type]

    send = getattr(fake_channel, "send", None)
    if send is not None and hasattr(send, "assert_not_called"):
        send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_join_absorbs_a_send_failure_without_raising(store: SQLiteStore) -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME, fail=True)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(member)  # type: ignore[arg-type] -- must not raise


@pytest.mark.asyncio
async def test_on_member_join_skips_send_when_missing_send_messages_permission(
    store: SQLiteStore,
) -> None:
    denied = FakePermissions(view_channel=True, send_messages=False)
    welcome = NeverSendChannel(1, WELCOME_CHANNEL_NAME, permissions=denied)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    # NeverSendChannel.send raises AssertionError if called at all -- the
    # permission pre-check must prevent the send attempt entirely.
    await runtime.on_member_join(member)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_on_member_join_skips_bot_members(store: SQLiteStore) -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([welcome, staff_notes])
    member = FakeMember(42, guild, bot=True)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert welcome.sent == []
    assert staff_notes.sent == []


@pytest.mark.asyncio
async def test_on_member_remove_posts_join_and_leave_timestamps(store: SQLiteStore) -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=JOINED_AT)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1
    content = str(staff_notes.sent[0]["content"])
    assert content.startswith("<@42> left. Joined: 2026-08-10T12:00:00+00:00. Left: ")


@pytest.mark.asyncio
async def test_on_member_remove_handles_missing_joined_at(store: SQLiteStore) -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=None)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    content = str(staff_notes.sent[0]["content"])
    assert "Joined: unknown." in content


@pytest.mark.asyncio
async def test_on_member_remove_skips_silently_when_staff_notes_channel_absent(
    store: SQLiteStore,
) -> None:
    guild = FakeGuild([])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_remove(member)  # type: ignore[arg-type] -- must not raise


@pytest.mark.asyncio
async def test_on_member_remove_skips_bot_members(store: SQLiteStore) -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, bot=True)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    assert staff_notes.sent == []


@pytest.mark.asyncio
async def test_disabled_guild_produces_no_announcements_at_all(store: SQLiteStore) -> None:
    await store.set_guild_enabled(GUILD_ID, False)
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([welcome, staff_notes])
    joining_member = FakeMember(42, guild)
    leaving_member = FakeMember(43, guild)
    runtime = MembershipAnnouncementRuntime(store)

    await runtime.on_member_join(joining_member)  # type: ignore[arg-type]
    await runtime.on_member_remove(leaving_member)  # type: ignore[arg-type]

    assert welcome.sent == []
    assert staff_notes.sent == []
