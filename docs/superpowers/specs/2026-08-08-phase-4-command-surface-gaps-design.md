# Phase 4 Command Surface Gaps Design

**Date:** 2026-08-08
**Status:** Approved for implementation planning
**Scope:** Expose Phase 4's `returning_member_view`, `recognition_candidates`,
member deletion, member export, and channel-exclusion configuration through
`/fetch` commands — closing the "fully built, zero command surface" gap
documented in the Phase 4 devlog and README.

## Context

Phase 4 (Activity Ledger) shipped five capabilities with no way for staff to
actually reach them: two read-only views (`returning_member_view`,
`recognition_candidates`) and three privacy-control operations (member
deletion, member export, channel exclusion) whose service/storage layer is
fully implemented and tested, but which no `/fetch` command calls. This spec
closes that gap using the exact patterns `ActivityCommandService`/`bot.py`
already established for `newcomers`/`inactive`/`activity`/`milestones`.

## Confirmed against current code

- `returning_member_view(store, guild_id, inactivity_threshold, now)` and
  `recognition_candidates(guild_id, events, window, now)` both exist, are
  tested, and return well-formed tuples (`services/activity_views.py:307`,
  `services/milestones.py:280`) — `recognition_candidates` is pure and needs
  its `events` fetched by the caller first via
  `store.list_ledger_events_for_guild`.
- `activity_privacy.delete_member` and `activity_privacy.export_member_data`
  (`services/activity_privacy.py:152,198`) both exist and are tested at the
  service layer, including a structural proof that deletion clears every
  member-scoped table and export never leaks another member's data.
- `SQLiteStore.save_exclusion_entry`/`list_exclusion_entries`
  (`storage/sqlite.py:3994,4034`) exist, but **the one existing caller
  (`ActivityRuntime`'s default-exclusion seeding) always writes the bot's own
  application ID as `excluded_by`, never a real staff member** — there is no
  precedent today for a command passing the actual invoking staff member's ID
  into this field. This spec's exclusion command is the first real caller.
- The Phase 4 design spec's Privacy Controls section specifies: **deletion is
  staff-triggered only**; **export is self-triggered or staff-triggered on a
  member's behalf, and the staff-on-behalf-of path must be audited.**
  `export_member_data` currently has no audit call at all — this spec adds
  one, but only for the staff-on-behalf-of path (a self-export needs no
  audit trail of a member accessing their own data).
- No existing `/fetch` command paginates a list. `newcomers`/`inactive`
  join every entry into one embed description string with no cap. This spec
  keeps that convention (introducing pagination is out of scope for closing
  a backlog gap) but adds a defensive truncation — Discord embed
  descriptions cap at 4096 characters, and neither existing command guards
  against that limit today. A guild large enough to hit it would currently
  get a raw Discord API error; this spec's new commands cap at a fixed entry
  count and append `"...and N more"` rather than risk that failure mode,
  since these are genuinely new code paths, not an existing convention being
  preserved.

## Commands

All are `@app_commands.default_permissions(manage_guild=True)`,
`@app_commands.guild_only()`, under the existing `FetchCommands` group in
`bot.py` — matching every other staff-only `/fetch` command. No new command
namespace.

1. **`/fetch returning`** — staff-only. Calls `returning_member_view` with the
   guild's configured `activity_ledger_inactivity_threshold_days` setting (the
   existing `Settings` field, already parsed, currently unused by any command
   surfacing "returning" specifically — `/fetch inactive` uses the same
   setting for its own threshold, so this command reuses it rather than
   adding a second, redundant threshold setting). Renders one
   `<@member_id> — active N/window days, M channels` line per entry,
   matching `newcomers`'/`inactive`'s exact rendering shape.

