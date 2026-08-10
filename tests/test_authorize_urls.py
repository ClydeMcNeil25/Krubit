"""Unit tests for krubit.integrations.authorize_urls -- pure URL
construction, no network calls, no framework dependency."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from krubit.domain.creator_signals import Capability, Platform
from krubit.integrations.authorize_urls import (
    build_meta_authorize_url,
    build_tiktok_authorize_url,
)


def test_build_meta_authorize_url_for_instagram_account() -> None:
    url = build_meta_authorize_url(
        app_id="app123",
        redirect_uri="https://example.com/callbacks/meta/authorize",
        state="state-token",
        platform=Platform.INSTAGRAM,
        capability=Capability.ACCOUNT,
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.facebook.com"
    assert parsed.path == "/v21.0/dialog/oauth"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["app123"]
    assert query["redirect_uri"] == ["https://example.com/callbacks/meta/authorize"]
    assert query["state"] == ["state-token"]
    assert query["scope"] == ["instagram_basic"]
    assert query["response_type"] == ["code"]


def test_build_meta_authorize_url_for_instagram_social_includes_publish_scope() -> None:
    url = build_meta_authorize_url(
        app_id="app123",
        redirect_uri="https://example.com/callbacks/meta/authorize",
        state="state-token",
        platform=Platform.INSTAGRAM,
        capability=Capability.SOCIAL,
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["instagram_basic,instagram_content_publish"]


def test_build_meta_authorize_url_for_threads_account() -> None:
    url = build_meta_authorize_url(
        app_id="app123",
        redirect_uri="https://example.com/callbacks/meta/authorize",
        state="state-token",
        platform=Platform.THREADS,
        capability=Capability.ACCOUNT,
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "threads.net"
    assert parsed.path == "/oauth/authorize"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["app123"]
    assert query["redirect_uri"] == ["https://example.com/callbacks/meta/authorize"]
    assert query["state"] == ["state-token"]
    assert query["scope"] == ["threads_basic"]
    assert query["response_type"] == ["code"]


def test_build_meta_authorize_url_for_threads_social_includes_publish_scope() -> None:
    url = build_meta_authorize_url(
        app_id="app123",
        redirect_uri="https://example.com/callbacks/meta/authorize",
        state="state-token",
        platform=Platform.THREADS,
        capability=Capability.SOCIAL,
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["threads_basic,threads_content_publish"]


@pytest.mark.parametrize("platform", [Platform.FACEBOOK, Platform.FACEBOOK_PAGE, Platform.TIKTOK])
def test_build_meta_authorize_url_rejects_unsupported_platforms(platform: Platform) -> None:
    with pytest.raises(ValueError, match="not supported"):
        build_meta_authorize_url(
            app_id="app123",
            redirect_uri="https://example.com/callbacks/meta/authorize",
            state="state-token",
            platform=platform,
            capability=Capability.ACCOUNT,
        )


def test_build_meta_authorize_url_rejects_live_capability() -> None:
    with pytest.raises(ValueError, match="not supported"):
        build_meta_authorize_url(
            app_id="app123",
            redirect_uri="https://example.com/callbacks/meta/authorize",
            state="state-token",
            platform=Platform.INSTAGRAM,
            capability=Capability.LIVE,
        )


def test_build_tiktok_authorize_url_for_account() -> None:
    url = build_tiktok_authorize_url(
        client_key="key123",
        redirect_uri="https://example.com/callbacks/tiktok/authorize",
        state="state-token",
        capability=Capability.ACCOUNT,
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.tiktok.com"
    assert parsed.path == "/v2/auth/authorize/"
    query = parse_qs(parsed.query)
    assert query["client_key"] == ["key123"]
    assert query["scope"] == ["user.info.profile"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["https://example.com/callbacks/tiktok/authorize"]
    assert query["state"] == ["state-token"]


def test_build_tiktok_authorize_url_for_social_includes_video_list_scope() -> None:
    url = build_tiktok_authorize_url(
        client_key="key123",
        redirect_uri="https://example.com/callbacks/tiktok/authorize",
        state="state-token",
        capability=Capability.SOCIAL,
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["user.info.profile,video.list"]


def test_build_tiktok_authorize_url_rejects_live_capability() -> None:
    with pytest.raises(ValueError, match="not supported"):
        build_tiktok_authorize_url(
            client_key="key123",
            redirect_uri="https://example.com/callbacks/tiktok/authorize",
            state="state-token",
            capability=Capability.LIVE,
        )
