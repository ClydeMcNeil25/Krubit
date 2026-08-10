# Membership Join/Leave Announcements Design

**Date:** 2026-08-10
**Status:** Approved for implementation planning
**Scope:** Automatic, convention-based announcements when a member joins or
leaves the guild — no configuration, no toggle command, no new storage.
Confirmed interactively with the project owner: enable/disable control for
this and every other feature will eventually live in a future web
dashboard (a separate, much larger project); this feature intentionally
carries zero config surface of its own until that exists.

## Context

Krubit already records every join/leave (Phase 1's `guild_events` table,
plus a `JoinEvent` in the Phase 4 activity ledger for joins) but never
announces them anywhere. Separately, Krubit's existing live-stream
announcement feature (`live_runtime.py`) already establishes the pattern
this feature reuses: find a channel by a fixed literal name, post if it
exists, silently do nothing if it doesn't — no command, no per-guild
config row, no toggle.

## Confirmed decisions (from conversation)

1. **No configuration of any kind.** No enable/disable command, no
   per-guild config table, no domain value object. The channels' mere
   presence or absence *is* the control — remove/rename a channel to turn
   its announcements off. Matches `live_runtime.py`'s existing
   `LIVE_CHANNEL_NAME` convention exactly.
2. **Two fixed channel names:** `welcome` and `staff-notes`, each looked
   up fresh (not cached) on every join/leave event via exact name match
   against `guild.channels`, restricted to text channels.
3. **Three independent messages:**
   - `#welcome` on join: `<@member> has joined the server.`
   - `#staff-notes` on join: `<@member> joined. Account created:
     <account creation date>.`
   - `#staff-notes` on leave: `<@member> left. Joined: <join date>. Left:
     <leave date>.` — uses Discord's own cached `member.joined_at` from
     the `discord.Member` object already passed into `on_member_remove`,
     no extra storage query.
4. **Every message is plain, factual text — no personality.** Consistent
   with Krubit's established tone everywhere else, including this being a
   public-facing (not staff-only) message: Krubit's own README describes
   it as "Zariya's non-conversational Discord pet," so even a welcome
   message stays factual rather than warm/personality-laden.
5. **Each channel/message is independent.** If `welcome` doesn't exist
   but `staff-notes` does, the staff-notes messages still post (and vice
   versa) — a missing channel silently skips only its own message, never
   the others.
6. **Send failures never break the join/leave handler's other work.**
   `on_member_join`/`on_member_remove` already drive Watchdog and Activity
   Ledger processing; a failed announcement send (missing permissions,
   channel deleted mid-flight, a transient Discord API error) must be
   caught and absorbed, matching `live_runtime.py`'s existing
   `except (discord.HTTPException, discord.Forbidden, discord.NotFound,
   ValueError)` precedent, never propagate and abort the rest of the
   handler.

## Implementation approach

- Add two small helper functions (or a tiny new module if `bot.py` is
  already large enough that this warrants separating out — check the
  current line count and existing module-splitting convention before
  deciding) that each: find a text channel by exact name, and if found,
  attempt to send a message, catching send failures per decision 6 above.
- Call these from the existing `on_member_join`/`on_member_remove`
  handlers in `bot.py` (currently around lines 1469-1486), alongside the
  existing `_watchdog_runtime`/`_activity_runtime` calls already there —
  additive only, no change to what those existing calls do.
- No new `Settings`/env var, no new database table, no new domain type,
  no new `/fetch` command.

## Explicit Exclusions

- No enable/disable mechanism of any kind (explicitly deferred to the
  future web dashboard).
- No configurable channel (fixed name only, matching the live-signal
  precedent).
- No Watchdog risk-band or any other enrichment in the staff-notes join
  message — plain join fact only, per the project owner's earlier
  confirmed choice.
- No change to `guild_events`/activity-ledger recording — this feature
  only adds announcements on top of already-recorded events, never
  changes what gets recorded.

## Testing

- Unit tests for the channel-lookup helper: finds an exact-name text
  channel, ignores a same-named non-text channel (e.g. a voice channel
  literally named `welcome`), returns `None` when absent.
- Tests for each of the three message-sending paths: correct text sent
  when the channel exists, nothing sent (no exception) when it doesn't.
- A test proving one channel's absence doesn't block the other channel's
  message (e.g. `staff-notes` missing, `welcome` still receives its
  message on join).
- A test proving a caught send exception (mock `channel.send` to raise
  `discord.Forbidden`) does not propagate out of `on_member_join`/
  `on_member_remove`, and that Watchdog/Activity Ledger processing in the
  same handler still completes normally afterward.

## Completion Gate

Complete when: a member joining a guild with both channels present
produces both the welcome and staff-notes join messages with the exact
specified text; a member leaving produces the staff-notes leave message;
removing/renaming either channel silently disables only that channel's
message; a send failure never breaks the rest of the join/leave handler;
the full test suite passes with no regressions.
