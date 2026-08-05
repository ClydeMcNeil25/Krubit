"""Behavioral tests for `ContentCorrelator.correlate`.

Correlation must stay conservative: exact identity or an exact canonical-URL match
merges unconditionally, a strong probabilistic match (same creator, bounded time window,
plus a matching outbound link or media fingerprint) merges, and everything else —
including same-creator crossposts that merely share a similar title — stays separate.
Krubit must never silently drop a legitimately distinct post.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.creator_signals import Platform
from krubit.services.content_signals import ContentCorrelator, CorrelationCandidate

GUILD_ID = 111
OWNER_MEMBER_ID = 222
NOW = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)

correlator = ContentCorrelator()


def candidate(
    *,
    platform: Platform,
    external_id: str,
    canonical_url: str,
    owner_member_id: int = OWNER_MEMBER_ID,
    title: str | None = "Big Announcement",
    published_at: datetime | None = NOW,
    outbound_url: str | None = None,
    media_fingerprint: str | None = None,
) -> CorrelationCandidate:
    return CorrelationCandidate(
        guild_id=GUILD_ID,
        owner_member_id=owner_member_id,
        platform=platform,
        external_id=external_id,
        canonical_url=canonical_url,
        title=title,
        published_at=published_at,
        outbound_url=outbound_url,
        media_fingerprint=media_fingerprint,
    )


def youtube_video(**overrides: object) -> CorrelationCandidate:
    defaults: dict[str, object] = {
        "platform": Platform.YOUTUBE,
        "external_id": "yt-1",
        "canonical_url": "https://www.youtube.com/watch?v=yt-1",
    }
    defaults.update(overrides)
    return candidate(**defaults)  # type: ignore[arg-type]


def x_post_without_shared_link(**overrides: object) -> CorrelationCandidate:
    defaults: dict[str, object] = {
        "platform": Platform.X,
        "external_id": "x-1",
        "canonical_url": "https://x.com/creator/status/x-1",
        "published_at": NOW + timedelta(minutes=2),
    }
    defaults.update(overrides)
    return candidate(**defaults)  # type: ignore[arg-type]


def test_identical_content_identity_merges() -> None:
    first = candidate(
        platform=Platform.YOUTUBE, external_id="v1", canonical_url="https://youtube.com/v1"
    )
    second = candidate(
        platform=Platform.YOUTUBE, external_id="v1", canonical_url="https://youtube.com/v1"
    )
    decision = correlator.correlate(first, second)
    assert decision.merge is True
    assert decision.correlation_group is not None


def test_identical_canonical_url_merges_across_platforms() -> None:
    first = candidate(
        platform=Platform.YOUTUBE,
        external_id="v1",
        canonical_url="https://shared.example.com/post/1",
    )
    second = candidate(
        platform=Platform.X,
        external_id="x1",
        canonical_url="https://shared.example.com/post/1/",  # trailing slash normalizes equal
    )
    decision = correlator.correlate(first, second)
    assert decision.merge is True


def test_ambiguous_crosspost_is_not_merged() -> None:
    assert correlator.correlate(youtube_video(), x_post_without_shared_link()).merge is False


def test_strong_simulcast_matches_on_shared_outbound_link() -> None:
    first = youtube_video(outbound_url="https://store.example.com/preorder")
    second = x_post_without_shared_link(outbound_url="https://store.example.com/preorder?ref=x")
    decision = correlator.correlate(first, second)
    assert decision.merge is True
    assert "outbound" in decision.reason or "fingerprint" in decision.reason


def test_strong_simulcast_matches_on_media_fingerprint() -> None:
    first = youtube_video(media_fingerprint="sha256:abc123")
    second = x_post_without_shared_link(media_fingerprint="sha256:abc123")
    decision = correlator.correlate(first, second)
    assert decision.merge is True


def test_title_similarity_alone_never_merges() -> None:
    first = youtube_video(title="Launch Day!")
    second = x_post_without_shared_link(title="Launch Day!")
    decision = correlator.correlate(first, second)
    assert decision.merge is False


def test_different_creators_never_merge_even_with_matching_fingerprint() -> None:
    first = youtube_video(media_fingerprint="sha256:abc123")
    second = x_post_without_shared_link(owner_member_id=333, media_fingerprint="sha256:abc123")
    decision = correlator.correlate(first, second)
    assert decision.merge is False
    assert "creator" in decision.reason


def test_outside_correlation_window_never_merges() -> None:
    first = youtube_video(media_fingerprint="sha256:abc123", published_at=NOW)
    second = x_post_without_shared_link(
        media_fingerprint="sha256:abc123", published_at=NOW + timedelta(hours=6)
    )
    decision = correlator.correlate(first, second)
    assert decision.merge is False


def test_missing_published_at_on_either_side_never_merges_on_probabilistic_grounds() -> None:
    first = youtube_video(media_fingerprint="sha256:abc123", published_at=None)
    second = x_post_without_shared_link(media_fingerprint="sha256:abc123")
    decision = correlator.correlate(first, second)
    assert decision.merge is False


def test_correlate_is_symmetric() -> None:
    first = youtube_video(outbound_url="https://store.example.com/preorder")
    second = x_post_without_shared_link(outbound_url="https://store.example.com/preorder")
    forward = correlator.correlate(first, second)
    backward = correlator.correlate(second, first)
    assert forward.merge == backward.merge is True
    assert forward.correlation_group == backward.correlation_group


def test_correlate_rejects_candidates_from_different_guilds() -> None:
    first = youtube_video()
    second = x_post_without_shared_link()
    second_other_guild = CorrelationCandidate(
        guild_id=999,
        owner_member_id=second.owner_member_id,
        platform=second.platform,
        external_id=second.external_id,
        canonical_url=second.canonical_url,
        title=second.title,
        published_at=second.published_at,
        outbound_url=second.outbound_url,
        media_fingerprint=second.media_fingerprint,
    )
    with pytest.raises(ValueError, match="same guild"):
        correlator.correlate(first, second_other_guild)
