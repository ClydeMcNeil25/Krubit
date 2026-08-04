from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import discord

from krubit.discord.live_signals import (
    build_live_view,
    extract_twitch_observation,
    live_allowed_mentions,
    render_live_content,
    render_live_embed,
)
from krubit.domain.live_signals import StreamingObservation, TwitchStream

NOW = datetime(2026, 8, 4, 20, 14, tzinfo=UTC)
STARTED_AT = datetime(2026, 8, 4, 20, 12, tzinfo=UTC)


def test_live_content_uses_alien_language_and_only_everyone_is_allowed() -> None:
    content = render_live_content("Krucial Studios")
    allowed = live_allowed_mentions()

    assert content == "⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone Krucial Studios ⌇⊑⏃ ⌰⟟⎐⏃!"
    assert allowed.everyone is True
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def test_extract_observation_ignores_bots_non_streaming_and_youtube() -> None:
    assert extract_twitch_observation(member_with_game(), observed_at=NOW) is None
    assert (
        extract_twitch_observation(
            member_with_stream("https://youtube.com/live/example"), observed_at=NOW
        )
        is None
    )
    assert (
        extract_twitch_observation(
            member_with_stream("https://twitch.tv/krucial", bot=True), observed_at=NOW
        )
        is None
    )


def test_extract_observation_keeps_only_valid_twitch_streaming_activity() -> None:
    member = member_with_stream("https://www.twitch.tv/KrucialStudios/", started_at=STARTED_AT)

    observation = extract_twitch_observation(member, observed_at=NOW)

    assert observation == StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=STARTED_AT,
        observed_at=NOW,
    )


def test_live_content_neutralizes_external_mentions_and_markdown_within_limit() -> None:
    content = render_live_content("@here **signal** " + "\\" * 120)

    creator = content.removeprefix("⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone ").removesuffix(" ⌇⊑⏃ ⌰⟟⎐⏃!")
    assert "@here" not in content
    assert "＠here" in content
    assert "**signal**" not in content
    assert "\\*\\*signal\\*\\*" in content
    assert len(creator) <= 100


def test_full_embed_uses_safe_purple_live_signal_card_and_canonical_thumbnail() -> None:
    stream = TwitchStream(
        stream_id="stream-1",
        user_login="krucialstudios",
        user_name="Creator @here **bold**",
        title="@everyone *a very real stream*",
        game_name="Games @role",
        started_at=STARTED_AT,
        thumbnail_url="https://cdn.twitch.tv/preview-{width}x{height}.jpg",
    )

    embed = render_live_embed(observation(), stream)

    assert embed.title == "🔮 LIVE SIGNAL FOUND"
    assert embed.description == "Krubit detected a creator streaming"
    assert embed.color is not None and embed.color.value == 0x8B5CF6
    assert field_values(embed) == {
        "Creator": "Creator ＠here \\*\\*bold\\*\\*",
        "Platform": "Twitch",
        "Title": "＠everyone \\*a very real stream\\*",
        "Category": "Games ＠role",
        "Status": "Streaming Now",
    }
    assert embed.thumbnail.url == "https://cdn.twitch.tv/preview-640x360.jpg"
    assert embed.footer.text == "Automated creature signal • Twitch"


def test_reduced_embed_uses_discord_activity_name_and_no_invented_twitch_facts() -> None:
    embed = render_live_embed(observation(), None, activity_name="@here **Discord stream**")

    assert embed.description == "Twitch details are still being fetched."
    assert field_values(embed) == {
        "Creator": "krucialstudios",
        "Platform": "Twitch",
        "Title": "＠here \\*\\*Discord stream\\*\\*",
        "Category": "Unavailable",
        "Status": "Streaming Now",
    }
    assert embed.thumbnail.url is None


def test_reduced_embed_bounds_escaped_activity_text_to_discord_field_limit() -> None:
    embed = render_live_embed(observation(), None, activity_name="*" * 2_000)

    assert len(field_values(embed)["Title"]) == 1_024


def test_live_view_has_nonpersistent_fetch_stream_link_to_normalized_twitch_url() -> None:
    view = build_live_view("https://www.twitch.tv/KrucialStudios/")

    assert view.is_persistent() is False
    assert len(view.children) == 1
    button = cast(discord.ui.Button[discord.ui.View], view.children[0])
    assert button.style is discord.ButtonStyle.link
    assert button.label == "Fetch the Stream"
    assert button.url == "https://www.twitch.tv/krucialstudios"


def field_values(embed: discord.Embed) -> dict[str, str]:
    return {str(field.name): str(field.value) for field in embed.fields}


def observation() -> StreamingObservation:
    return StreamingObservation(
        guild_id=111,
        member_id=222,
        twitch_login="krucialstudios",
        twitch_url="https://www.twitch.tv/krucialstudios",
        activity_started_at=STARTED_AT,
        observed_at=NOW,
    )


def member_with_game() -> discord.Member:
    activity = SimpleNamespace(type=discord.ActivityType.playing, url=None, start=None)
    return member_with_activities((activity,))


def member_with_stream(
    url: str, *, bot: bool = False, started_at: datetime | None = None
) -> discord.Member:
    activity = SimpleNamespace(type=discord.ActivityType.streaming, url=url, start=started_at)
    return member_with_activities((activity,), bot=bot)


def member_with_activities(
    activities: tuple[SimpleNamespace, ...], *, bot: bool = False
) -> discord.Member:
    return cast(
        discord.Member,
        SimpleNamespace(id=222, bot=bot, guild=SimpleNamespace(id=111), activities=activities),
    )
