# Membership Join/Leave Announcements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Krubit automatically posts a factual join announcement to a
`#welcome` channel and a `#staff-notes` channel, and a factual leave
announcement to `#staff-notes`, whenever both channels exist by that exact
name — no configuration, no command, no toggle.

**Architecture:** A new, small, dependency-free runtime module
(`src/krubit/discord/membership_announcements.py`), matching the existing
`live_runtime.py` pattern of a small runtime class with `on_member_join`/
`on_member_remove` methods, wired into `KrubitBot`'s existing handlers of
the same names. No domain type, no storage, no settings — this is the
smallest runtime module in the codebase, since decision #1 in the design
spec removed all configuration surface.

**Tech Stack:** Python 3.13, discord.py 2.7.1, pytest/pytest-asyncio.

## Global Constraints

- No configuration of any kind — a channel's presence/absence by exact
  name is the only control. No new `Settings` field, no new database
  table, no new domain type, no new `/fetch` command.
- Channel names: exactly `"welcome"` and `"staff-notes"`, looked up fresh
  (not cached) on every join/leave event.
- Channel-type check matches `live_runtime.py`'s existing `_is_text_channel`
  duck-typing (`channel is not None and callable(channel.send) and
  callable(channel.permissions_for)`), not `isinstance(discord.TextChannel)`
  — reuse the exact same check, don't reinvent it.
- Messages are plain factual text, exactly:
  - `#welcome` on join: `<@{member.id}> has joined the server.`
  - `#staff-notes` on join: `<@{member.id}> joined. Account created: {member.created_at.isoformat()}.`
  - `#staff-notes` on leave: `<@{member.id}> left. Joined: {joined_at}. Left: {left_at}.`
    where `joined_at` is `member.joined_at.isoformat()` if not `None` else
    the literal string `"unknown"`, and `left_at` is `datetime.now(UTC).isoformat()`.
- Each of the two channels/three messages is independent — one missing
  channel must never block the other channel's message.
- A caught send failure (`discord.HTTPException`, `discord.Forbidden`,
  `discord.NotFound`, `ValueError` — the exact set `live_runtime.py`
  already catches around its own `channel.send` calls) must never
  propagate out of `on_member_join`/`on_member_remove`, and must never
  prevent the Watchdog/Activity Ledger calls already in those handlers
  from running.

---

### Task 1: `MembershipAnnouncementRuntime` and wiring into `on_member_join`/`on_member_remove`

**Files:**
- Create: `src/krubit/discord/membership_announcements.py`
- Modify: `src/krubit/discord/bot.py` (wire into `KrubitBot.__init__` and
  the two handlers, currently at lines 1469-1474 and 1480-1485)
- Test: create `tests/test_membership_announcements.py`

**Interfaces:**
- Produces: `WELCOME_CHANNEL_NAME = "welcome"`, `STAFF_NOTES_CHANNEL_NAME
  = "staff-notes"` (module-level constants, public — matching
  `live_runtime.py`'s `LIVE_CHANNEL_NAME` being importable by its test
  file)
- Produces: `class MembershipAnnouncementRuntime` with `async def
  on_member_join(self, member: discord.Member) -> None` and `async def
  on_member_remove(self, member: discord.Member) -> None`. No
  constructor arguments — this class holds no state and needs no
  dependencies (no store, no settings), since decision #1 removed all
  configuration.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_membership_announcements.py`. This mirrors
`tests/test_live_signal_runtime.py`'s fake-object pattern (read that file
first if you want the full precedent) but is much simpler — no roles, no
sessions, just a member joining/leaving a guild with zero, one, or two
named channels present.

```python
"""Unit tests for `krubit.discord.membership_announcements`.

Uses lightweight fake Discord objects (no real discord.py network calls),
matching `tests/test_live_signal_runtime.py`'s established convention for
this codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord
import pytest

from krubit.discord.membership_announcements import (
    STAFF_NOTES_CHANNEL_NAME,
    WELCOME_CHANNEL_NAME,
    MembershipAnnouncementRuntime,
)

CREATED_AT = datetime(2020, 1, 1, tzinfo=UTC)
JOINED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, *, fail: bool = False) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[dict[str, object]] = []
        self._fail = fail

    async def send(self, **kwargs: object) -> None:
        if self._fail:
            raise discord.Forbidden(response=object(), message="no permission")  # type: ignore[arg-type]
        self.sent.append(kwargs)

    def permissions_for(self, member: object) -> object:
        return object()


class FakeVoiceChannel:
    """A same-named non-text channel -- must never be treated as a target."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class FakeGuild:
    def __init__(self, channels: list[object]) -> None:
        self.id = 111
        self.channels = channels


