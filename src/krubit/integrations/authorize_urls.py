"""Builds the outbound OAuth authorization URL a member clicks to
authorize Krubit against their Instagram, Threads, or TikTok account.

Pure functions only -- no I/O, no framework dependency, matching this
package's established convention for connector modules. This is the one
genuinely new piece of code Phase 2's OAuth flow was missing: state
issuance (`SQLiteStore.issue_oauth_attempt`) and the receiving/token-
exchange side (`src/krubit/web/wiring.py`) already existed and are
untouched by this module.

Scopes are verified against Meta's own permissions reference
(`developers.facebook.com/docs/permissions`) and TikTok's OAuth docs, not
guessed -- see `docs/superpowers/plans/2026-08-10-creator-authorize-link.md`'s
Global Constraints for the source of each value. `user.info.profile` for
TikTok specifically matches the exact scope name
`src/krubit/web/wiring.py`'s own token-exchange handler already depends on
for username resolution -- reusing it here is correctness, not a new guess.

Facebook Page/Profile are deliberately unsupported here (both are already
a documented dead end even with a working link -- no Page-token exchange
implemented), and `Capability.LIVE` is unsupported for every platform
(neither Meta nor TikTok has a stable, well-documented scope for this
today) -- both raise `ValueError` rather than building a URL that can't
work.

Threads is not routed through Facebook's Login dialog (`www.facebook.com`):
despite living under the Meta umbrella, Threads has its own standalone
OAuth authorization host (`https://threads.net/oauth/authorize`), matching
`src/krubit/integrations/meta.py`'s own `_THREADS_TOKEN_EXCHANGE_URL`
(`https://graph.threads.net/oauth/access_token`) -- the Facebook Login
dialog does not grant `threads_basic`/`threads_content_publish` scopes or
mint a code redeemable at `graph.threads.net`. Instagram genuinely does use
the Facebook Login dialog, so only Threads gets this separate host; both
still authenticate with the same `meta_app_id`/`meta_app_secret` Meta
Developer app credentials (`src/krubit/web/wiring.py`'s token-exchange
route already assumes this for both platforms).
"""

from __future__ import annotations

from urllib.parse import urlencode

from krubit.domain.creator_signals import Capability, Platform

_META_GRAPH_API_VERSION = "v21.0"

# Threads' own standalone OAuth authorization host -- not Facebook's Login
# dialog. Matches `krubit.integrations.meta._THREADS_TOKEN_EXCHANGE_URL`
# (`https://graph.threads.net/oauth/access_token`), which this codebase
# already uses to redeem a Threads authorization code; a code minted by the
# Facebook Login dialog can't be redeemed there. See the module docstring.
_THREADS_AUTHORIZE_URL = "https://threads.net/oauth/authorize"

_INSTAGRAM_SCOPES: dict[Capability, str] = {
    Capability.ACCOUNT: "instagram_basic",
    Capability.SOCIAL: "instagram_basic,instagram_content_publish",
}
_THREADS_SCOPES: dict[Capability, str] = {
    Capability.ACCOUNT: "threads_basic",
    Capability.SOCIAL: "threads_basic,threads_content_publish",
}
_META_SCOPES_BY_PLATFORM: dict[Platform, dict[Capability, str]] = {
    Platform.INSTAGRAM: _INSTAGRAM_SCOPES,
    Platform.THREADS: _THREADS_SCOPES,
}

_TIKTOK_SCOPES: dict[Capability, str] = {
    Capability.ACCOUNT: "user.info.profile",
    Capability.SOCIAL: "user.info.profile,video.list",
}


def build_meta_authorize_url(
    *, app_id: str, redirect_uri: str, state: str, platform: Platform, capability: Capability
) -> str:
    scopes_by_capability = _META_SCOPES_BY_PLATFORM.get(platform)
    if scopes_by_capability is None:
        raise ValueError(f"{platform.value} is not supported for Meta OAuth authorization")
    scope = scopes_by_capability.get(capability)
    if scope is None:
        raise ValueError(f"{capability.value} capability is not supported for authorization yet")
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            "response_type": "code",
        }
    )
    if platform is Platform.THREADS:
        return f"{_THREADS_AUTHORIZE_URL}?{query}"
    return f"https://www.facebook.com/{_META_GRAPH_API_VERSION}/dialog/oauth?{query}"


def build_tiktok_authorize_url(
    *, client_key: str, redirect_uri: str, state: str, capability: Capability
) -> str:
    scope = _TIKTOK_SCOPES.get(capability)
    if scope is None:
        raise ValueError(f"{capability.value} capability is not supported for authorization yet")
    query = urlencode(
        {
            "client_key": client_key,
            "scope": scope,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://www.tiktok.com/v2/auth/authorize/?{query}"
