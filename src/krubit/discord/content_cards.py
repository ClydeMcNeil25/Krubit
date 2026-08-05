"""Platform-neutral Discord card rendering for unified creator notifications.

`build_live_card` and `build_social_card` turn a `ContentGroup` — the bounded,
presentation-ready evidence for one delivery, gathered from a `ContentPlan` and its
correlation-group members by `ContentRuntime` — into a `RenderedCard` ready to send or
edit. Rendering never performs I/O and never resolves a mention itself: the caller
supplies the already-decided `MentionKind` (see `NotificationPolicy.evaluate_and_claim`)
and this module only turns that decision into literal mention text and an explicit
`discord.AllowedMentions`.

Every excerpt is escaped and bounded the same way the approved Twitch live-signal card
already is (`krubit.discord.live_signals.safe_display_text`), every preview URL is
resolved through the same https-only, template-bounded rule
(`krubit.discord.live_signals.https_preview_url`), and a card falls back to a reduced,
image-free rendering — never losing its title, creator, or platform buttons — whenever
that preview is unavailable or `ContentGroup.media_safe` is `False`.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

from krubit.discord.live_signals import https_preview_url, safe_display_text
from krubit.domain.creator_signals import ContentKind, Platform
from krubit.services.notification_policy import MentionKind

_DISCORD_FIELD_LIMIT = 1_024
_DISCORD_CONTENT_LIMIT = 2_000
_CARD_COLOR_LIVE = 0x8B5CF6
_CARD_COLOR_SOCIAL = 0x22C55E
_CARD_VIEW_TIMEOUT_SECONDS = 180.0
_FOOTER_TEXT = "Krubit creator signal"

_PLATFORM_DISPLAY_NAMES: dict[Platform, str] = {
    Platform.TWITCH: "Twitch",
    Platform.YOUTUBE: "YouTube",
    Platform.X: "X",
    Platform.INSTAGRAM: "Instagram",
    Platform.FACEBOOK: "Facebook",
    Platform.FACEBOOK_PAGE: "Facebook Page",
    Platform.THREADS: "Threads",
    Platform.BLUESKY: "Bluesky",
    Platform.TIKTOK: "TikTok",
    Platform.FANBASE: "Fanbase",
}

_CONTENT_KIND_LABELS: dict[ContentKind, str] = {
    ContentKind.LIVE: "Live",
    ContentKind.VIDEO: "Video",
    ContentKind.SHORT: "Short",
    ContentKind.POST: "Post",
    ContentKind.REEL: "Reel",
}


def platform_display_name(platform: Platform) -> str:
    """The approved human-readable label for one platform, used in cards and buttons."""
    return _PLATFORM_DISPLAY_NAMES.get(platform, platform.value.title())


@dataclass(frozen=True, slots=True)
class ContentGroupMember:
    """One platform's contribution to a (possibly cross-posted) content group.

    `preview_image_url` is optional and, when present, is only ever trusted after
    `https_preview_url` validates it — the ledger itself carries no thumbnail, so this
    is populated only when a connector or later enrichment step has supplied one.
    """

    platform: Platform
    canonical_url: str
    creator_display_name: str
    title: str | None = None
    preview_image_url: str | None = None

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise ValueError("platform must be a Platform")
        if not self.canonical_url.startswith("https://"):
            raise ValueError("canonical_url must use https")
        if not self.creator_display_name.strip():
            raise ValueError("creator_display_name must not be blank")


@dataclass(frozen=True, slots=True)
class ContentGroup:
    """The bounded, presentation-ready evidence for one card.

    `members` is ordered; `members[0]` is the originating platform for the delivery
    that triggered this card, and every later member is a correlated cross-post
    surfaced as an additional platform button. `mention_role_id` carries the role a
    `MentionKind.ROLE` decision should ping — resolved by the caller from the account's
    `CreatorRoute`, never guessed here. `media_safe=False` forces a reduced card even
    when a preview URL is otherwise well-formed (for example a moderation flag).
    """

    content_kind: ContentKind
    members: tuple[ContentGroupMember, ...]
    mention_role_id: int | None = None
    media_safe: bool = True

    def __post_init__(self) -> None:
        if type(self.content_kind) is not ContentKind:
            raise ValueError("content_kind must be a ContentKind")
        if type(self.members) is not tuple or not self.members:
            raise ValueError("members must be a non-empty tuple")
        if type(self.media_safe) is not bool:
            raise ValueError("media_safe must be a bool")

    @property
    def primary(self) -> ContentGroupMember:
        return self.members[0]


@dataclass(frozen=True, slots=True)
class RenderedCard:
    """A fully rendered, ready-to-send-or-edit Discord announcement."""

    content: str
    embed: discord.Embed
    buttons: tuple[discord.ui.Button[discord.ui.View], ...]
    allowed_mentions: discord.AllowedMentions

    def build_view(self, *, timeout: float | None = _CARD_VIEW_TIMEOUT_SECONDS) -> discord.ui.View:
        """Build a fresh, non-persistent view carrying this card's platform buttons."""
        view = discord.ui.View(timeout=timeout)
        for button in self.buttons:
            view.add_item(button)
        return view


