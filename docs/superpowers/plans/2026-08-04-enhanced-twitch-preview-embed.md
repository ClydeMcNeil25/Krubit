# Enhanced Twitch Preview Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Krubit's existing single Discord live-signal embed with a linked Twitch title and large 640×360 stream preview while retaining its compact thumbnail, statistics, controlled mention, button, and one-message delivery guarantees.

**Architecture:** Keep the change inside the pure Discord renderer in `krubit.discord.live_signals`; the runtime continues to send and edit the returned embed without presentation-specific branches. Reuse the existing canonical Twitch URL and `_thumbnail_url` validation boundary, so no storage, Twitch client, permissions, or lifecycle changes are required.

**Tech Stack:** Python 3.13, discord.py 2.7.1, pytest 9, Ruff, Pyright strict mode

## Global Constraints

- Preserve the exact creature-language `@everyone` content and controlled `AllowedMentions` behavior.
- Preserve Creator, Platform, Title, Category, Status, purple `0x8B5CF6` color, footer, and **Fetch the Stream** button.
- Use only the existing normalized Twitch channel URL and validated HTTPS 640×360 thumbnail URL.
- Keep one Discord message per Twitch stream; enrichment edits that message and never announces again.
- Show no image when Twitch metadata or its thumbnail is missing or unsafe.
- Do not add permissions, secrets, dependencies, tables, background jobs, native link unfurls, or inline video playback.

## File Structure

- Modify `src/krubit/discord/live_signals.py`: render the linked title and large preview using existing validated values.
- Modify `tests/test_live_signal_discord.py`: protect the linked-title, large-image, fallback, fields, and unsafe-thumbnail contracts.
- Verify `tests/test_live_signal_runtime.py`: retain the existing edit-in-place and deduplication behavior without runtime changes.

---

### Task 1: Render and Roll Out the Enhanced Single Embed

**Files:**
- Modify: `src/krubit/discord/live_signals.py:57-108`
- Test: `tests/test_live_signal_discord.py:73-117`
- Verify: `tests/test_live_signal_runtime.py`

**Interfaces:**
- Consumes: `render_live_embed(observation: StreamingObservation, stream: TwitchStream | None, *, activity_name: str | None = None) -> discord.Embed`
- Consumes: `_thumbnail_url(value: str) -> str | None`
- Produces: the same `discord.Embed` interface with `embed.url` set to `observation.twitch_url` and, for a valid enriched stream, both `embed.thumbnail.url` and `embed.image.url` set to the canonical 640×360 preview URL.

- [ ] **Step 1: Write failing renderer contract tests**

Extend `test_full_embed_uses_safe_purple_live_signal_card_and_canonical_thumbnail` with independently derived assertions:

```python
assert embed.url == "https://www.twitch.tv/krucialstudios"
assert embed.thumbnail.url == "https://cdn.twitch.tv/preview-640x360.jpg"
assert embed.image.url == "https://cdn.twitch.tv/preview-640x360.jpg"
```

Extend `test_reduced_embed_uses_discord_activity_name_and_no_invented_twitch_facts`:

```python
assert embed.url == "https://www.twitch.tv/krucialstudios"
assert embed.thumbnail.url is None
assert embed.image.url is None
```

Add a boundary test that keeps unsafe preview values out of both image surfaces while retaining the safe Twitch destination:

```python
def test_full_embed_rejects_unsafe_preview_without_losing_stream_link() -> None:
    stream = TwitchStream(
        stream_id="stream-1",
        user_login="krucialstudios",
        user_name="Krucial Studios",
        title="Live",
        game_name="Art",
        started_at=STARTED_AT,
        thumbnail_url="http://cdn.twitch.tv/preview-{width}x{height}.jpg",
    )

    embed = render_live_embed(observation(), stream)

    assert embed.url == "https://www.twitch.tv/krucialstudios"
    assert embed.thumbnail.url is None
    assert embed.image.url is None
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_live_signal_discord.py -q
```

Expected: FAIL because the enriched embed has no `url` and no large `image`; the existing fields, sanitization, thumbnail, and fallback assertions remain green.

- [ ] **Step 3: Implement the minimal renderer change**

Construct the embed with its existing title and the canonical observation URL:

```python
embed = discord.Embed(
    title="🔮 LIVE SIGNAL FOUND",
    url=observation.twitch_url,
    description=description,
    color=_LIVE_PURPLE,
)
```

Use the already validated preview URL for both supported image surfaces:

```python
if thumbnail_url is not None:
    embed.set_thumbnail(url=thumbnail_url)
    embed.set_image(url=thumbnail_url)
```

Do not change content rendering, field rendering, `_thumbnail_url`, the view/button, the runtime, storage, or Twitch lookup logic.

- [ ] **Step 4: Run focused renderer and runtime tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_live_signal_discord.py tests\test_live_signal_runtime.py -q
```

Expected: PASS, including linked title, large preview, unsafe/fallback behavior, edit-in-place recovery, nonce limit, role ownership, and delivery deduplication.

- [ ] **Step 5: Run the full repository verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pyright.exe --pythonpath .\.venv\Scripts\python.exe
git diff --check
```

Expected: all tests pass; Ruff reports `All checks passed!`; Pyright reports `0 errors, 0 warnings, 0 informations`; `git diff --check` exits successfully.

- [ ] **Step 6: Commit the implementation**

```powershell
git add -- src/krubit/discord/live_signals.py tests/test_live_signal_discord.py
git commit -m "feat: add large Twitch stream preview"
```

- [ ] **Step 7: Restart the reviewed feature build and verify the live edit path**

Stop only the verified `phase-2a-live-stream-signals` process tree, restart `scripts/invoke-krubit.ps1 run` from this worktree with `KRUBIT_LIVE_SIGNALS_ENABLED=true`, and confirm:

```text
Krubit reconnects without stderr output.
The existing active session retains one delivery row and the same announcement message ID.
The announcement is edited to show the linked title and large Twitch preview.
No second @everyone message is created.
The Streaming Now role remains owned by Krubit until the stream ends.
```

- [ ] **Step 8: Verify the end transition**

After the streamer stops, confirm the session becomes terminal, Krubit removes only its owned `Streaming Now` role, and the succeeded announcement delivery remains as the audit record. Do not remove pre-existing roles or delete the announcement.
