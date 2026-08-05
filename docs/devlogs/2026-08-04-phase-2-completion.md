# Krubit Development Log: Phase 2 Completion

**Date:** August 4-5, 2026
**Status:** Automated implementation and verification through Task 14 complete. No live
Discord or platform canary has run in this development environment; see the
[Phase 2 completion audit](../operations/phase-2-completion-audit.md) for exactly what
is and is not evidenced.

## Scope

This effort completes Phase 2 (the Creator Signal and Notification Hub) beyond the
Twitch/Discord-presence slice delivered in Phase 2A. It adds a creator registry,
per-platform connector catalog, normalized content ledger, cross-platform correlation,
notification policy (quiet hours, mention budgets, batching via correlation windows),
shared Discord delivery, YouTube/X/Bluesky/Instagram/Facebook/Threads/TikTok/Fanbase
connectors, Discord Scheduled Event synchronization, the full `/fetch` command surface
from the design doc, and — in this final task — operator documentation, rollout
controls, and a completion audit.

Krubit remains a non-conversational companion: no capability added in this phase
generates commentary, infers sentiment, or acts without a durable, guild-scoped
receipt.

## Delivered implementation, by task

| Task | Delivered |
|---|---|
| 1 | Connector catalog and URL recognition for all 10 platforms |
| 2 | Guild-scoped creator registry with owner/admin authority and redacted receipts |
| 3 | Connector protocol, `CredentialVault` (AES-GCM sealed OAuth grants), and the callback-ingress route builders |
| 4 | Normalized content ledger, idempotent lifecycle transitions |
| 5 | Notification policy: quiet hours (DST-correct), mention budgets, template validation |
| 6 | Discord delivery engine: durable sends, edits, retries, retractions, recovery |
| 7 | YouTube connector: uploads polling, quota-aware backoff, push-Atom parsing |
| 8 | X and Bluesky connectors: original-post polling, quote-post exclusion |
| 9 | Meta connectors: Instagram, Facebook Pages, Facebook profiles, Threads, OAuth authorization flow |
| 10 | TikTok and Fanbase: OAuth/Display API connector and dormant pending-capability handling |
| 11 | Discord Scheduled Event synchronization with exact-ID recovery |
| 12 | Unified `/fetch creator`/`/fetch notifications` command surface |
| 13 | Unified polling scheduler and idempotent Twitch-to-content-ledger migration |
| 14 | Operator runbook, rollout env vars, completion audit (this task) |

## Commit sequence (Tasks 1-13, most recent last)

```text
899f7a4  feat: add creator connector catalog
6185dbd  fix: mark Twitch live capability unconfigured until credentials exist
3432e20  feat: persist creator registry
924802b  feat: define social connector contracts
1eecbbb  fix: redact secrets from callback error logs and Settings repr
b928368  feat: add durable creator content ledger
2921a15  fix: claim a fresh content delivery per publish/live transition
11ba8e8  feat: add creator notification policy
e166c8e  fix: close decide/claim TOCTOU gap in mention budget consumption
55d757a  feat: deliver unified creator notifications
d0503b5  fix: record edit-failure receipts and stop double-claiming mentions on replay
d64ff81  feat: monitor YouTube creator content
5b6607b  feat: monitor X and Bluesky posts
3611057  fix: exclude X quote-tweets from surfaced content
7775663  feat: monitor authorized Meta creator content
1ec0524  fix: enforce owner/admin authority for Meta OAuth authorization completion
7fa7b3c  fix: implement concrete Meta token exchange and redact OAuth grant secrets
cb75fb3  feat: add TikTok and Fanbase capabilities
863aa39  feat: sync creator scheduled events
8487e0c  fix: record durable receipts on scheduled event Discord failures
b63a8b2  feat: add creator notification controls
e9cf3d9  fix: audit route changes, clarify remove/verify command copy
11bb93f  feat: run unified creator signal hub
a576cc4  fix: isolate scheduler cycle failures and honor real rate-limit headers
```

## New operator-facing surfaces

- Env vars: `KRUBIT_CREATOR_SIGNALS_ENABLED`, `KRUBIT_SOCIAL_DELIVERY_ENABLED`, and the
  full per-platform credential set (`YOUTUBE_KRUBIT_*`, `X_KRUBIT_BEARER_TOKEN`,
  `META_KRUBIT_*`, `TIKTOK_KRUBIT_*`, `KRUBIT_CREDENTIAL_ENCRYPTION_KEY`,
  `KRUBIT_CALLBACK_PUBLIC_BASE_URL`, `KRUBIT_CALLBACK_PORT`) — all optional, all
  default to leaving the corresponding capability `unconfigured` rather than blocking
  startup.