def build_live_card(group: ContentGroup, *, mention: MentionKind) -> RenderedCard:
    """Render a platform-neutral live-content announcement card."""
    primary = group.primary
    creator = safe_display_text(primary.creator_display_name, limit=_DISCORD_FIELD_LIMIT)
    headline = safe_display_text(primary.title or "Live now", limit=_DISCORD_FIELD_LIMIT)
    embed = discord.Embed(
        title="🔴 Live Now",
        url=primary.canonical_url,
        description=headline,
        color=_CARD_COLOR_LIVE,
    )
    embed.add_field(name="Creator", value=creator, inline=True)
    embed.add_field(name="Platform", value=platform_display_name(primary.platform), inline=True)
    preview = _resolve_preview(group)
    if preview is not None:
        embed.set_thumbnail(url=preview)
        embed.set_image(url=preview)
    embed.set_footer(text=_FOOTER_TEXT)
    content = f"{_mention_prefix(mention, group.mention_role_id)}{creator} is live!"
    return RenderedCard(
        content=content[:_DISCORD_CONTENT_LIMIT],
        embed=embed,
        buttons=tuple(_platform_button(member, verb="Watch") for member in group.members),
        allowed_mentions=_allowed_mentions_for(mention),
    )


def build_social_card(group: ContentGroup, *, mention: MentionKind) -> RenderedCard:
    """Render a platform-neutral social/video-content announcement card."""
    primary = group.primary
    creator = safe_display_text(primary.creator_display_name, limit=_DISCORD_FIELD_LIMIT)
    excerpt = safe_display_text(primary.title or "New post", limit=_DISCORD_FIELD_LIMIT)
    kind_label = _CONTENT_KIND_LABELS[group.content_kind]
    embed = discord.Embed(
        title=f"📣 New {kind_label}",
        url=primary.canonical_url,
        description=excerpt,
        color=_CARD_COLOR_SOCIAL,
    )
    embed.add_field(name="Creator", value=creator, inline=True)
    embed.add_field(name="Platform", value=platform_display_name(primary.platform), inline=True)
    preview = _resolve_preview(group)
    if preview is not None:
        embed.set_image(url=preview)
    embed.set_footer(text=_FOOTER_TEXT)
    content = (
        f"{_mention_prefix(mention, group.mention_role_id)}"
        f"{creator} posted a new {kind_label.lower()}"
    )
    return RenderedCard(
        content=content[:_DISCORD_CONTENT_LIMIT],
        embed=embed,
        buttons=tuple(_platform_button(member, verb="View") for member in group.members),
        allowed_mentions=_allowed_mentions_for(mention),
    )


def _resolve_preview(group: ContentGroup) -> str | None:
    if not group.media_safe or group.primary.preview_image_url is None:
        return None
    return https_preview_url(group.primary.preview_image_url)


def _platform_button(
    member: ContentGroupMember, *, verb: str
) -> discord.ui.Button[discord.ui.View]:
    return discord.ui.Button(
        label=f"{verb} on {platform_display_name(member.platform)}",
        style=discord.ButtonStyle.link,
        url=member.canonical_url,
    )


def _mention_prefix(mention: MentionKind, role_id: int | None) -> str:
    if mention is MentionKind.EVERYONE:
        return "@everyone "
    if mention is MentionKind.ROLE:
        if role_id is None:
            raise ValueError("a role mention requires ContentGroup.mention_role_id")
        return f"<@&{role_id}> "
    return ""


def _allowed_mentions_for(mention: MentionKind) -> discord.AllowedMentions:
    if mention is MentionKind.EVERYONE:
        return discord.AllowedMentions(
            everyone=True, users=False, roles=False, replied_user=False
        )
    if mention is MentionKind.ROLE:
        return discord.AllowedMentions(
            everyone=False, users=False, roles=True, replied_user=False
        )
    return discord.AllowedMentions.none()
