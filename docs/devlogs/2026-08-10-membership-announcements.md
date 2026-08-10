# Krubit Development Log: Membership Join/Leave Announcements

**Date:** August 10, 2026
**Status:** Implementation, fix wave, and final review complete. Merged to `main`.

## Scope

Automatic, convention-based announcements when a member joins or leaves
the guild: a factual message to a channel literally named `welcome` on
join, and to a channel literally named `staff-notes` on both join and
leave. No configuration, no toggle command, no new storage — a channel's
mere presence or absence by exact name is the only control, deliberately
matching the existing live-stream-announcement feature's
`LIVE_CHANNEL_NAME` convention. Enable/disable control for this and every
other Krubit feature is explicitly deferred to a future web dashboard;
see [the design spec](../superpowers/specs/2026-08-10-membership-announcements-design.md)
for the full reasoning behind that scope-narrowing decision.

## Delivered

`MembershipAnnouncementRuntime` — a new, small, dependency-free runtime
module wired into `KrubitBot`'s existing `on_member_join`/
`on_member_remove` handlers, alongside the existing Watchdog/Activity
Ledger calls.

## Final review and fix wave

The final whole-branch review found a real Critical bug: the feature
never checked `guild_is_enabled` — the per-guild kill switch every other
Discord-writing runtime in this codebase honors, and which defaults to
**off**. A guild that had installed Krubit but never explicitly enabled
it via `enable-guild` would still get public `#welcome` posts, while
every other feature stayed correctly silent. This directly contradicted
the project's stated shadow-canary posture and was not the deferred
configuration system — it was a switch that already existed everywhere
else. Fixed by threading the store into the runtime and checking
`guild_is_enabled` before any channel lookup.

Three Important findings were also fixed in the same pass: no pre-send
permission check (meaning a mispermissioned channel would produce a
silent, permanently-repeating 403 on every join, forever); voice/stage
channels wrongly passed the duck-typed "text channel" check (fixed with
an explicit `isinstance` exclusion, and the test that claimed to prove
this was corrected — it had been testing an object with no `.send`
method at all, not an actual voice channel); and the `#staff-notes` leave
message writes member data with no deletion path or privacy-policy
coverage (documented, not solved — an accepted, noted gap for a channel
that already predates this feature's own retention discipline).

## Known limitations

- The `#staff-notes` leave message's data is not covered by `/fetch
  admin member-delete`'s deletion guarantee — documented in the module
  docstring and `docs/PRIVACY_POLICY.md`, not fixed.
- A guild that happens to already have channels literally named
  `welcome` or `staff-notes` for unrelated purposes will start receiving
  these messages with no warning — an accepted sharp edge of the
  zero-configuration design.

## Test evidence

Full suite: 1141/1141 passing at merge. Ruff clean throughout.
