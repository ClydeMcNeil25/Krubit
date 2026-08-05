"""Generalized Twitch/YouTube Discord presence detection.

Twitch behavior must remain byte-for-byte what `tests/test_live_signal_discord.py`
already locks in for `extract_twitch_observation`; these tests only cover the
generalized `extract_streaming_observation` wrapper and the new YouTube branch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import discord

from krubit.discord.live_runtime import (
    YouTubeStreamingPresence,
    extract_streaming_observation,
)
from krubit.domain.live_signals import StreamingObservation

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 8, 5, 11, 55, tzinfo=UTC)


def member_with_stream(
    url: str, *, bot: bool = False, started_at: datetime | None = None
) -> discord.Member:
    activity = SimpleNamespace(type=discord.ActivityType.streaming, url=url, start=started_at)
    return member_with_activities((activity,), bot=bot)


def member_with_game() -> discord.Member:
    activity = SimpleNamespace(type=discord.ActivityType.playing, url=None, start=None)
    return member_with_activities((activity,))


def member_with_activities(
    activities: tuple[SimpleNamespace, ...], *, bot: bool = False
) -> discord.Member:
    return cast(
        discord.Member,
        SimpleNamespace(id=222, bot=bot, guild=SimpleNamespace(id=111), activities=activities),
    )


def test_youtube_presence_accepts_canonical_watch_and_live_urls() -> None:
    assert extract_streaming_observation(member_with_stream("https://youtube.com/watch?v=abc"))
    assert extract_streaming_observation(member_with_stream("https://youtube.com/live/abc"))


def test_youtube_presence_returns_a_youtube_streaming_presence_with_the_video_id() -> None:
    member = member_with_stream(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", started_at=STARTED_AT
    )

    observation = extract_streaming_observation(member, NOW)

    assert observation == YouTubeStreamingPresence(
        guild_id=111,
        member_id=222,
        video_id="dQw4w9WgXcQ",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        activity_started_at=STARTED_AT,
        observed_at=NOW,
    )


def test_youtube_presence_rejects_non_watch_non_live_youtube_urls() -> None:
    assert (
        extract_streaming_observation(member_with_stream("https://youtube.com/@someone"), NOW)
        is None
    )
    assert (
        extract_streaming_observation(member_with_stream("https://youtube.com/watch"), NOW)
        is None
    )
    assert (
        extract_streaming_observation(member_with_stream("http://youtube.com/watch?v=abc"), NOW)
        is None
    )


def test_extract_streaming_observation_still_prefers_twitch_when_present() -> None:
    member = member_with_stream("https://www.twitch.tv/krucialstudios", started_at=STARTED_AT)

    observation = extract_streaming_observation(member, NOW)

    assert observation == StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=STARTED_AT,
        observed_at=NOW,
    )


def test_extract_streaming_observation_ignores_bots_and_non_streaming_activity() -> None:
    assert extract_streaming_observation(member_with_game(), NOW) is None
    assert (
        extract_streaming_observation(
            member_with_stream("https://youtube.com/watch?v=abc", bot=True), NOW
        )
        is None
    )


def test_extract_streaming_observation_defaults_observed_at_to_now() -> None:
    observation = extract_streaming_observation(
        member_with_stream("https://youtube.com/watch?v=abc")
    )

    assert isinstance(observation, YouTubeStreamingPresence)
    assert observation.observed_at.tzinfo is not None
