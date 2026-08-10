# `/fetch creator authorize` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/fetch creator authorize <url> <capability>` command that
builds a real, clickable OAuth authorization URL for Instagram/Threads/
TikTok and sends it to the requesting member — closing Phase 2's
documented "nothing generates the click-to-authorize link" gap.

**Architecture:** Two pure URL-building functions (no framework
dependency) plus one new service method on the existing
`ContentCommandService`, following the exact `creator_add` pattern for
authority/URL-recognition/card-rendering. The command reads Meta/TikTok
credentials at the Discord layer and passes them as plain arguments
(matching this codebase's established "settings read at the Discord
layer, never inside a framework-independent service" convention) —
`ContentCommandService` itself gains no dependency on `Settings`.

**Tech Stack:** Python 3.13, discord.py 2.7.1, pytest/pytest-asyncio.

## Global Constraints

- Supported platforms: `Platform.INSTAGRAM`, `Platform.THREADS` (via
  Meta), `Platform.TIKTOK`. Every other platform (including
  `Platform.FACEBOOK_PAGE`/`Platform.FACEBOOK`) is rejected with
  `CommandStatus.FAILED` before any URL is built or state issued.
- Supported capabilities: `Capability.ACCOUNT`, `Capability.SOCIAL` only.
  `Capability.LIVE` is rejected the same way, before any state issuance —
  checked early, so no wasted `oauth_attempts` row gets written for an
  unsupported capability.
- Exact scopes (verified against Meta's own permissions reference and
  TikTok's docs — not guessed):
  - Instagram: `account` → `instagram_basic`; `social` →
    `instagram_basic,instagram_content_publish`
  - Threads: `account` → `threads_basic`; `social` →
    `threads_basic,threads_content_publish`
  - TikTok: `account` → `user.info.profile` (matches the exact scope name
    this codebase's own `src/krubit/web/wiring.py:240` comment already
    depends on for username resolution); `social` →
    `user.info.profile,video.list`
- Meta authorize URL: `https://www.facebook.com/v21.0/dialog/oauth` with
  query params `client_id`, `redirect_uri`, `state`, `scope`,
  `response_type=code`.
- TikTok authorize URL: `https://www.tiktok.com/v2/auth/authorize/` with
  query params `client_key`, `scope`, `response_type=code`,
  `redirect_uri`, `state`.
- Redirect URIs must exactly match the existing receiving routes:
  `{meta_callback_base_url}/callbacks/meta/authorize` and
  `{tiktok_callback_base_url}/callbacks/tiktok/authorize` (per
  `src/krubit/web/wiring.py:272,373` — do not invent different paths).
- Authority: staff-or-self, reusing
  `krubit.services.creator_registry.require_creator_authority` exactly as
  `creator_add` already does (admin acts on anyone; Creator-role member
  acts only on themselves).
- The command must verify the account is already registered (via
  `/fetch creator add`) and that its stored `owner_member_id` matches the
  intended owner, before issuing anything — never trust the caller's
  `owner` argument alone without cross-checking the actual stored row.
- If the relevant platform's credentials are unset (`meta_app_id`/
  `tiktok_client_key` is `None` — today's live state), return
  `CommandStatus.FAILED` with a clear "not configured yet" message, never
  a broken or partially-built link.
- No change to `MetaOAuthStates`/`TikTokOAuthStates` (superseded,
  untouched) or to any receiving-side callback route.

---

### Task 1: `authorize_urls.py` — pure URL-building functions

**Files:**
- Create: `src/krubit/integrations/authorize_urls.py`
- Test: create `tests/test_authorize_urls.py`

**Interfaces:**
- Produces: `build_meta_authorize_url(*, app_id: str, redirect_uri: str,
  state: str, platform: Platform, capability: Capability) -> str` — raises
  `ValueError` for any platform other than `INSTAGRAM`/`THREADS`, or any
  capability other than `ACCOUNT`/`SOCIAL`.
- Produces: `build_tiktok_authorize_url(*, client_key: str, redirect_uri:
  str, state: str, capability: Capability) -> str` — raises `ValueError`
  for any capability other than `ACCOUNT`/`SOCIAL`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_authorize_urls.py`:

```python
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
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["threads_basic"]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_authorize_urls.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'krubit.integrations.authorize_urls'`

- [ ] **Step 3: Implement `authorize_urls.py`**

Create `src/krubit/integrations/authorize_urls.py`:

```python
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
"""

from __future__ import annotations

from urllib.parse import urlencode

from krubit.domain.creator_signals import Capability, Platform

_META_GRAPH_API_VERSION = "v21.0"

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_authorize_urls.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/integrations/authorize_urls.py tests/test_authorize_urls.py`
Expected: clean.

```bash
git add src/krubit/integrations/authorize_urls.py tests/test_authorize_urls.py
git commit -m "feat: add pure Meta/TikTok OAuth authorize-URL builders"
```

---

### Task 2: `ContentCommandService.creator_authorize` and the Discord command

**Files:**
- Modify: `src/krubit/discord/content_commands.py`
- Modify: `src/krubit/discord/bot.py`
- Test: `tests/test_content_commands.py`
- Test: `tests/test_cli.py` (or wherever `FetchCommands`'s structural
  child-count test lives — `/fetch creator` gains one child, not
  `FetchCommands` itself, so this is likely a no-op verification step
  rather than an edit; check first)

**Interfaces:**
- Consumes: `build_meta_authorize_url`/`build_tiktok_authorize_url` (Task
  1); `SQLiteStore.issue_oauth_attempt` (already exists,
  `src/krubit/storage/sqlite.py:2205`); `creator_account_id` (already
  exists, `src/krubit/domain/creator_signals.py:142`);
  `require_creator_authority` (already used by `creator_add`).
- Produces: `ContentCommandService.creator_authorize(self, *, actor:
  ActorContext, owner: ActorContext, url: str, capability: Capability,
  meta_app_id: str | None, meta_callback_base_url: str | None,
  tiktok_client_key: str | None, tiktok_callback_base_url: str | None) ->
  CommandResult`

- [ ] **Step 1: Write the failing service-layer tests**

Add to `tests/test_content_commands.py`, near the existing `creator_add`
tests (after line ~200, before the pause/resume section). First add the
new imports this needs at the top of the file:
```python
from krubit.integrations.catalog import recognize_account_url
```
(Check whether `Capability`/`creator_account_id` are already imported
from `krubit.domain.creator_signals` in this file's existing import block
at line 18 — if not, add them to that same import line rather than a new
one.)

```python
# -- authorize -----------------------------------------------------------------

INSTAGRAM_URL = "https://www.instagram.com/examplecreator"
TIKTOK_URL = "https://www.tiktok.com/@examplecreator"

_META_APP_ID = "test-meta-app-id"
_META_CALLBACK_BASE_URL = "https://example.com"
_TIKTOK_CLIENT_KEY = "test-tiktok-client-key"
_TIKTOK_CALLBACK_BASE_URL = "https://example.com"


async def _add_and_confirm(
    commands: ContentCommandService, *, actor: ActorContext, owner: ActorContext, url: str
) -> str:
    added = await commands.creator_add(actor=actor, owner=owner, url=url, confirm=True)
    account_id = added.detail["account_id"]
    assert isinstance(account_id, str)
    return account_id


@pytest.mark.asyncio
async def test_creator_authorize_denies_non_owner_non_admin(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL)

    result = await commands.creator_authorize(
        actor=other_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.DENIED


@pytest.mark.asyncio
async def test_creator_authorize_succeeds_for_owner_with_instagram_account(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL)

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "facebook.com" in result.card.description


@pytest.mark.asyncio
async def test_creator_authorize_succeeds_for_admin_on_behalf_of_another_member(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=TIKTOK_URL)

    result = await commands.creator_authorize(
        actor=admin_member(),
        owner=creator_member(),
        url=TIKTOK_URL,
        capability=Capability.SOCIAL,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.SUCCEEDED
    assert result.card is not None
    assert "tiktok.com" in result.card.description


@pytest.mark.asyncio
async def test_creator_authorize_fails_when_account_not_yet_added(
    commands: ContentCommandService,
) -> None:
    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_rejects_facebook_page_before_issuing_any_state(
    commands: ContentCommandService, store: SQLiteStore
) -> None:
    facebook_page_url = "https://www.facebook.com/pages/Example-Page/123456789"
    await _add_and_confirm(
        commands, actor=creator_member(), owner=creator_member(), url=facebook_page_url
    )

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=facebook_page_url,
        capability=Capability.ACCOUNT,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_rejects_live_capability_before_issuing_any_state(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL)

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.LIVE,
        meta_app_id=_META_APP_ID,
        meta_callback_base_url=_META_CALLBACK_BASE_URL,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED


@pytest.mark.asyncio
async def test_creator_authorize_fails_cleanly_when_meta_not_configured(
    commands: ContentCommandService,
) -> None:
    await _add_and_confirm(commands, actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL)

    result = await commands.creator_authorize(
        actor=creator_member(),
        owner=creator_member(),
        url=INSTAGRAM_URL,
        capability=Capability.ACCOUNT,
        meta_app_id=None,
        meta_callback_base_url=None,
        tiktok_client_key=_TIKTOK_CLIENT_KEY,
        tiktok_callback_base_url=_TIKTOK_CALLBACK_BASE_URL,
    )
    assert result.status is CommandStatus.FAILED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_content_commands.py -v -k authorize`
Expected: FAIL — `AttributeError: 'ContentCommandService' object has no
attribute 'creator_authorize'`

- [ ] **Step 3: Implement `ContentCommandService.creator_authorize`**

Add to `src/krubit/discord/content_commands.py`. First add the new
imports near the top (alongside the existing `from krubit.integrations.
catalog import CATALOG, recognize_account_url` line):

```python
from krubit.domain.creator_signals import Capability, creator_account_id
from krubit.integrations.authorize_urls import (
    build_meta_authorize_url,
    build_tiktok_authorize_url,
)
```

(Check the existing `from krubit.domain.creator_signals import (...)`
import block at the top of the file — `Capability` and
`creator_account_id` likely need to be added to that existing multi-line
import rather than a new import line, matching the file's existing
style.)

Add this module-level constant near the other fixed-window constants
(e.g. near `_NOTIFICATION_CHANNEL_NAME`):

```python
# How long an issued authorize link stays valid before it must be
# re-requested. No existing precedent constant for this in the codebase
# (this is the first caller of `issue_oauth_attempt`) -- 10 minutes is
# long enough for a member to click through, short enough that a leaked
# link (e.g. pasted into the wrong channel) has a small blast radius.
_OAUTH_ATTEMPT_TTL = timedelta(minutes=10)
```

Add the method to `ContentCommandService`, after `creator_add`:

```python
    async def creator_authorize(
        self,
        *,
        actor: ActorContext,
        owner: ActorContext,
        url: str,
        capability: Capability,
        meta_app_id: str | None,
        meta_callback_base_url: str | None,
        tiktok_client_key: str | None,
        tiktok_callback_base_url: str | None,
    ) -> CommandResult:
        if actor.guild_id != owner.guild_id:
            return CommandResult(CommandStatus.DENIED, detail={"reason": "cross_guild_owner"})
        try:
            _require_authority(
                actor_member_id=actor.member_id,
                owner_member_id=owner.member_id,
                actor_is_admin=actor.is_admin,
                actor_has_creator_role=actor.has_creator_role,
            )
        except CreatorAuthorityError as exc:
            return _denied(exc)
        if capability not in (Capability.ACCOUNT, Capability.SOCIAL):
            return CommandResult(
                CommandStatus.FAILED,
                detail={"reason": f"{capability.value} capability is not supported yet"},
            )
        try:
            recognized = recognize_account_url(url)
        except ValueError as exc:
            return CommandResult(CommandStatus.FAILED, detail={"reason": str(exc)})

        account_id = creator_account_id(recognized.platform, recognized.handle)
        account = await self._store.get_creator_account(owner.guild_id, account_id)
        if account is None or account.owner_member_id != owner.member_id:
            return CommandResult(
                CommandStatus.FAILED,
                detail={
                    "reason": "creator account not found -- add it first with /fetch creator add"
                },
            )

        now = self._now()
        if recognized.platform in (Platform.INSTAGRAM, Platform.THREADS):
            if meta_app_id is None or meta_callback_base_url is None:
                return CommandResult(
                    CommandStatus.FAILED,
                    detail={"reason": "Meta authorization is not configured yet"},
                )
            redirect_uri = f"{meta_callback_base_url}/callbacks/meta/authorize"
            state = await self._store.issue_oauth_attempt(
                guild_id=owner.guild_id,
                member_id=owner.member_id,
                account_id=account.account_id,
                platform=recognized.platform.value,
                capability=capability.value,
                redirect_uri=redirect_uri,
                now=now,
                ttl=_OAUTH_ATTEMPT_TTL,
            )
            authorize_url = build_meta_authorize_url(
                app_id=meta_app_id,
                redirect_uri=redirect_uri,
                state=state,
                platform=recognized.platform,
                capability=capability,
            )
        elif recognized.platform is Platform.TIKTOK:
            if tiktok_client_key is None or tiktok_callback_base_url is None:
                return CommandResult(
                    CommandStatus.FAILED,
                    detail={"reason": "TikTok authorization is not configured yet"},
                )
            redirect_uri = f"{tiktok_callback_base_url}/callbacks/tiktok/authorize"
            state = await self._store.issue_oauth_attempt(
                guild_id=owner.guild_id,
                member_id=owner.member_id,
                account_id=account.account_id,
                platform=recognized.platform.value,
                capability=capability.value,
                redirect_uri=redirect_uri,
                now=now,
                ttl=_OAUTH_ATTEMPT_TTL,
            )
            authorize_url = build_tiktok_authorize_url(
                client_key=tiktok_client_key,
                redirect_uri=redirect_uri,
                state=state,
                capability=capability,
            )
        else:
            return CommandResult(
                CommandStatus.FAILED,
                detail={
                    "reason": f"{recognized.platform.value} does not support OAuth authorization"
                },
            )

        card = Card(
            "fetched",
            "Fetched: Authorization Link",
            f"Click to authorize {capability.value} access for {account.handle}: "
            f"{authorize_url}\n\nThis link expires in "
            f"{int(_OAUTH_ATTEMPT_TTL.total_seconds() // 60)} minutes and can only be used once.",
        )
        return CommandResult(
            CommandStatus.SUCCEEDED,
            card=card,
            detail={"platform": recognized.platform.value, "capability": capability.value},
        )
```

Note: `Card`/`CardField` are already imported in this file (used by
`_confirmation` and `creator_add`) — no new import needed for those.
`Platform` is already imported too.

- [ ] **Step 4: Run the service-layer tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_content_commands.py -v -k authorize`
Expected: all PASS

- [ ] **Step 5: Wire the Discord command and Settings plumbing**

In `src/krubit/discord/bot.py`:

1. `FetchCommands.__init__` (around line 76-90): add four new optional
   keyword parameters, following the exact style of the existing
   `activity_ledger_inactivity_threshold_days: int | None = None`
   parameter immediately above where you insert:
   ```python
   meta_app_id: str | None = None,
   meta_callback_base_url: str | None = None,
   tiktok_client_key: str | None = None,
   tiktok_callback_base_url: str | None = None,
   ```
   Store them: `self._meta_app_id = meta_app_id`, etc. (four lines,
   alongside where `self._presence_intent = presence_intent` and similar
   are already stored).

2. In `KrubitBot.__init__`'s `FetchCommands(...)` construction (around
   line 1137-1164), add the four corresponding arguments, reading from
   `settings`:
   ```python
   meta_app_id=settings.meta_app_id,
   meta_callback_base_url=settings.meta_callback_base_url,
   tiktok_client_key=settings.tiktok_client_key,
   tiktok_callback_base_url=settings.tiktok_callback_base_url,
   ```

3. In `CreatorCommands` (`src/krubit/discord/content_commands.py`, class
   starting at line 888), add the new command after `add`:
   ```python
       @app_commands.command(
           name="authorize", description="Get an OAuth authorization link for a creator account"
       )
       @app_commands.guild_only()
       async def authorize(
           self,
           interaction: discord.Interaction,
           url: str,
           capability: Literal["account", "social"],
           member: discord.Member | None = None,
       ) -> None:
           actor = await _actor_context(self._service, interaction)
           if actor is None:
               return
           owner = (
               await _actor_context(self._service, interaction, member_id=member.id)
               if member is not None
               else actor
           )
           if owner is None:
               return
           await interaction.response.defer(ephemeral=True, thinking=True)
           result = await self._service.creator_authorize(
               actor=actor,
               owner=owner,
               url=url,
               capability=Capability(capability),
               meta_app_id=self._parent._meta_app_id,
               meta_callback_base_url=self._parent._meta_callback_base_url,
               tiktok_client_key=self._parent._tiktok_client_key,
               tiktok_callback_base_url=self._parent._tiktok_callback_base_url,
           )
           await self._present(interaction, result)
   ```
   `Literal` has no precedent elsewhere in this codebase's Discord command
   layer (confirmed by repo-wide search — no existing `Literal[...]`
   parameter or `app_commands.Choice`/`@app_commands.choices` usage
   anywhere in `src/krubit/discord/`), but it is standard, built-in
   discord.py 2.x behavior: a `Literal["account", "social"]`-typed
   parameter automatically renders as a two-option dropdown in Discord,
   no extra registration needed. Add `from typing import Literal` to this
   file's imports (check the existing `from typing import ...` line near
   the top and add to it if one exists, otherwise add a new import line).
   Add `Capability` to the existing `krubit.domain.creator_signals`
   import if not already added in Task 2 Step 3.

- [ ] **Step 6: Add the Discord-layer test**

No existing test in this codebase exercises `CreatorCommands`'s Discord
command wrappers directly (confirmed by repo-wide search — only
`ContentCommandService`'s service-layer methods are tested, e.g. every
existing `creator_add`/`creator_authorize` test in
`tests/test_content_commands.py` calls the service directly, never
`.add.callback(...)`/`.authorize.callback(...)`). This step adds the
first one, proving specifically what only the full command wrapper (not
the service method alone) can prove: that `self._parent._meta_app_id`
etc. are correctly read and threaded through.

`_actor_context` (`content_commands.py:829`, already read in full while
writing this plan) does three things a fake must satisfy: (1) rejects
unless `interaction.guild_id`/`interaction.guild` are set; (2) rejects
unless `interaction.user` passes `isinstance(user, discord.Member)` —
this means `interaction.user` must be recognized as a `discord.Member` by
the *`content_commands` module's own imported `discord` reference*, so
the test must `monkeypatch.setattr("krubit.discord.content_commands.
discord.Member", _FakeMember)`, exactly mirroring the established
precedent in `tests/test_phase_one_commands.py` (same trick, different
module's `discord` binding); (3) calls `resolve_creator_bootstrap(store,
guild, now=...)`, which reads `guild.roles`/`guild.text_channels` (both
must exist, empty lists are fine — no bootstrap match needed for this
test) and `store.get_creator_bootstrap(guild.id)` (a real call against
the real `store` fixture, returns `None` for a guild with nothing seeded,
which is fine); (4) calls `guild.get_member(subject_id)`.

Add to `tests/test_content_commands.py`, after the service-layer
authorize tests from Step 1:

```python
class _FakeParent:
    """A minimal stand-in for `FetchCommands` -- `CreatorCommands.
    authorize` only reads these four credential attributes off `_parent`,
    so a full `FetchCommands`/`KrubitBot` construction is unnecessary."""

    def __init__(self) -> None:
        self._meta_app_id: str | None = _META_APP_ID
        self._meta_callback_base_url: str | None = _META_CALLBACK_BASE_URL
        self._tiktok_client_key: str | None = _TIKTOK_CLIENT_KEY
        self._tiktok_callback_base_url: str | None = _TIKTOK_CALLBACK_BASE_URL


class _AuthorizeFakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False)
        self.roles: list[object] = []


class _AuthorizeFakeGuild:
    def __init__(self, members: dict[int, _AuthorizeFakeMember]) -> None:
        self.id = GUILD_ID
        self.roles: list[object] = []
        self.text_channels: list[object] = []
        self._members = members

    def get_member(self, member_id: int) -> _AuthorizeFakeMember | None:
        return self._members.get(member_id)


class _AuthorizeFakeResponse:
    def __init__(self) -> None:
        self.deferred: dict[str, bool] | None = None
        self.sent: dict[str, object] | None = None

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.sent = {"content": content, "ephemeral": ephemeral}


class _AuthorizeFakeFollowup:
    def __init__(self) -> None:
        self.sent: dict[str, object] | None = None

    async def send(self, *, embed: object, ephemeral: bool) -> None:
        self.sent = {"embed": embed, "ephemeral": ephemeral}


class _AuthorizeFakeInteraction:
    def __init__(self, guild: _AuthorizeFakeGuild, member: _AuthorizeFakeMember) -> None:
        self.guild_id = guild.id
        self.guild = guild
        self.user = member
        self.response = _AuthorizeFakeResponse()
        self.followup = _AuthorizeFakeFollowup()


@pytest.mark.asyncio
async def test_authorize_command_denies_non_owner_before_any_send(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from krubit.discord.content_commands import ContentCommandService, CreatorCommands

    monkeypatch.setattr(
        "krubit.discord.content_commands.discord.Member", _AuthorizeFakeMember
    )
    service = ContentCommandService(store, now=lambda: NOW)
    await service.creator_add(
        actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL, confirm=True
    )
    commands = CreatorCommands(cast(object, _FakeParent()), service)
    owner = _AuthorizeFakeMember(OWNER_ID)
    other = _AuthorizeFakeMember(OTHER_ID)
    guild = _AuthorizeFakeGuild({OWNER_ID: owner, OTHER_ID: other})
    interaction = _AuthorizeFakeInteraction(guild, other)

    await commands.authorize.callback(  # type: ignore[attr-defined]
        commands, cast(discord.Interaction, interaction), INSTAGRAM_URL, "account", None
    )

    assert interaction.response.deferred is None
    assert interaction.followup.sent is None


@pytest.mark.asyncio
async def test_authorize_command_sends_an_embed_for_the_owner(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from krubit.discord.content_commands import ContentCommandService, CreatorCommands

    monkeypatch.setattr(
        "krubit.discord.content_commands.discord.Member", _AuthorizeFakeMember
    )
    service = ContentCommandService(store, now=lambda: NOW)
    await service.creator_add(
        actor=creator_member(), owner=creator_member(), url=INSTAGRAM_URL, confirm=True
    )
    commands = CreatorCommands(cast(object, _FakeParent()), service)
    owner = _AuthorizeFakeMember(OWNER_ID)
    guild = _AuthorizeFakeGuild({OWNER_ID: owner})
    interaction = _AuthorizeFakeInteraction(guild, owner)

    await commands.authorize.callback(  # type: ignore[attr-defined]
        commands, cast(discord.Interaction, interaction), INSTAGRAM_URL, "account", None
    )

    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.followup.sent is not None
```

Note: `creator_member()`/`other_member()` (the plain `ActorContext`
builder functions already used by the service-layer tests) are a
different thing from `_AuthorizeFakeMember` (a fake `discord.Member`
double) — the Discord-layer test needs the latter, since it exercises the
real `_actor_context` resolution path, not a hand-built `ActorContext`.
`OWNER_ID`/`OTHER_ID`/`GUILD_ID` constants already exist at the top of
this test file (used throughout the existing `creator_add` tests) — reuse
them, don't redefine.

- [ ] **Step 7: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, no regressions (baseline before this task: 1141
passing).

- [ ] **Step 8: Lint and type-check**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/discord/content_commands.py src/krubit/discord/bot.py src/krubit/integrations/authorize_urls.py`
Expected: clean.

Run: `./.venv/Scripts/python.exe -m pyright src/krubit/discord/content_commands.py src/krubit/discord/bot.py src/krubit/integrations/authorize_urls.py`
Expected: no new error category versus this branch's baseline (confirm
via `git stash`/`git stash pop` diffing, per this project's established
verification convention for every prior task).

- [ ] **Step 9: Commit**

```bash
git add src/krubit/discord/content_commands.py src/krubit/discord/bot.py tests/test_content_commands.py
git commit -m "feat: add /fetch creator authorize command"
```

---

## Final Verification

- [ ] Run the full suite once more: `./.venv/Scripts/python.exe -m pytest -q` — must show `1141 + N passed` where `N` is the number of new tests, zero failures.
- [ ] No live Meta/TikTok verification is required for this plan to be
      considered complete — matching this project's established
      convention for every prior OAuth-adjacent piece of work (code-trace
      + test-suite verified, live canary deferred until the project owner
      has real credentials).
- [ ] Confirm `.env.example` already lists `META_KRUBIT_APP_ID`,
      `META_KRUBIT_CALLBACK_BASE_URL`, `TIKTOK_KRUBIT_CLIENT_KEY`,
      `TIKTOK_KRUBIT_CALLBACK_BASE_URL` (it does, per existing lines
      24-29) — no `.env.example` change needed, these were already
      scaffolded before this plan.