- Commands: `/fetch creator add|remove|list|show|verify|pause|resume|route|transfer|template`,
  `/fetch notifications`, `/fetch notifications preview|retry|retract`, `/fetch latest`,
  `/fetch schedule`.
- Discord resources: `#social-notifications` channel and `Creator` role, resolved once
  by exact name alongside the existing `#live-notifications`/`Streaming Now` pair.
- Full operator setup, remediation, rollback, and data-deletion guidance in the new
  [Phase 2 operations guide](../operations/phase-2-creator-signal-hub.md).

## Three build gaps documented prominently for operators

All three are deliberate, reviewed safety choices or straightforwardly missing wiring,
not oversights, and are called out at the top of the operator runbook rather than
buried:

1. **Meta and TikTok connectors are not scheduled.** Each needs a per-account OAuth
   token resolved at poll time, not the single bot-wide token `Settings` provides for
   YouTube/X. Wiring a shared token would fetch every enrolled creator's content under
   one account's credentials — an active correctness/privacy bug, not a missing
   feature. Instagram/Facebook/Threads/TikTok are fully implemented and tested against
   recorded fixtures but do not run in production in this build.
2. **The OAuth/push callback server is never started.** `CallbackServer` and its route
   builders are implemented and tested against a real `aiohttp` application, and
   `Settings` already validates `KRUBIT_CALLBACK_PUBLIC_BASE_URL`/`KRUBIT_CALLBACK_PORT`,
   but `krubit run` never constructs or starts a `CallbackServer`. YouTube push,
   Meta/TikTok OAuth redirects, and Meta signed webhooks have nowhere to land.
3. **Discord Scheduled Event synchronization has no production call site.**
   `ScheduledEventSynchronizer` is implemented and tested (including restart-safe,
   exact-ID-only recovery), but nothing in `bot.py`, `__main__.py`, or
   `content_runtime.py` ever constructs or calls it. `/fetch schedule` will always
   report no Krubit-owned events in this build, regardless of what is scheduled on any
   platform. Found by the whole-branch final review (see below), not caught by this
   task's own review.

A whole-branch final review performed after this devlog was first written also found
and fixed a fourth, more serious composition-level defect: `KRUBIT_CREATOR_SIGNALS_ENABLED`
and `KRUBIT_SOCIAL_DELIVERY_ENABLED` were parsed and validated
(`src/krubit/config.py`) but never actually read anywhere else in `src/` — so leaving
both at their documented `false` default did not, in fact, prevent connector polling or
public Discord delivery. Both flags are now genuinely enforced; see the [completion
audit's
addendum](../operations/phase-2-completion-audit.md#addendum-final-whole-branch-review-fix-wave)
for the complete list of fixes from that review, including two migration-safety fixes
(a boot-crashing owner conflict and a silent re-pause on every restart) and an
authority-check gap in `notification_preview`.

## Verification performed in this session (Task 14)

```text
.venv\Scripts\python.exe -m pytest -q          -> 575 passed
.venv\Scripts\ruff.exe check .                 -> All checks passed!
.venv\Scripts\pyright.exe --pythonpath .venv\Scripts\python.exe  -> 0 errors, 0 warnings
git diff --check                               -> clean
```

`scripts\invoke-krubit.ps1 doctor` does not exist in this build (confirmed by running
it and recording the argparse error in the audit). No live Discord guild, bot token, or
real platform credential was available in this sandboxed development session, so no
live `/fetch` command and no per-connector production canary was run. See the
[completion audit](../operations/phase-2-completion-audit.md) for the full, line-by-line
mapping of the design doc's Completion Gate against evidence gathered in this session
versus items explicitly deferred to a credentialed operator post-merge.

## Known carried-forward limitations

The full, itemized ledger of limitations surfaced during Tasks 1-13 code review is
preserved verbatim in the [completion audit](../operations/phase-2-completion-audit.md)
and the [operator runbook](../operations/phase-2-creator-signal-hub.md); the highest-
impact items are:

- No guild-level `NotificationPolicy` configuration storage — every guild currently
  gets unlimited mention budgets and no quiet hours by default. (Still open after the
  final-review fix wave — judged out of scope for a review-fix pass; see the runbook.)
- `extract_streaming_observation` (the shared Twitch/YouTube presence extractor) has no
  production call site; `handle_presence` still calls the Twitch-only extractor
  directly.
- TikTok's consumed-OAuth-nonce set grows unboundedly (never pruned after TTL).
- `FacebookPageConnector.fetch_page` re-polls three bounded edges rather than
  paginating (`next_cursor` is always `None`).

None of these block the automated verification above; they are documented so they are
not mistaken for resolved.
