# Phase 1 Closeout

**Closed:** August 4, 2026
**Disposition:** Accepted for progression to Phase 2

## Outcome

Phase 1 is complete. Krubit is operating as a read-only Discord companion in Krucial Town, with Zariya retaining community interpretation, recommendations, member interaction, and moderation judgment.

The owner accepted the initial live canary after all staff smoke commands completed without a visible failure. Automated coverage, live database evidence, the Zariya inventory comparison, and the remaining platform limitations are recorded below.

## Automated verification

- `62 passed` from the complete pytest suite.
- Ruff reports `All checks passed!`.
- Pyright reports zero errors, warnings, or informational findings.
- The dedicated Phase 1 command test proves `/fetch status` is guild-only, Manage Server restricted, ephemeral, and actor-receipted.
- Daily-summary tests prove guild/day deduplication, disabled delivery, disabled-guild isolation, missing-channel handling, missing-permission handling, failed-delivery handling, and durable receipts.
- Snapshot tests prove deterministic hashing, guild isolation, readable ID-aware differences, and restore-preview non-mutation.
- Event and storage tests prove replay deduplication, tenant scoping, and secret redaction.

## Live evidence

- Exactly one launcher-managed runtime chain is active. On Windows this consists of one PowerShell launcher, one Python trampoline, and one Python worker.
- The active runtime's stdout and stderr logs are empty.
- SQLite `PRAGMA quick_check` reports healthy.
- The live database contains 27 action receipts, four accepted events, one configuration snapshot, and zero duplicate event IDs.
- Successful receipts exist for `/fetch status`, `/fetch test-card`, `/fetch server-health`, `/fetch changes`, `/fetch permissions`, `/fetch integrations`, `/fetch backup status`, `/fetch backup create`, and `/fetch backup preview`.
- The role-change smoke test produced one `role_updated` event.
- The saved snapshot contains 50 roles and 45 channels with a stable integrity hash.
- A SQLite-consistent temporary copy of the live database was reinitialized successfully with every count preserved and all required tables present. The live database was not modified by this migration check.
- Daily summary delivery remains intentionally disabled because no private staff channel is configured. The scheduler records no unsolicited message, and all delivery outcomes are covered by automated tests.

## Zariya comparison

Krubit's snapshot was compared with Zariya's latest read-only audit rather than modifying either system to force matching results.

| Inventory item | Krubit | Zariya | Result |
|---|---:|---:|---|
| Categories | 9 | 9 | Match |
| Text and announcement channels | 27 | 27 | Match |
| Voice and stage channels | 7 | 7 | Match |
| Forum and media channels | 2 | 2 | Match |
| Scheduled Events | 0 | 0 | Match |
| Roles | 50 | 49 | One-role timing/install difference |
| AutoMod rules | 0 visible | 1 | Limited by Discord permission requirements |

The snapshots were captured at different times. The one-role delta is retained as a coverage note and is consistent with Krubit's installation and the intervening role smoke test; it is not treated as corruption.

## Intentional Discord coverage limits

Discord requires `Manage Server` to list AutoMod rules and `Manage Webhooks` to list guild webhooks. Both permissions can modify server resources and are intentionally excluded from Krubit's read-only Phase 1 role.

Krubit therefore records explicit `limited` coverage with the Discord `403` result for these sections. It does not silently claim full visibility. Krubit can still record supported gateway change events that Discord delivers under its granted access.

These limits are accepted because Phase 1 prioritizes least privilege over complete inventory. A later approval-gated architecture may use Discord audit evidence or a separately governed permission path without giving the ordinary Krubit runtime mutation authority.

## Acceptance-criteria disposition

- Supported gateway changes are stored exactly once: **passed by tests and live event evidence**.
- Snapshots are deterministic and guild-isolated: **passed by tests and live integrity evidence**.
- Changes are readable: **passed by card/diff tests and live smoke check**.
- Missing access is visible: **passed; AutoMod and webhook limitations are explicit**.
- Every command is staff-only and receipted: **passed after closeout correction and regression coverage**.
- Restore previews cannot mutate Discord: **passed by service test and live smoke check**.
- Daily summaries cannot duplicate: **passed by database uniqueness and scheduler tests**.
- Automated tests and static checks pass: **passed**.
- Live commands work without disrupting Zariya: **accepted by the owner after smoke testing**.

## Rollback

If a regression appears, stop the exact launcher-managed runtime chain and restart the last accepted Phase 1 commit through `scripts/invoke-krubit.ps1`. Preserve the database, logs, and action receipts for diagnosis; do not delete the SQLite database, WAL, or SHM files.
