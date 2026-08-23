# Moderation Discord Integration Design (Phase 5+6 reconciliation, slice 4 of 4)

**Status:** Approved
**Scope:** Real Discord `ActionExecutor` (timeout only — kick/ban never
autonomously executable, per the roadmap's Phase 5 boundary), a new
`/fetch moderation` command group for human approval/review, and
`#mod-room` notifications posted to a dedicated, independently-configured
channel. No Zariya-side client code — KAI-Studio already built that
independently against slice 3's HTTP transport.

## Background

Slices 1-3 (merged) built the typed contract, a storage-backed
`ModerationService` implementing all 11 Zariya-facing operations, and an
authenticated HTTP transport with a `NoOpActionExecutor` stand-in. This
slice replaces the stand-in with a real Discord executor and gives human
staff the approval/review surface the whole adapter has been building
toward — nothing has executed a real Discord mutation anywhere in this
codebase before this slice (confirmed: Phase 3's watchdog module has a
structural safety test proving it never calls a Discord moderation-mutation
method; this slice is the first code that does).

Scoped down from the full original handoff wishlist, given this is already
the largest slice of the four: covers `pending`, `approve`, `reject`,
`history`, `case` (5 commands). Timeout **renewal/early-release** and
**appeal review** commands are explicitly deferred to a follow-up slice.

## 1. Closing the action-structure gap

`recommended_action` (free text, e.g. "24h timeout") stays evidence-only —
never parsed to derive an executable action. `ModerationService.record_human_decision`
(internal, already exists) gains a required `timeout_duration: timedelta`
parameter when `decision == ApprovalDecision.APPROVED` (omitted/`None` when
`REJECTED`). On approval, it computes `action_expiration = self._clock() +
timeout_duration` and sets it on the case via `dataclasses.replace` —
`ModerationCase.action_expiration` already exists (slice 1), currently
always `None`; this is the first code path that ever sets it. This is the
sole source of truth the executor reads to know how long to time a member
out for.

`record_human_decision`'s new signature:

```python
async def record_human_decision(
    self,
    case_id: str,
    *,
    guild_id: int,
    reviewer_id: int,
    decision: ApprovalDecision,
    timeout_duration: timedelta | None = None,
) -> ModerationCase:
```

Validation: `timeout_duration` is required (raises `ValueError`) when
`decision is ApprovalDecision.APPROVED`; ignored (may be `None`) when
`REJECTED`. Reuses the existing `case.guild_id != guild_id` tenant check
already established in every other mutating method (this method predates
that fix — slice 4 also closes that gap, matching the pattern from slices
5/6/7 of the review-found guild-check series).

## 2. `DiscordActionExecutor` — new module `src/krubit/discord/moderation_executor.py`

```python
class DiscordActionExecutor:
    """Real Discord-mutation ActionExecutor. Timeout only -- kick and ban
    are never reachable through this class, matching the roadmap's Phase 5
    boundary ("Kick and permanent ban remain unavailable to autonomous
    execution in this phase regardless of configuration").
    """

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        if case.action_expiration is None:
            raise ValueError(
                f"case {case.case_id} has no action_expiration set -- "
                "record_human_decision must run before execute_approved_action"
            )
        receipt_detail_ok = f"timed out until {case.action_expiration.isoformat()}"
        guild = self._bot.get_guild(case.guild_id)
        if guild is None:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"guild {case.guild_id} not found or bot not a member",
            )
        member = guild.get_member(case.member_id)
        if member is None:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"member {case.member_id} not found in guild (left?)",
            )
        try:
            await member.timeout(
                case.action_expiration, reason=f"Krubit moderation case {case.case_id}"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )
        return ActionReceipt(
            receipt_id=f"receipt:{uuid.uuid4().hex}", succeeded=True, detail=receipt_detail_ok
        )
```

Per a slice-2 fix, `ModerationService.execute_approved_action` never trusts
an executor-supplied `receipt_id` — it always overwrites whatever the
executor returns with its own `self._receipt_id_factory()` call, for both
success and failure outcomes. `DiscordActionExecutor.execute` therefore
still populates `receipt_id` on the `ActionReceipt` it returns (the type
requires a value), but that value is discarded by the caller; only
`succeeded` and `detail` are load-bearing. The plan pins the exact current
call site before implementing, since this ownership rule already lives in
`services/moderation.py` and must not be duplicated or contradicted here.

Guild/member resolution failures (bot not in guild, member left) and
Discord API failures (missing `Moderate Members` permission, rate limit,
etc.) all produce an honest `succeeded=False` receipt with a real reason —
never a silent success, matching `NoOpActionExecutor`'s existing contract.

### Production wiring

`__main__.py` currently builds the callback server (and therefore
`ModerationService` with `NoOpActionExecutor`, per slice 3's
`web/wiring.py`) *before* constructing `KrubitBot`. `DiscordActionExecutor`
needs a live `discord.Client`/`KrubitBot` instance to resolve guilds and
members. Two ways to close this ordering gap; the plan picks the less
invasive one after checking `__main__.py`'s actual current structure:

- **(a)** Construct `KrubitBot` first, then build `ModerationService`/the
  callback server with `DiscordActionExecutor(bot)`, since a `discord.Client`
  is usable (its methods can be called, though the cache is empty) before
  `bot.start()` actually connects — `get_guild`/`get_member` simply return
  `None` until the gateway connection populates the cache, which
  `DiscordActionExecutor.execute` already treats as a normal failure case,
  not a crash.
- **(b)** Build the callback server with a lazily-resolved executor — a
  thin wrapper whose `_bot` attribute is set once `KrubitBot` exists, if
  `__main__.py`'s existing ordering can't be safely reversed without wider
  changes.

## 3. `/fetch moderation` — new sibling `app_commands.Group` in `discord/bot.py`

Added via `self.add_command(ModerationCommands(self))` alongside the
existing `self.add_command(AdminCommands(self))` /
`self.add_command(SniffCommands(self))` lines — a new top-level group, not
nested inside the near-full `AdminCommands` (17/25 slots already used).
Staff-gated using the exact same `authorize`/`finish` pattern already
established on `FetchCommands`/`AdminCommands` (`manage_guild` permission,
ephemeral defer-then-respond flow):

- `/fetch moderation pending` — `ModerationService.get_pending_reviews(guild_id, ...)`, rendered as a list card.
- `/fetch moderation approve <case_id> <duration>` — `duration` as a Discord
  command parameter (string, parsed as e.g. `24h`/`7d` using a small parser
  — reuse `KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS`-style int-hours parsing
  precedent if a duration-parsing helper doesn't already exist in this
  codebase; the plan confirms via search before writing one). Calls
  `record_human_decision(case_id, guild_id=..., reviewer_id=actor_id,
  decision=APPROVED, timeout_duration=parsed_duration)`, then
  `execute_approved_action` with a fresh idempotency key
  (`f"admin-approve:{case_id}:{uuid4().hex}"`), reports the resulting
  receipt (success/failure) back to the approving staff member.
- `/fetch moderation reject <case_id> <reason>` — `record_human_decision(...,
  decision=REJECTED)`; `reason` is logged/evidence text, not currently a
  named field on `ModerationCase` (matches `CloseIncidentRequest.decision`'s
  existing "accepted but not persisted structurally" precedent from slice
  2 — acceptable, not a new gap).
- `/fetch moderation history <member>` — `get_member_moderation_history`.
- `/fetch moderation case <case_id>` — combines `get_incident_status` and
  (when a `receipt_state` exists) `get_action_receipt` into one card.

## 4. `#mod-room` — new `Settings.moderation_channel_id: int | None`, independently configured

`KRUBIT_MODERATION_CHANNEL_ID` — a separate env var from
`KRUBIT_STAFF_CHANNEL_ID`, following user preference for independent
configurability. Resolution is by numeric Discord channel ID (renaming the
channel in Discord's UI never breaks this — IDs are stable across renames).
Posts mirror `WatchdogRuntime.notify_staff`'s exact gate order and failure
handling: feature-flag check → channel resolved via
`guild.get_channel(moderation_channel_id)` → sendable-channel and
permission check (`view_channel`/`send_messages`/`embed_links`) →
`channel.send(embed=...)` → catch `discord.HTTPException`/`Forbidden`/`NotFound`,
log, never raise. A notification failure never invalidates the already-committed
ledger entry (the write happens first, in `ModerationService`; notification
is a separate, best-effort step after).

Two trigger points, both driven from `ModerationCommands`/`ModerationService`
call sites (not from inside `services/moderation.py` itself, which stays
framework-independent per its own module docstring — Discord notification
logic lives in the `discord/` package, called by the command handlers, not
by the service layer):

- A case enters `APPROVAL_REQUIRED` (posted after `request_human_approval`
  succeeds) — the pending-review announcement.
- A case reaches `EXECUTED`/`EXECUTION_FAILED` (posted after
  `execute_approved_action` completes, from the `/fetch moderation approve`
  command handler) — the outcome receipt.

## Non-goals for this slice

No timeout renewal/early-release commands. No appeal-review command. No
kick/ban execution path, ever, through this adapter (per the roadmap's
Phase 5 boundary — those remain staff/Zariya-triggered via existing Discord
tooling outside this adapter entirely). No KAI-Studio/Zariya-side code.

## Testing

`tests/test_moderation_executor.py`: `DiscordActionExecutor` against a
fake/mocked `discord.Client`/`Guild`/`Member` (following whatever mocking
convention this codebase's existing Discord-command tests already use —
the plan investigates `tests/test_watchdog_commands.py` or similar before
writing new mocks) — success path, guild-not-found, member-not-found,
`discord.Forbidden`, `discord.HTTPException` all produce the correct
`ActionReceipt`.

`tests/test_moderation_commands.py`: the five `/fetch moderation` commands
against a real `ModerationService` (in-memory-backed, matching
`test_moderation_service.py`'s existing fixture convention) and a fake
executor — authorization gating (non-staff rejected), each command's happy
path, `approve` threading a parsed duration through to
`action_expiration`, and `#mod-room` notification firing (or not, when
`moderation_channel_id` is unset) at both trigger points.

`tests/test_moderation_service.py`: extend for `record_human_decision`'s
new `timeout_duration` parameter and its guild-check addition.
