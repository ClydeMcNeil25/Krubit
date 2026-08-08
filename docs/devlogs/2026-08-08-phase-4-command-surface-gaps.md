# Krubit Development Log: Phase 4 Command Surface Gaps

**Date:** August 8, 2026
**Status:** Implementation, fix wave, and final whole-branch review complete.
Branch is **not merged and not pushed** — implemented autonomously overnight
per the project owner's explicit request while they were unavailable; awaits
their review and go-ahead. No live Discord guild was available in this
session; all verification is via the automated test suite.

## Scope

Phase 4 (Activity Ledger) shipped five capabilities with no way for staff to
reach them: `returning_member_view`, `recognition_candidates`, member
deletion, member export, and channel-exclusion configuration were all fully
implemented and tested at the service/storage layer, but no `/fetch` command
ever called them. This effort closes that gap with six new commands, per
[the design spec](../superpowers/specs/2026-08-08-phase-4-command-surface-gaps-design.md).

## A real architectural blocker, found and resolved mid-implementation

Task 1 discovered Discord enforces a hard 25-child-per-command-group limit,
and `FetchCommands` was already at 24. Adding six new flat `/fetch <name>`
commands as originally designed would have broken the build. With the
project owner unavailable to consult, the controller made a unilateral call:
introduce a new `ActivityAdminCommands` subgroup (`/fetch activity-admin
<name>`) housing all six commands, adding exactly one new child to
`FetchCommands` (25/25) — rather than the alternative of renaming any of the
24 already-shipped commands to free a slot, which would have changed
production command paths without the owner present to approve it.

The final whole-branch review independently assessed this as sound
engineering under the circumstances, but flagged it for the owner's explicit
sign-off since it deviates from the design spec's original flat command
list — **this decision needs review, not just a note in a devlog.** The
review also flagged a consequence worth planning around: `/fetch` is now
genuinely full; the next new flat command will hit the same wall, with no
cheap escape left.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | `/fetch activity-admin returning`, `/fetch activity-admin recognition-candidates` — staff-only read-only views; discovered and resolved the 25-child-cap blocker |
| 2 | `/fetch activity-admin member-delete` — staff-only, two-call confirmation, minimal redacted receipt |
| 3 | `/fetch activity-admin member-export` — staff-or-self, JSON file attachment (first `/fetch` command to send one), audit receipt on the staff-on-behalf-of path only |
| 4 | `/fetch activity-admin exclude-channel`, `/fetch activity-admin exclusions` — fixes a real bug where the only prior caller of `save_exclusion_entry` always recorded the bot's own application ID instead of the acting staff member |

## Final review and fix wave

The final whole-branch review found two Important issues, both fixed and
independently reverified in a scoped re-review:

1. **List-rendering truncation was entry-count-based, not character-based** —
   `exclusions` (up to 300-char reasons) and `recognition-candidates`
   (unbounded reasons per candidate) could each produce an embed description
   exceeding Discord's 4096-character limit well before hitting the 40-entry
   cap, causing a raw API error. Fixed with a character-budget-based
   truncation (~3900 chars) shared by all three list commands, with tests
   proving it's load-bearing (a naive render of the same fixture data is
   asserted to exceed the safe threshold, so the fix demonstrably matters).
2. **`exclude-channel` could raise an unhandled `ValueError`** on a blank or
   over-length exclusion reason, escaping the Discord command handler
   entirely. Fixed by catching it and returning `CommandStatus.FAILED`,
   matching this codebase's established `creator_add` precedent, plus a
   `discord.py` client-side `Range[str, 1, 300]` constraint on the option
   itself (verified correct against the pinned discord.py 2.7.1 by directly
   inspecting the resolved command payload, not just assumed).

Two lower-priority fixes were also applied within the bounds the controller
set for unattended work: a defense-in-depth cross-guild guard on
`delete_member`/`export_member`, and a payload-size reduction on exports
(dropping JSON indentation).

## Known limitations that change what this build actually does

- **`ActivityAdminCommands`'s naming and path is a controller decision made
  without the project owner present — it needs their explicit sign-off**,
  not just a devlog mention. See "A real architectural blocker" above.
- **`/fetch` is now at Discord's 25-child cap.** Any future flat command
  addition needs a deliberate reorganization plan (e.g. moving more existing
  commands into subgroups) — there is no cheap next step.
- **`member-export`'s audit receipt is written before the Discord file send
  can fail.** If a future export exceeds Discord's attachment size limit,
  the receipt would record an export the requester never actually received.
  A payload-size mitigation (dropping JSON indentation) was applied, but the
  underlying ordering issue is a documented, deliberate follow-up — the
  controller was explicitly instructed not to attempt a larger redesign of
  the receipt-then-send ordering unattended.
- **No live Discord guild was available in this development session.** Every
  property in this devlog is evidenced by the automated test suite (1102
  tests, all passing) and direct code inspection — not by an end-to-end run
  against a real Discord server. Discord-layer authority gating
  (`authorize()` correctly wired to all five staff-only commands,
  `member-export` correctly staff-or-self) is verified by code trace during
  review, not by an automated test — this module's established convention
  tests authority at the framework-independent service layer only.