class FakeMember:
    def __init__(
        self, member_id: int, guild: FakeGuild, *, joined_at: datetime | None = JOINED_AT
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.created_at = CREATED_AT
        self.joined_at = joined_at


@pytest.mark.asyncio
async def test_on_member_join_posts_to_both_channels_when_present() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([welcome, staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert welcome.sent == [{"content": "<@42> has joined the server."}]
    assert staff_notes.sent == [
        {"content": "<@42> joined. Account created: 2020-01-01T00:00:00+00:00."}
    ]


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_welcome_channel_but_still_posts_staff_notes() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1


@pytest.mark.asyncio
async def test_on_member_join_skips_missing_staff_notes_channel_but_still_posts_welcome() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type]

    assert len(welcome.sent) == 1


@pytest.mark.asyncio
async def test_on_member_join_ignores_a_same_named_non_text_channel() -> None:
    fake_voice = FakeVoiceChannel(1, WELCOME_CHANNEL_NAME)
    guild = FakeGuild([fake_voice])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    # Must not raise (e.g. AttributeError from calling .send on the voice
    # channel double, which has none) and must not be treated as a target.
    await runtime.on_member_join(member)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_on_member_join_absorbs_a_send_failure_without_raising() -> None:
    welcome = FakeTextChannel(1, WELCOME_CHANNEL_NAME, fail=True)
    guild = FakeGuild([welcome])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_join(member)  # type: ignore[arg-type] -- must not raise


@pytest.mark.asyncio
async def test_on_member_remove_posts_join_and_leave_timestamps() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=JOINED_AT)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    assert len(staff_notes.sent) == 1
    content = str(staff_notes.sent[0]["content"])
    assert content.startswith("<@42> left. Joined: 2026-08-10T12:00:00+00:00. Left: ")


@pytest.mark.asyncio
async def test_on_member_remove_handles_missing_joined_at() -> None:
    staff_notes = FakeTextChannel(2, STAFF_NOTES_CHANNEL_NAME)
    guild = FakeGuild([staff_notes])
    member = FakeMember(42, guild, joined_at=None)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type]

    content = str(staff_notes.sent[0]["content"])
    assert "Joined: unknown." in content


@pytest.mark.asyncio
async def test_on_member_remove_skips_silently_when_staff_notes_channel_absent() -> None:
    guild = FakeGuild([])
    member = FakeMember(42, guild)
    runtime = MembershipAnnouncementRuntime()

    await runtime.on_member_remove(member)  # type: ignore[arg-type] -- must not raise
```

Note on the `discord.Forbidden` construction in the failure test: check
what shape discord.py 2.7.1 actually requires for constructing this
exception in a test context (some versions need a real-ish response
object) — if the literal `discord.Forbidden(response=object(),
message=...)` call doesn't work cleanly, use whatever construction
pattern `tests/test_live_signal_runtime.py` or another existing test file
in this repo already uses to fabricate a `discord.Forbidden` for a similar
"send fails" test case, and copy that pattern instead of inventing a new
one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_membership_announcements.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named
'krubit.discord.membership_announcements'`

- [ ] **Step 3: Implement `membership_announcements.py`**

Create `src/krubit/discord/membership_announcements.py`:

```python
"""Automatic, convention-based join/leave announcements.

No configuration of any kind -- a channel's mere presence or absence by
exact name (`WELCOME_CHANNEL_NAME`/`STAFF_NOTES_CHANNEL_NAME`) is the only
control, matching `live_runtime.py`'s existing `LIVE_CHANNEL_NAME`
convention exactly. Enable/disable control for this and every other
Krubit feature is explicitly deferred to a future web dashboard (see the
design doc) -- this module intentionally carries zero settings/storage
dependency until that exists.

Every message is plain, factual text, matching this codebase's
established tone: Krubit's own README describes it as a "non-conversational"
bot, so even this public-facing welcome message stays factual rather than
personality-laden.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord

WELCOME_CHANNEL_NAME = "welcome"
STAFF_NOTES_CHANNEL_NAME = "staff-notes"

_logger = logging.getLogger(__name__)


def _is_text_channel(channel: object | None) -> bool:
    """Duck-typed text-channel check, matching `live_runtime.py`'s
    identical helper -- a channel needs a callable `.send` and
    `.permissions_for`, never `isinstance(discord.TextChannel)`."""
    return (
        channel is not None
        and callable(getattr(channel, "send", None))
        and callable(getattr(channel, "permissions_for", None))
    )


def _find_channel_by_name(guild: discord.Guild, name: str) -> discord.abc.Messageable | None:
    return next(
        (
            candidate
            for candidate in guild.channels
            if getattr(candidate, "name", None) == name and _is_text_channel(candidate)
        ),
        None,
    )


async def _send(channel: discord.abc.Messageable | None, content: str) -> None:
    if channel is None:
        return
    try:
        await channel.send(content=content)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
        _logger.debug("membership announcement send failed", exc_info=True)


class MembershipAnnouncementRuntime:
    """Posts join/leave announcements to fixed-name channels, if present."""

    async def on_member_join(self, member: discord.Member) -> None:
        welcome_channel = _find_channel_by_name(member.guild, WELCOME_CHANNEL_NAME)
        await _send(welcome_channel, f"<@{member.id}> has joined the server.")

        staff_notes_channel = _find_channel_by_name(member.guild, STAFF_NOTES_CHANNEL_NAME)
        created_at = member.created_at.isoformat()
        await _send(
            staff_notes_channel, f"<@{member.id}> joined. Account created: {created_at}."
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        staff_notes_channel = _find_channel_by_name(member.guild, STAFF_NOTES_CHANNEL_NAME)
        joined_at = member.joined_at.isoformat() if member.joined_at is not None else "unknown"
        left_at = datetime.now(UTC).isoformat()
        await _send(
            staff_notes_channel,
            f"<@{member.id}> left. Joined: {joined_at}. Left: {left_at}.",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_membership_announcements.py -v`
Expected: all PASS

- [ ] **Step 5: Wire into `KrubitBot`**

In `src/krubit/discord/bot.py`:

1. Add the import near the other runtime imports at the top of the file
   (find where `LiveSignalRuntime`/`WatchdogRuntime`/`ActivityRuntime` are
   imported and add `MembershipAnnouncementRuntime` alongside them):
   ```python
   from krubit.discord.membership_announcements import MembershipAnnouncementRuntime
   ```
2. In `KrubitBot.__init__`, after the existing `self._activity_runtime =
   ActivityRuntime(...)` block (around line 1134), add:
   ```python
   self._membership_announcements = MembershipAnnouncementRuntime()
   ```
3. In `on_member_join` (currently lines 1469-1474), add the new call
   after the existing three:
   ```python
   async def on_member_join(self, member: discord.Member) -> None:
       await self._ingest_change(
           "member_joined", member.guild.id, member.id, None, {"bot": member.bot}
       )
       await self._watchdog_runtime.on_member_join(member, datetime.now(UTC))
       await self._activity_runtime.on_member_join(member, datetime.now(UTC))
       await self._membership_announcements.on_member_join(member)
   ```
4. In `on_member_remove` (currently lines 1480-1485), add the new call
   after the existing three:
   ```python
   async def on_member_remove(self, member: discord.Member) -> None:
       await self._ingest_change(
           "member_left", member.guild.id, member.id, {"bot": member.bot}, None
       )
       await self._live_runtime.handle_member_leave(member)
       await self._activity_runtime.on_member_remove(member, datetime.now(UTC))
       await self._membership_announcements.on_member_remove(member)
   ```