2. **`/fetch recognition-candidates`** — staff-only. Fetches
   `store.list_ledger_events_for_guild(guild_id)`, calls
   `recognition_candidates(guild_id, events, CohortWindow.THIRTY_DAY, now)`.
   Renders `<@member_id> — reason1, reason2` per entry (joining the
   candidate's `reasons` tuple with `", "`).

3. **`/fetch member delete <member>`** — staff-only, **not** staff-or-self
   (matches the design spec's "staff-triggered only"). Two-call
   `confirm: bool = False` pattern copied exactly from
   `content_commands.notification_retract`'s shape: on the first call, return
   `CONFIRMATION_REQUIRED` with a preview card naming the target member and
   warning the action is irreversible; on `confirm=True`, call
   `activity_privacy.delete_member(store, guild_id, member_id,
   requested_by=actor.member_id, now=now)` and render the resulting
   `ActivityReceipt`'s `receipt_id` and `created_at` — never a table list or
   row count, matching the "redacted receipt" requirement.

4. **`/fetch member export [member]`** — staff-or-self, matching
   `activity`/`milestones`' existing self-view pattern exactly (self_view
   computed server-side in the service, never trusted from an omitted
   Discord option default). When `self_view` is `False` (staff exporting on
   another member's behalf), writes an audit receipt via
   `store.record_activity_receipt` (action `"member_data_exported"`, detail
   `{"requested_by": actor.member_id, "member_id": target.member_id}`) before
   returning the export — matching `delete_member`'s existing receipt-write
   shape, since no such call exists on the export path today. Delivers the
   `MemberExportPackage` as an ephemeral attached file (JSON), not inline in
   the embed — an export can contain arbitrarily many events, unlike the
   bounded list commands above, so it does not fit the "one line per entry in
   an embed description" shape at all; this is a structurally different
   rendering need, not a deviation from convention for its own sake.

5. **`/fetch exclude-channel <channel> <reason>`** — staff-only. Constructs an
   `ExclusionEntry` with `excluded_by=actor.member_id` (the real invoking
   staff member — the gap identified above) and calls
   `save_exclusion_entry`. No confirmation step (upserting an exclusion is
   reversible by excluding a different channel or re-running the command;
   unlike deletion, nothing is destroyed).

6. **`/fetch exclusions`** — staff-only, read-only companion to #5. Calls
   `list_exclusion_entries(guild_id)`, renders `#<channel_id> — <reason>
   (excluded by <@excluded_by> at <timestamp>)` per entry. Added because a
   configure-only command with no way to view current state is an
   incomplete feature, and the read path (`list_exclusion_entries`) already
   exists and is already tested — this is a small, low-risk addition to the
   same command, not scope creep into a new subsystem.

## Explicit Exclusions

- No pagination infrastructure — truncation only, per above.
- No self-service deletion — the design spec is explicit that deletion is
  staff-triggered only; a member wanting their own data deleted asks staff,
  who runs `/fetch member delete`.
- No change to `ActivityRuntime`'s existing default-exclusion seeding
  behavior — it continues writing the bot's own application ID for
  auto-seeded rows; this spec's new command is a second, independent write
  path for staff-initiated exclusions, not a replacement.
- No retroactive audit receipt added for self-exports (already-shipped
  behavior; the design spec only requires auditing the staff-on-behalf-of
  path).

## Testing

Matching `tests/test_activity_commands.py`'s existing conventions
(`ActivityActorContext` constructed directly, no `discord.Interaction`
mocking, real `SQLiteStore` via `tmp_path`, deterministic `now=lambda: NOW`):
staff-only denial for all six commands when `is_staff=False`; `returning`
and `recognition-candidates` render real fixture data correctly and truncate
past the entry cap; `member delete` requires `confirm=True` to actually
delete, is idempotent on a second `confirm=True` call, and the resulting
receipt contains no table/row-count detail; `member export` self-view never
writes an audit receipt, staff-on-behalf-of view does, and the exported file
never contains another member's data (reusing the existing structural test's
assertion style); `exclude-channel` records the real invoking staff member's
ID, not the bot's application ID; `exclusions` renders an empty and a
populated state correctly.

## Completion Gate

Complete when: all six commands exist, staff-gated correctly (five
staff-only, one staff-or-self); member deletion requires explicit
confirmation and produces only a minimal redacted receipt; member export
audits the staff-on-behalf-of path and never leaks cross-member data;
channel exclusion records the real acting staff member, not the bot's own
ID; every command's list rendering is defensively truncated against
Discord's embed size limit; and every property above is covered by an
automated test.
