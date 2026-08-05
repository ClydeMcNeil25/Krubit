"""Discord presence extraction and rendering for live-stream signals."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit
from warnings import catch_warnings, simplefilter

import discord

from krubit.domain.live_signals import StreamingObservation, TwitchStream, normalize_twitch_channel

_DISCORD_CONTENT_CREATOR_LIMIT = 100
_DISCORD_EMBED_FIELD_VALUE_LIMIT = 1_024
_DISCORD_URL_LIMIT = 2_048
_LIVE_PURPLE = 0x8B5CF6
_LIVE_VIEW_TIMEOUT_SECONDS = 180.0
_FULLWIDTH_AT = "＠"


def extract_twitch_observation(
    member: discord.Member, *, observed_at: datetime
) -> StreamingObservation | None:
    """Extract only a validated Twitch streaming activity from a member presence."""
    if member.bot:
        return None
    for activity in member.activities:
        if activity.type is not discord.ActivityType.streaming:
            continue
        url = _activity_url(activity)
        if url is None:
            continue
        twitch_login = normalize_twitch_channel(url)
        if twitch_login is None:
            continue
        return StreamingObservation(
            guild_id=member.guild.id,
            member_id=member.id,
            twitch_login=twitch_login,
            twitch_url=url,
            activity_started_at=_activity_started_at(activity),
            observed_at=observed_at,
        )
    return None


def live_allowed_mentions() -> discord.AllowedMentions:
    """Allow only the intentional, fixed @everyone mention in live content."""
    return discord.AllowedMentions(
        everyone=True,
        users=False,
        roles=False,
        replied_user=False,
    )


def render_live_content(display_name: str) -> str:
    """Render the approved creature-language live announcement safely."""
    safe_name = _safe_text(display_name, limit=_DISCORD_CONTENT_CREATOR_LIMIT)
    return f"⟟⋏⎅⏃ ⎎⟒⏁⊑ @everyone {safe_name} ⌇⊑⏃ ⌰⟟⎐⏃!"


def render_live_embed(
    observation: StreamingObservation,
    stream: TwitchStream | None,
    *,
    activity_name: str | None = None,
) -> discord.Embed:
    """Render the approved full or honest reduced Twitch live-signal card."""
    if stream is None:
        creator = observation.twitch_login
        title = activity_name or "Unavailable"
        category = "Unavailable"
        description = "Twitch details are still being fetched."
        thumbnail_url = None
    else:
        creator = stream.user_name
        title = stream.title
        category = stream.game_name
        description = "Krubit detected a creator streaming"
        thumbnail_url = _thumbnail_url(stream.thumbnail_url)

    embed = discord.Embed(
        title="🔮 LIVE SIGNAL FOUND",
        url=observation.twitch_url,
        description=description,
        color=_LIVE_PURPLE,
    )
    embed.add_field(
        name="Creator",
        value=_safe_text(creator, limit=_DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=True,
    )
    embed.add_field(name="Platform", value="Twitch", inline=True)
    embed.add_field(
        name="Title",
        value=_safe_text(title, limit=_DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=False,
    )
    embed.add_field(
        name="Category",
        value=_safe_text(category, limit=_DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=True,
    )
    embed.add_field(name="Status", value="Streaming Now", inline=True)
    if thumbnail_url is not None:
        embed.set_thumbnail(url=thumbnail_url)
        embed.set_image(url=thumbnail_url)
    embed.set_footer(text="Automated creature signal • Twitch")
    return embed


def safe_display_text(value: str, *, limit: int) -> str:
    """Escape markdown/mentions and bound length; shared by every card renderer.

    Extracted so `krubit.discord.content_cards` renders platform-neutral cards with the
    same neutralization rules already approved for the Twitch live-signal card, instead
    of re-implementing them.
    """
    return _safe_text(value, limit=limit)


def https_preview_url(value: str) -> str | None:
    """Resolve a templated preview URL to a bounded, https-only canonical URL.

    Substitutes the conventional `{width}`/`{height}` template tokens with a fixed
    640x360 size and returns `None` for anything too long, unparsable, or not https —
    the caller renders a reduced card (no image) rather than trust unsafe media.
    Extracted so `krubit.discord.content_cards` reuses the same media-safety rule
    already approved for the Twitch live-signal card.
    """
    return _thumbnail_url(value)


def build_live_view(twitch_url: str) -> discord.ui.View:
    """Build a temporary link-only view for an already normalized Twitch channel."""
    twitch_login = normalize_twitch_channel(twitch_url)
    if twitch_login is None:
        raise ValueError("twitch_url must be a valid Twitch channel URL")
    view = discord.ui.View(timeout=_LIVE_VIEW_TIMEOUT_SECONDS)
    view.add_item(
        discord.ui.Button(
            label="Fetch the Stream",
            style=discord.ButtonStyle.link,
            url=f"https://www.twitch.tv/{twitch_login}",
        )
    )
    return view


def _activity_url(activity: object) -> str | None:
    value = getattr(activity, "url", None)
    return value if isinstance(value, str) else None


def _activity_started_at(activity: object) -> datetime | None:
    value = getattr(activity, "start", None)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None and value.utcoffset() is not None else None


def _safe_text(value: str, *, limit: int) -> str:
    with catch_warnings():
        simplefilter("ignore", DeprecationWarning)
        escaped = discord.utils.escape_markdown(value.replace("@", _FULLWIDTH_AT))
    return escaped.replace("\x00", "")[:limit] or "Unavailable"


def _thumbnail_url(value: str) -> str | None:
    canonical = value.replace("{width}", "640").replace("{height}", "360")
    if len(canonical) > _DISCORD_URL_LIMIT:
        return None
    try:
        parsed = urlsplit(canonical)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return canonical