Do not reorder the existing calls — the new call is strictly additive,
appended after the existing ones, so Watchdog/Activity Ledger/live-signal
processing order is unchanged.

- [ ] **Step 6: Add a wiring-level test proving a send failure doesn't
      block the other handlers**

This is the one behavior Step 1's unit tests can't prove on their own
(they test `MembershipAnnouncementRuntime` in isolation, not its position
inside `on_member_join`/`on_member_remove`). No existing test file
currently calls `KrubitBot.on_member_join`/`on_member_remove` directly
(confirmed by repo-wide search) — `tests/test_cli.py` is the closest
precedent, constructing `KrubitBot(settings, FoundationService(store))`
and calling other handler methods (e.g. `test_bot_records_guild_installed
_while_runtime_is_connected` calls `bot.on_guild_join`). Add a new test to
`tests/test_cli.py` following that exact construction pattern:

```python
@pytest.mark.asyncio
async def test_on_member_join_survives_an_announcement_send_failure(
    tmp_path: Path,
) -> None:
    """A failed announcement send must never block guild-event ingestion --
    Watchdog and Activity Ledger are both disabled by default (Settings()'s
    defaults), so this only needs to prove `_ingest_change` still runs."""
    store = await SQLiteStore.open(tmp_path / "krubit.db")
    await store.initialize()
    await store.set_guild_enabled(111, True)
    settings = Settings(application_id=123, database_path=tmp_path / "krubit.db")
    bot = KrubitBot(settings, FoundationService(store))

    class _FailingChannel:
        name = WELCOME_CHANNEL_NAME

        async def send(self, **kwargs: object) -> None:
            raise discord.Forbidden(response=object(), message="no permission")  # type: ignore[arg-type]

        def permissions_for(self, member: object) -> object:
            return object()

    guild = SimpleNamespace(id=111, name="Krucial Town", channels=[_FailingChannel()])
    member = SimpleNamespace(
        id=42,
        guild=guild,
        bot=False,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    try:
        await bot.on_member_join(cast(discord.Member, member))  # must not raise

        event_count, _ = await store.counts(111)
        assert event_count == 1  # _ingest_change still recorded "member_joined"
    finally:
        await bot.close()
        await store.close()
```

Add `from krubit.discord.membership_announcements import WELCOME_CHANNEL_NAME`
to this test file's imports. Check the top of `test_cli.py` for its
existing `datetime`/`UTC`/`discord`/`SimpleNamespace`/`cast` imports before
adding duplicates — most are very likely already imported given the file's
existing test bodies use them elsewhere.

- [ ] **Step 7: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, no regressions (baseline before this task: 1127
passing).

- [ ] **Step 8: Lint and type-check**

Run: `./.venv/Scripts/python.exe -m ruff check src/krubit/discord/membership_announcements.py tests/test_membership_announcements.py src/krubit/discord/bot.py`
Expected: clean.

Run: `./.venv/Scripts/python.exe -m pyright src/krubit/discord/membership_announcements.py src/krubit/discord/bot.py`
Expected: no new error *category* versus this file's existing baseline
(this codebase's pyright baseline is non-zero, entirely pre-existing
`discord.py` stub noise — confirm via `git stash`/`git stash pop` diffing
if the count changes, per this project's established verification
convention for every prior task).

- [ ] **Step 9: Commit**

```bash
git add src/krubit/discord/membership_announcements.py src/krubit/discord/bot.py tests/test_membership_announcements.py
git commit -m "feat: add automatic join/leave announcements to welcome/staff-notes channels"
```

---

## Final Verification

- [ ] Run the full suite once more: `./.venv/Scripts/python.exe -m pytest -q` — must show `1127 + N passed` where `N` is the number of new tests added, zero failures.
- [ ] No live Discord verification is required for this plan to be
      considered complete (matching this project's established convention
      for Discord-layer changes verified by direct code trace plus the
      automated test suite) — but if you want to see it live, create
      channels literally named `welcome` and `staff-notes` in your test
      guild and have a member join/leave.
