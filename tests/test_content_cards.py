from __future__ import annotations

import discord
import pytest

from krubit.discord.content_cards import (
    ContentGroup,
    ContentGroupMember,
    build_live_card,
    build_social_card,
)
from krubit.domain.creator_signals import ContentKind, Platform
from krubit.services.notification_policy import MentionKind


def live_group(**overrides: object) -> ContentGroup:
    defaults: dict[str, object] = {
        "content_kind": ContentKind.LIVE,
        "members": (
            ContentGroupMember(
                platform=Platform.TWITCH,
                canonical_url="https://www.twitch.tv/krucialstudios",
                creator_display_name="Krucial Studios",
                title="Building Krucial Town",
                preview_image_url="https://cdn.twitch.tv/preview-{width}x{height}.jpg",
            ),
            ContentGroupMember(
                platform=Platform.YOUTUBE,
                canonical_url="https://www.youtube.com/watch?v=abc123",
                creator_display_name="Krucial Studios",
            ),
        ),
    }
    defaults.update(overrides)
    return ContentGroup(**defaults)  # type: ignore[arg-type]


def social_group(**overrides: object) -> ContentGroup:
    defaults: dict[str, object] = {
        "content_kind": ContentKind.POST,
        "members": (
            ContentGroupMember(
                platform=Platform.X,
                canonical_url="https://x.com/krucialstudios/status/1",
                creator_display_name="Krucial Studios",
                title="A brand new devlog is up!",
            ),
        ),
    }
    defaults.update(overrides)
    return ContentGroup(**defaults)  # type: ignore[arg-type]


def test_live_card_uses_approved_copy_preview_and_multiple_watch_buttons() -> None:
    rendered = build_live_card(live_group(), mention=MentionKind.EVERYONE)

    assert "@everyone" in rendered.content
    assert rendered.embed.image is not None and rendered.embed.image.url is not None
    assert rendered.embed.image.url.endswith("640x360.jpg")
    assert [button.label for button in rendered.buttons] == [
        "Watch on Twitch",
        "Watch on YouTube",
    ]
    assert rendered.allowed_mentions.everyone is True
    assert rendered.allowed_mentions.roles is False
    assert rendered.allowed_mentions.users is False


def test_live_card_role_mention_uses_group_role_id_and_only_allows_roles() -> None:
    rendered = build_live_card(
        live_group(mention_role_id=555), mention=MentionKind.ROLE
    )

    assert rendered.content.startswith("<@&555> ")
    assert rendered.allowed_mentions.roles is True
    assert rendered.allowed_mentions.everyone is False


def test_live_card_role_mention_without_role_id_raises() -> None:
    with pytest.raises(ValueError, match="mention_role_id"):
        build_live_card(live_group(), mention=MentionKind.ROLE)


def test_live_card_none_mention_has_no_mention_text_and_empty_allowed_mentions() -> None:
    rendered = build_live_card(live_group(), mention=MentionKind.NONE)

    assert "@everyone" not in rendered.content
    assert "<@&" not in rendered.content
    assert rendered.allowed_mentions.everyone is False
    assert rendered.allowed_mentions.roles is False


def test_live_card_reduced_when_media_unsafe_keeps_link_and_buttons() -> None:
    rendered = build_live_card(
        live_group(media_safe=False), mention=MentionKind.NONE
    )

    assert rendered.embed.image is not None and rendered.embed.image.url is None
    assert rendered.embed.thumbnail is not None and rendered.embed.thumbnail.url is None
    assert rendered.embed.url == "https://www.twitch.tv/krucialstudios"
    assert len(rendered.buttons) == 2


def test_live_card_reduced_when_preview_is_not_https() -> None:
    unsafe_member = ContentGroupMember(
        platform=Platform.TWITCH,
        canonical_url="https://www.twitch.tv/krucialstudios",
        creator_display_name="Krucial Studios",
        preview_image_url="http://cdn.twitch.tv/preview-{width}x{height}.jpg",
    )
    rendered = build_live_card(
        live_group(members=(unsafe_member,)), mention=MentionKind.NONE
    )

    assert rendered.embed.image is not None and rendered.embed.image.url is None


def test_live_card_neutralizes_mentions_and_markdown_in_creator_and_title() -> None:
    member = ContentGroupMember(
        platform=Platform.TWITCH,
        canonical_url="https://www.twitch.tv/krucialstudios",
        creator_display_name="@everyone **Krucial**",
        title="@here *going live*",
    )
    rendered = build_live_card(live_group(members=(member,)), mention=MentionKind.NONE)

    assert "@everyone" not in rendered.content
    assert "＠everyone" in rendered.content
    assert "\\*\\*Krucial\\*\\*" in rendered.content
    assert "＠here" in str(rendered.embed.description)
    assert "\\*going live\\*" in str(rendered.embed.description)


def test_social_card_uses_view_buttons_and_content_kind_label() -> None:
    rendered = build_social_card(social_group(), mention=MentionKind.NONE)

    assert [button.label for button in rendered.buttons] == ["View on X"]
    assert rendered.embed.title == "📣 New Post"
    assert rendered.embed.url == "https://x.com/krucialstudios/status/1"
    assert "post" in rendered.content


def test_social_card_bounds_excerpt_to_discord_field_limit() -> None:
    member = ContentGroupMember(
        platform=Platform.X,
        canonical_url="https://x.com/krucialstudios/status/1",
        creator_display_name="Krucial Studios",
        title="x" * 2_000,
    )
    rendered = build_social_card(social_group(members=(member,)), mention=MentionKind.NONE)

    assert len(str(rendered.embed.description)) == 1_024


def test_social_card_role_mention_pings_only_the_configured_role() -> None:
    rendered = build_social_card(
        social_group(mention_role_id=777), mention=MentionKind.ROLE
    )

    assert rendered.content.startswith("<@&777> ")
    assert rendered.allowed_mentions.roles is True
    assert isinstance(rendered.buttons[0], discord.ui.Button)


def test_content_group_rejects_empty_members() -> None:
    with pytest.raises(ValueError, match="members"):
        ContentGroup(content_kind=ContentKind.LIVE, members=())
