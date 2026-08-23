# Moderation Discord Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace slice 3's `NoOpActionExecutor` with a real Discord timeout
executor, add a `/fetch moderation` command group for human approval/review,
and post `#mod-room` notifications — slice 4 of 4 of the Zariya-Krubit
moderation adapter (Phase 5+6 reconciliation).

**Architecture:** `DiscordActionExecutor` (real, timeout-only) and
`LazyDiscordActionExecutor` (a bindable wrapper solving the boot-order
problem — the HTTP callback server is built before `KrubitBot` exists in
`__main__.py`) live in a new `discord/moderation_executor.py`.
`record_human_decision` gains a `timeout_duration` parameter. A new
`ModerationCommands` group (sibling to `AdminCommands`/`SniffCommands`
under `/fetch`) exposes 5 staff commands. A new `ModerationNotifier` mirrors
`WatchdogRuntime.notify_staff`'s exact gate/failure pattern for `#mod-room`.

**Tech Stack:** Python 3.12+, `discord.py` (already a dependency), `pytest` +
`pytest-asyncio`.

## Global Constraints

- Kick and permanent ban are never reachable through this adapter's
  execution path — `DiscordActionExecutor` only ever calls
  `member.timeout(...)`.
- `record_human_decision`'s `timeout_duration` is required when
  `decision is ApprovalDecision.APPROVED`, ignored when `REJECTED`.
- `DiscordActionExecutor.execute` never raises for ordinary failure modes
  (guild/member not found, `discord.Forbidden`, `discord.HTTPException`) —
  it returns `ActionReceipt(succeeded=False, detail=...)` instead, matching
  `NoOpActionExecutor`'s existing honest-failure contract from slice 3.
- `ModerationService.execute_approved_action` (unchanged, slice 2/3 code)
  always regenerates `receipt_id` itself via `self._receipt_id_factory()`
  for both outcomes — whatever `receipt_id` `DiscordActionExecutor` returns
  is discarded by the caller; do not rely on it being used.
- `#mod-room` notifications: feature-flag/channel-configured check first,
  then channel resolution, then a sendable-channel + permission check, then
  `send`, catching `discord.HTTPException`/`Forbidden`/`NotFound` and
  logging — never raising, never blocking the already-committed ledger
  write it follows.
- `moderation_channel_id` is a separate `Settings` field from
  `staff_channel_id` — independently configurable, per explicit user
  request during design.
- No new third-party dependencies.

---

## File Structure

- Modify: `src/krubit/services/moderation.py` — `record_human_decision`
  gains `timeout_duration`.
- Modify: `tests/test_moderation_service.py` — new tests for the above.
- Create: `src/krubit/discord/moderation_executor.py` —
  `DiscordActionExecutor`, `LazyDiscordActionExecutor`.
- Create: `tests/test_moderation_executor.py`.
- Modify: `src/krubit/config.py` — `moderation_channel_id` field.
- Modify: `tests/test_config.py` — new test for the above.
- Create: `src/krubit/discord/moderation_notifier.py` — `ModerationNotifier`.
- Create: `tests/test_moderation_notifier.py`.
- Modify: `src/krubit/web/wiring.py` — `build_callback_routes` accepts an
  optional `executor` parameter.
- Modify: `src/krubit/discord/bot.py` — new `ModerationCommands` group,
  `KrubitBot`/`FetchCommands` thread a `moderation_executor` parameter
  through to it.
- Modify: `src/krubit/__main__.py` — construct `LazyDiscordActionExecutor`
  once, pass to both wiring paths, bind after `KrubitBot` construction.
- Create: `tests/test_moderation_commands.py`.

---

### Task 1: `record_human_decision` gains `timeout_duration`

**Files:**
- Modify: `src/krubit/services/moderation.py:244-265`
- Modify: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: existing `ModerationCase`, `ApprovalDecision`, `transition`,
  `replace` (all already imported).
- Produces: `record_human_decision(self, guild_id: int, case_id: str, *,
  reviewer_id: int, decision: ApprovalDecision, timeout_duration: timedelta
  | None = None) -> ModerationCase`. Sets `case.action_expiration` to
  `self._clock() + timeout_duration` when `decision is
  ApprovalDecision.APPROVED`. Also produces `async def get_case(self,
  guild_id: int, case_id: str) -> ModerationCase | None` — a small addition
  filling a real gap: none of the 11 contract read operations are keyed by
  `case_id` directly (they're keyed by `incident_id`/`receipt_id`/`member_id`),
  but both the `/fetch moderation case` command (Task 6) and the
  `#mod-room` pending-review notification hook (Task 6) need to fetch a
  case's full record — including `member_id` and `recommended_action`,
  which no existing response type carries — directly by `case_id`. Thin
  wrapper over the already-existing `self._store.get_moderation_case`,
  with the same guild-check pattern as every other method in this class.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_service.py
from datetime import timedelta


@pytest.mark.asyncio
async def test_record_human_decision_approve_sets_action_expiration(store: SQLiteStore) -> None:
    fixed_time = datetime(2026, 6, 1, tzinfo=UTC)
    service = ModerationService(store, FakeExecutor(), clock=lambda: fixed_time)
    await _seed_incident(store)
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": "idem:2",
            }
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    updated = await service.record_human_decision(
        100,
        record_response.case_id,
        reviewer_id=999,
        decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    assert updated.action_expiration == fixed_time + timedelta(hours=24)


@pytest.mark.asyncio
async def test_record_human_decision_approve_without_duration_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    await _seed_incident(store)
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    with pytest.raises(ValueError, match="timeout_duration"):
        await service.record_human_decision(
            100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED
        )


@pytest.mark.asyncio
async def test_record_human_decision_reject_ignores_duration(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    await _seed_incident(store)
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    updated = await service.record_human_decision(
        100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.REJECTED
    )
    assert updated.action_expiration is None
    assert updated.status is ModerationStatus.REJECTED
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_service.py -k record_human_decision -v`
Expected: FAIL — `test_record_human_decision_approve_sets_action_expiration`
fails because `action_expiration` stays `None` (no `timeout_duration`
handling yet); `test_record_human_decision_approve_without_duration_raises`
fails because no `ValueError` is raised.

- [ ] **Step 3: Implement**

Replace `record_human_decision` in `src/krubit/services/moderation.py`:

```python
    async def record_human_decision(
        self,
        guild_id: int,
        case_id: str,
        *,
        reviewer_id: int,
        decision: ApprovalDecision,
        timeout_duration: timedelta | None = None,
    ) -> ModerationCase:
        """Internal — not part of the 11-operation Zariya contract. Reserved
        for slice 4's `/fetch moderation` approval commands.
        """
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            raise ValueError(f"unknown case_id {case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        target = (
            ModerationStatus.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ModerationStatus.REJECTED
        )
        if target is ModerationStatus.APPROVED and timeout_duration is None:
            raise ValueError("timeout_duration is required when approving a case")
        new_status = transition(case.status, target)
        action_expiration = (
            self._clock() + timeout_duration if target is ModerationStatus.APPROVED else None
        )
        updated = replace(
            case,
            status=new_status,
            reviewer_id=reviewer_id,
            reviewer_decision=decision,
            action_expiration=action_expiration,
        )
        return await self._store.save_moderation_case(updated)
```

Add `from datetime import timedelta` if not already imported (check the
existing `from datetime import UTC, datetime` line and extend it to
`from datetime import UTC, datetime, timedelta`).

Also add `get_case` to `ModerationService` (place it near the other read
methods, e.g. after `get_pending_reviews`):

```python
    async def get_case(self, guild_id: int, case_id: str) -> ModerationCase | None:
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            return None
        if case.guild_id != guild_id:
            return None
        return case
```

Add a corresponding test:

```python
# append to tests/test_moderation_service.py
@pytest.mark.asyncio
async def test_get_case_returns_full_record(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    case = await service.get_case(100, record_response.case_id)
    assert case is not None
    assert case.member_id == 200


@pytest.mark.asyncio
async def test_get_case_wrong_guild_returns_none(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    assert await service.get_case(999, record_response.case_id) is None
```

Note `get_case` returns `None` on a cross-tenant mismatch rather than
raising `ValueError` (unlike every mutating method) — deliberate: it's a
read-only lookup used by notification/inspection code, where "not visible
to this guild" and "doesn't exist" should behave identically (same
information-disclosure reasoning already established for the HTTP layer's
404-not-400 cross-tenant handling in slice 3), not throw.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: PASS, all tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/services/moderation.py tests/test_moderation_service.py
git commit -m "feat: add timeout_duration to record_human_decision, setting action_expiration on approval"
```

---

### Task 2: `DiscordActionExecutor` and `LazyDiscordActionExecutor`

**Files:**
- Create: `src/krubit/discord/moderation_executor.py`
- Create: `tests/test_moderation_executor.py`

**Interfaces:**
- Consumes: `krubit.domain.moderation.{ModerationCase, ActionReceipt}`;
  `discord.Client`, `discord.Guild`, `discord.Member`, `discord.Forbidden`,
  `discord.HTTPException` (all from the `discord` package, already a
  project dependency).
- Produces: `DiscordActionExecutor.__init__(self, bot: discord.Client) ->
  None`, `async def execute(self, case: ModerationCase, execution_key: str)
  -> ActionReceipt`. `LazyDiscordActionExecutor.__init__(self) -> None`,
  `def bind(self, bot: discord.Client) -> None`, `async def execute(self,
  case: ModerationCase, execution_key: str) -> ActionReceipt` (delegates to
  a `DiscordActionExecutor` built from the bound bot; raises `RuntimeError`
  if called before `bind`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moderation_executor.py
"""Tests for the real Discord ActionExecutor (slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.discord.moderation_executor import DiscordActionExecutor, LazyDiscordActionExecutor
from krubit.domain.moderation import AppealStatus, ModerationCase, ModerationStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EXPIRY = datetime(2026, 1, 2, tzinfo=UTC)


def _case(*, action_expiration=EXPIRY) -> ModerationCase:
    return ModerationCase(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=NOW,
        offense_number=1,
        recommended_action="24h timeout",
        executed_action=None,
        action_expiration=action_expiration,
        status=ModerationStatus.APPROVED,
        review_deadline=None,
        reviewer_id=999,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )


@pytest.mark.asyncio
async def test_execute_succeeds_and_calls_member_timeout() -> None:
    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")

    assert receipt.succeeded is True
    member.timeout.assert_awaited_once_with(EXPIRY, reason="Krubit moderation case case:1")


@pytest.mark.asyncio
async def test_execute_missing_action_expiration_raises() -> None:
    bot = MagicMock()
    executor = DiscordActionExecutor(bot)
    with pytest.raises(ValueError, match="action_expiration"):
        await executor.execute(_case(action_expiration=None), "exec-key-1")


@pytest.mark.asyncio
async def test_execute_guild_not_found_returns_failed_receipt() -> None:
    bot = MagicMock()
    bot.get_guild.return_value = None
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False
    assert "guild" in receipt.detail.lower()


@pytest.mark.asyncio
async def test_execute_member_not_found_returns_failed_receipt() -> None:
    guild = MagicMock()
    guild.get_member.return_value = None
    bot = MagicMock()
    bot.get_guild.return_value = guild
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False
    assert "member" in receipt.detail.lower()


@pytest.mark.asyncio
async def test_execute_forbidden_returns_failed_receipt() -> None:
    member = MagicMock()
    member.timeout = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing permissions")
    )
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False


@pytest.mark.asyncio
async def test_lazy_executor_raises_before_bind() -> None:
    executor = LazyDiscordActionExecutor()
    with pytest.raises(RuntimeError, match="bind"):
        await executor.execute(_case(), "exec-key-1")


@pytest.mark.asyncio
async def test_lazy_executor_delegates_after_bind() -> None:
    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = LazyDiscordActionExecutor()
    executor.bind(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'krubit.discord.moderation_executor'`.

- [ ] **Step 3: Implement**

```python
# src/krubit/discord/moderation_executor.py
"""Real Discord ActionExecutor for the moderation adapter (Phase 5+6 slice 4).

Timeout only -- kick and permanent ban are never reachable through this
class, matching the roadmap's Phase 5 boundary ("Kick and permanent ban
remain unavailable to autonomous execution in this phase regardless of
configuration"). Every ordinary failure mode (guild/member not found,
missing permission, Discord API error) produces an honest
`ActionReceipt(succeeded=False, ...)` rather than raising -- matching
`krubit.web.moderation_adapter.NoOpActionExecutor`'s contract from slice 3.
"""

from __future__ import annotations

import uuid

import discord

from krubit.domain.moderation import ActionReceipt, ModerationCase

_MAX_DETAIL_LENGTH = 500


class DiscordActionExecutor:
    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        if case.action_expiration is None:
            raise ValueError(
                f"case {case.case_id} has no action_expiration set -- "
                "record_human_decision must run before execute_approved_action"
            )
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
                detail=f"{type(exc).__name__}: {exc}"[:_MAX_DETAIL_LENGTH],
            )
        return ActionReceipt(
            receipt_id=f"receipt:{uuid.uuid4().hex}",
            succeeded=True,
            detail=f"timed out until {case.action_expiration.isoformat()}",
        )


class LazyDiscordActionExecutor:
    """Bindable wrapper solving `__main__.py`'s boot-order problem: the HTTP
    callback server (which needs an `ActionExecutor` to construct its
    `ModerationService`) is built before `KrubitBot` exists. Construct one
    instance, pass it wherever an `ActionExecutor` is needed, then call
    `bind(bot)` once `KrubitBot` is constructed.
    """

    def __init__(self) -> None:
        self._bot: discord.Client | None = None

    def bind(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        if self._bot is None:
            raise RuntimeError(
                "LazyDiscordActionExecutor.execute called before bind() -- "
                "the bot must be constructed and bound first"
            )
        return await DiscordActionExecutor(self._bot).execute(case, execution_key)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_executor.py -v`
Expected: PASS, all 7 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/discord/moderation_executor.py tests/test_moderation_executor.py
git commit -m "feat: add real Discord timeout ActionExecutor and lazy-bind wrapper"
```

---

### Task 3: `Settings.moderation_channel_id`

**Files:**
- Modify: `src/krubit/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `_parse_bool` is not needed here (this is an int
  field, not bool) — follow the `staff_channel_id`/`watchdog_zariya_bridge_url`
  int-field parsing pattern instead (digit-string validation).
- Produces: `Settings.moderation_channel_id: int | None` (default `None`,
  not secret — channel IDs are not sensitive, matching `staff_channel_id`'s
  own non-`repr=False` treatment).

- [ ] **Step 1: Write the failing tests**

First read how `staff_channel_id` is parsed and tested in
`tests/test_config.py` (search for `KRUBIT_STAFF_CHANNEL_ID`) to match its
exact validation style (positive numeric Discord snowflake), then write:

```python
# append to tests/test_config.py
def test_moderation_channel_id_defaults_to_none():
    settings = Settings.from_env(base_env())
    assert settings.moderation_channel_id is None


def test_moderation_channel_id_can_be_set():
    settings = Settings.from_env({**base_env(), "KRUBIT_MODERATION_CHANNEL_ID": "123456789"})
    assert settings.moderation_channel_id == 123456789


def test_moderation_channel_id_rejects_non_numeric():
    with pytest.raises(SettingsError, match="KRUBIT_MODERATION_CHANNEL_ID"):
        Settings.from_env({**base_env(), "KRUBIT_MODERATION_CHANNEL_ID": "not-a-number"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_config.py -k moderation_channel -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute
'moderation_channel_id'`.

- [ ] **Step 3: Implement**

Add to the `Settings` dataclass body (near `staff_channel_id`):

```python
    moderation_channel_id: int | None = None
```

In the `from_env` classmethod, following the exact validation shape used
for `staff_channel_id` (find that block — it validates a positive numeric
Discord snowflake and raises `SettingsError` on failure — and copy its
structure with the new field/env-var names):

```python
        raw_moderation_channel_id = values.get("KRUBIT_MODERATION_CHANNEL_ID", "").strip()
        moderation_channel_id: int | None = None
        if raw_moderation_channel_id:
            if not raw_moderation_channel_id.isdigit() or int(raw_moderation_channel_id) <= 0:
                raise SettingsError(
                    "KRUBIT_MODERATION_CHANNEL_ID must be a positive numeric Discord snowflake"
                )
            moderation_channel_id = int(raw_moderation_channel_id)
```

And in the final `cls(...)` construction call:

```python
            moderation_channel_id=moderation_channel_id,
```

If this file has a `_UNREAD_FIELD_EXEMPTIONS` (or similarly named) AST-scan
safeguard list (a prior slice's Task 1 found and used one — search for it),
add `moderation_channel_id` to it following the same pattern as its other
entries, since nothing in this task's own diff reads the field yet (Task 6
will).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_config.py -v`
Expected: PASS, all tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/config.py tests/test_config.py
git commit -m "feat: add moderation_channel_id setting, independent of staff_channel_id"
```

---

### Task 4: Wire the executor into `web/wiring.py` and `__main__.py`

**Files:**
- Modify: `src/krubit/web/wiring.py`
- Modify: `src/krubit/__main__.py`
- Modify: `tests/test_wiring_oauth.py` (or whichever file Task 5 of the
  slice-3 plan found tests `build_callback_routes`'s gating — reuse that
  same file)

**Interfaces:**
- Consumes: Task 2's `LazyDiscordActionExecutor`;
  `krubit.services.moderation.ActionExecutor` (the `Protocol`, already
  exists).
- Produces: `build_callback_routes(settings, store, vault, oauth_session,
  *, executor: ActionExecutor | None = None) -> tuple[CallbackRoute, ...]`
  — when `executor` is provided, it's used instead of `NoOpActionExecutor`
  for the moderation route's `ModerationService`.

- [ ] **Step 1: Write the failing test**

```python
# append to the test file identified above (matching its existing
# Settings/store/vault construction convention exactly)
async def test_build_callback_routes_uses_supplied_executor_for_moderation_route(...):
    # Construct Settings with moderation_adapter_secret set,
    # callback_public_base_url/callback_port set. Build a trivial fake
    # executor object satisfying the ActionExecutor protocol (an
    # `async def execute(self, case, execution_key)` returning a fixed
    # ActionReceipt). Call build_callback_routes(..., executor=fake_executor)
    # and assert the returned moderation route's underlying ModerationService
    # was constructed with that exact executor instance -- if the route/
    # service internals aren't easily introspectable, instead assert via a
    # real HTTP round-trip through the built route: an execute_approved_action
    # call on an APPROVED case reaches the fake executor and its receipt's
    # `detail` value appears in the HTTP response, proving the supplied
    # executor (not NoOpActionExecutor's fixed "not yet implemented" detail)
    # was actually used.
    ...
```

Write this using the real conventions of whichever test file you found —
match its actual `Settings`/store/vault/`ModerationService`-seeding helpers
rather than inventing new ones.

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL — `build_callback_routes` has no `executor` parameter yet
(`TypeError`), or (if you wrote it as a keyword that's silently accepted by
`**kwargs` — it isn't here, so this shouldn't happen) the moderation route
still uses `NoOpActionExecutor`'s fixed detail text.

- [ ] **Step 3: Implement**

In `src/krubit/web/wiring.py`, change `build_callback_routes`'s signature
and the moderation-route-building block found in the slice-3 plan's Task 5:

```python
def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
    *,
    executor: ActionExecutor | None = None,
) -> tuple[CallbackRoute, ...]:
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []

    if settings.moderation_adapter_secret:
        moderation_service = ModerationService(store, executor or NoOpActionExecutor())
        routes.append(
            build_moderation_adapter_route(
                path="/moderation/adapter",
                header_name="X-Krubit-Moderation-Signature",
                secret=settings.moderation_adapter_secret,
                service=moderation_service,
                test_mode_enabled=settings.moderation_test_mode,
                test_secret=settings.moderation_test_mode_secret,
            )
        )
    # ... rest of the function (creator_signals_enabled gate, TikTok/Meta
    # blocks) unchanged from the slice-3 version.
```

Add `from krubit.services.moderation import ActionExecutor` to the imports
(the `Protocol` type, alongside the existing `ModerationService` import).

In `src/krubit/__main__.py`, immediately before the `callback_routes =
build_callback_routes(...)` line, construct the lazy executor and pass it
in:

```python
        moderation_executor = LazyDiscordActionExecutor()
        callback_routes = build_callback_routes(
            settings, store, vault, oauth_session, executor=moderation_executor
        )
```

Add `from krubit.discord.moderation_executor import LazyDiscordActionExecutor`
to the top-of-file imports. Immediately after the existing `bot = KrubitBot(...)`
construction call (further down in the same function), add:

```python
        moderation_executor.bind(bot)
```

- [ ] **Step 4: Run the test to verify it passes**

Run the same filtered command as Step 2.
Expected: PASS.

- [ ] **Step 5: Run the full wiring test suite to confirm no regressions**

Run: `uv run --offline pytest tests/test_wiring_oauth.py
tests/test_wiring_deauthorization.py tests/test_wiring_security.py -v`
Expected: PASS, all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/krubit/web/wiring.py src/krubit/__main__.py tests/test_wiring_oauth.py
git commit -m "feat: wire the real Discord executor into the moderation HTTP route via lazy-bind"
```

---

### Task 5: `ModerationNotifier` — `#mod-room` posting

**Files:**
- Create: `src/krubit/discord/moderation_notifier.py`
- Create: `tests/test_moderation_notifier.py`

**Interfaces:**
- Consumes: `krubit.domain.moderation.ModerationCase`;
  `krubit.discord.cards.{Card, CardField, render_card}` (the existing card
  helpers used throughout `discord/bot.py` and `watchdog_runtime.py` — check
  their exact import path before writing, matching `_incident_card`'s usage
  in `watchdog_runtime.py`).
- Produces: `ModerationNotifier.__init__(self, channel_id: int | None) ->
  None`, `def bind(self, guild_lookup: Callable[[int], discord.Guild |
  None]) -> None` (mirrors `LazyDiscordActionExecutor.bind` — constructed
  before `KrubitBot` exists, bound after), `async def
  notify_pending_review(self, case: ModerationCase) -> None`, `async def
  notify_outcome(self, case: ModerationCase, succeeded: bool) -> None`.

- [ ] **Step 1: Write the failing tests**

First read `WatchdogRuntime.notify_staff`'s full implementation (already
shown in this plan's research — the gate order is: feature check → channel
resolved → sendable-channel/permission check → `send` → catch and log) and
its surrounding test file (`tests/test_watchdog_runtime.py` — find the
tests exercising `notify_staff` specifically) to match its exact mocking
convention for `discord.Guild`/channel objects.

```python
# tests/test_moderation_notifier.py
"""Tests for #mod-room moderation notifications (slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.discord.moderation_notifier import ModerationNotifier
from krubit.domain.moderation import AppealStatus, ModerationCase, ModerationStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _case(status: ModerationStatus = ModerationStatus.APPROVAL_REQUIRED) -> ModerationCase:
    return ModerationCase(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=NOW,
        offense_number=1,
        recommended_action="24h timeout",
        executed_action=None,
        action_expiration=None,
        status=status,
        review_deadline=None,
        reviewer_id=None,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )


def _sendable_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    permissions = MagicMock(view_channel=True, send_messages=True, embed_links=True)
    channel.permissions_for.return_value = permissions
    return channel


@pytest.mark.asyncio
async def test_notify_pending_review_sends_when_configured() -> None:
    channel = _sendable_channel()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())

    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_pending_review_noop_when_channel_id_unset() -> None:
    guild = MagicMock()
    notifier = ModerationNotifier(channel_id=None)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())

    guild.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pending_review_noop_when_guild_not_found() -> None:
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: None)
    await notifier.notify_pending_review(_case())  # must not raise


@pytest.mark.asyncio
async def test_notify_pending_review_swallows_send_failure() -> None:
    channel = _sendable_channel()
    channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())  # must not raise


@pytest.mark.asyncio
async def test_notify_outcome_sends_for_executed_case() -> None:
    channel = _sendable_channel()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_outcome(_case(status=ModerationStatus.EXECUTED), succeeded=True)

    channel.send.assert_awaited_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'krubit.discord.moderation_notifier'`.

- [ ] **Step 3: Implement**

```python
# src/krubit/discord/moderation_notifier.py
"""#mod-room notifications for the moderation adapter (Phase 5+6 slice 4).

Mirrors `krubit.discord.watchdog_runtime.WatchdogRuntime.notify_staff`'s
exact gate order and failure handling: a message-send failure never raises
and never invalidates the already-committed ledger entry it follows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

import discord

from krubit.discord.cards import Card, CardField, render_card
from krubit.domain.moderation import ModerationCase

_logger = logging.getLogger(__name__)


def _is_sendable_channel(channel: object) -> bool:
    return isinstance(channel, discord.TextChannel | discord.Thread)


class ModerationNotifier:
    """Constructed with only `channel_id` -- `bind(guild_lookup)` supplies
    the guild-resolution callable later, mirroring
    `LazyDiscordActionExecutor`'s pattern, since `__main__.py` must
    construct this before `KrubitBot` (and therefore any real guild lookup)
    exists.
    """

    def __init__(self, channel_id: int | None) -> None:
        self._channel_id = channel_id
        self._guild_lookup: Callable[[int], discord.Guild | None] | None = None

    def bind(self, guild_lookup: Callable[[int], discord.Guild | None]) -> None:
        self._guild_lookup = guild_lookup

    async def notify_pending_review(self, case: ModerationCase) -> None:
        await self._send(case.guild_id, _pending_review_card(case))

    async def notify_outcome(self, case: ModerationCase, succeeded: bool) -> None:
        await self._send(case.guild_id, _outcome_card(case, succeeded))

    async def _send(self, guild_id: int, card: Card) -> None:
        if self._channel_id is None or self._guild_lookup is None:
            return
        guild = self._guild_lookup(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(self._channel_id)
        if not _is_sendable_channel(channel):
            return
        sendable = cast(discord.TextChannel, channel)
        permissions = sendable.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
            )
        ):
            return
        try:
            await sendable.send(embed=render_card(card))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            _logger.exception("ModerationNotifier: failed to deliver notification")


def _pending_review_card(case: ModerationCase) -> Card:
    return Card(
        kind="guarded",
        title="🦴 Moderation: Pending Review",
        description=f"Case `{case.case_id}` needs staff review.",
        fields=(
            CardField("Case", case.case_id, inline=True),
            CardField("Member", str(case.member_id), inline=True),
            CardField("Recommended action", case.recommended_action or "—", inline=False),
        ),
    )


def _outcome_card(case: ModerationCase, succeeded: bool) -> Card:
    return Card(
        kind="guarded" if succeeded else "failed",
        title="🦴 Moderation: Action Outcome",
        description=f"Case `{case.case_id}` execution {'succeeded' if succeeded else 'failed'}.",
        fields=(
            CardField("Case", case.case_id, inline=True),
            CardField("Member", str(case.member_id), inline=True),
            CardField("Status", case.status.value, inline=True),
        ),
    )
```

Before writing this, check `Card`'s actual field names/required `kind`
values (`"fetched"`, `"detected"`, `"guarded"`, `"failed"`, `"healthy"` per
the design doc's Phase 0 card primitives, referenced earlier in this
project) against `src/krubit/discord/cards.py` directly — use whatever the
real enum/literal values are, adjusting `kind="guarded"`/`kind="failed"`
above if they differ from what's shown here.

Add a test proving the unbound case is a clean no-op, matching
`LazyDiscordActionExecutor`'s equivalent precedent (Task 2):

```python
@pytest.mark.asyncio
async def test_notify_pending_review_noop_before_bind() -> None:
    notifier = ModerationNotifier(channel_id=555)
    await notifier.notify_pending_review(_case())  # must not raise, no bind() called
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_notifier.py -v`
Expected: PASS, all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/discord/moderation_notifier.py tests/test_moderation_notifier.py
git commit -m "feat: add ModerationNotifier for #mod-room pending-review and outcome posts"
```

---

### Task 6: `ModerationCommands` — the `/fetch moderation` group

**Files:**
- Modify: `src/krubit/discord/bot.py`
- Create: `tests/test_moderation_commands.py`

**Interfaces:**
- Consumes: Task 1's `record_human_decision(..., timeout_duration=...)`;
  Task 2's `LazyDiscordActionExecutor`; Task 5's `ModerationNotifier`;
  `krubit.services.moderation.ModerationService`;
  `krubit.contracts.moderation.{GetPendingReviewsQuery,
  GetMemberModerationHistoryQuery, GetIncidentStatusQuery,
  GetActionReceiptQuery, ExecuteApprovedActionRequest}`;
  `FetchCommands.authorize`/`FetchCommands.finish` (existing, `manage_guild`
  permission).
- Produces: `ModerationCommands(app_commands.Group)` with 5 commands
  (`pending`, `approve`, `reject`, `history`, `case`), added via
  `self.add_command(ModerationCommands(self))` inside `FetchCommands.__init__`.
  `KrubitBot.__init__` gains a `moderation_executor: ActionExecutor`
  parameter (required, no default — every production caller must supply
  one), threaded to `FetchCommands.__init__`, which constructs
  `self._moderation_service = ModerationService(service.store,
  moderation_executor)` and `self._moderation_notifier =
  ModerationNotifier(lambda gid: self._bot_guild_lookup(gid),
  settings.moderation_channel_id)`.

- [ ] **Step 1: Investigate the exact `FetchCommands`/`KrubitBot` constructor signatures**

Read `src/krubit/discord/bot.py`'s `FetchCommands.__init__` and
`KrubitBot.__init__` signatures in full (both are large — read the whole
parameter list, not just the body) before writing this task's code. You
need: (a) how `FetchCommands` currently gets a way to look up a live
`discord.Guild` by ID (check if it already stores a `bot`/`client`
reference, or if commands resolve guilds only via `interaction.guild`
inside a handler — if there's no stored client reference, `ModerationNotifier`
needs one threaded in as a new constructor parameter too, matching how
`moderation_executor` is threaded in); (b) the exact existing
`self.add_command(...)` block's location to add
`self.add_command(ModerationCommands(self))` right after
`self.add_command(SniffCommands(self))`; (c) how existing test files
construct a `FetchCommands`/`KrubitBot` instance for testing (search
`tests/test_watchdog_commands.py` or similar for the pattern) so this
task's own tests match it.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_moderation_commands.py
"""Tests for the /fetch moderation command group (slice 4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.contracts.moderation import RecordIncidentRequest
from krubit.domain.moderation import ActionReceipt
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.moderation import ModerationService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeExecutor:
    async def execute(self, case: object, execution_key: str) -> ActionReceipt:
        return ActionReceipt(receipt_id="receipt:fake", succeeded=True, detail="ok")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    await value.record_incident(
        Incident(
            guild_id=100,
            incident_id="incident:1",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=NOW,
            evidence_packet_id="incident:1",
            recommended_action="review",
            acknowledged_by=None,
        )
    )
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_moderation_service_pending_and_approve_flow(store: SQLiteStore) -> None:
    """This test exercises the ModerationService + record_human_decision +
    execute_approved_action call sequence a /fetch moderation approve
    command handler must perform -- a full Discord-interaction-level test
    additionally requires mocking `discord.Interaction`, which depends on
    Step 1's investigation of this file's existing interaction-mocking
    convention (see `tests/test_watchdog_commands.py`). Write that
    additional interaction-level test using whatever mock/fixture shape
    that file already establishes, following this same call sequence.
    """
    from datetime import timedelta

    from krubit.contracts.moderation import (
        RequestHumanApprovalRequest,
        SubmitActionRecommendationRequest,
    )
    from krubit.domain.moderation import ApprovalDecision

    service = ModerationService(store, FakeExecutor())
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    from krubit.contracts.moderation import GetPendingReviewsQuery

    pending = await service.get_pending_reviews(
        100, GetPendingReviewsQuery.from_dict({"guild_id": "100"})
    )
    assert record_response.case_id in pending.cases

    await service.record_human_decision(
        100,
        record_response.case_id,
        reviewer_id=999,
        decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    from krubit.contracts.moderation import ExecuteApprovedActionRequest

    execute_response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict(
            {"case_id": record_response.case_id, "idempotency_key": "idem:4"}
        ),
    )
    assert execute_response.status.value == "executed"
```

Then, following Step 1's investigation of the real `discord.Interaction`
mocking convention already used in this codebase's Discord-command tests,
add at least one interaction-level test per command (`pending`, `approve`,
`reject`, `history`, `case`) — five more tests — using the actual
`FetchCommands`/bot construction pattern you found, asserting each command
produces the correct ephemeral response and correctly calls through to
`ModerationService`. Also add a test proving `ModerationCommands` is
gated by `manage_guild` the same way `AdminCommands` is (a non-staff caller
gets rejected), matching whatever assertion shape the existing
`AdminCommands` authorization tests use.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_commands.py -v`
Expected: The `ModerationService`-only test may pass immediately (it
doesn't touch new command code); the interaction-level tests FAIL with
`ImportError`/`AttributeError` since `ModerationCommands` doesn't exist yet.

- [ ] **Step 4: Implement `ModerationCommands`**

Based on Step 1's investigation, add a new class to `src/krubit/discord/bot.py`
(placed after `SniffCommands`, following its exact structural pattern —
`authorize`/`finish` calls, ephemeral responses, `app_commands.command`
decorators with `@app_commands.guild_only()` and
`@app_commands.default_permissions(manage_guild=True)`):

```python
class ModerationCommands(app_commands.Group):
    """`/fetch moderation` -- staff approval and review commands for the
    Zariya-Krubit moderation adapter (Phase 5+6 slice 4). A sibling of
    `AdminCommands`/`SniffCommands`, not nested inside either -- AdminCommands
    is already at 17/25 Discord subcommand slots.
    """

    def __init__(self, parent: FetchCommands) -> None:
        super().__init__(name="moderation", description="Moderation approval and review commands")
        self._parent = parent

    @app_commands.command(name="pending", description="List cases awaiting review")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def pending(self, interaction: discord.Interaction) -> None:
        context = await self._parent.authorize(interaction, "fetch_moderation_pending")
        if context is None:
            return
        guild, actor_id = context
        result = await self._parent._moderation_service.get_pending_reviews(
            guild.id, GetPendingReviewsQuery.from_dict({"guild_id": str(guild.id)})
        )
        card = Card(
            kind="fetched",
            title="🦴 Fetched: Pending Moderation Reviews",
            description=f"{len(result.cases)} case(s) awaiting review.",
            fields=tuple(CardField(f"Case {i + 1}", case_id, inline=False) for i, case_id in enumerate(result.cases)),
        )
        await self._parent.finish(
            interaction, action="fetch_moderation_pending", actor_id=actor_id,
            embed=render_card(card), detail={"count": len(result.cases)},
        )

    @app_commands.command(name="approve", description="Approve a moderation case with a timeout")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def approve(
        self, interaction: discord.Interaction, case_id: str, duration_hours: app_commands.Range[int, 1, 672]
    ) -> None:
        context = await self._parent.authorize(interaction, "fetch_moderation_approve")
        if context is None:
            return
        guild, actor_id = context
        from datetime import timedelta

        try:
            case = await self._parent._moderation_service.record_human_decision(
                guild.id, case_id, reviewer_id=actor_id, decision=ApprovalDecision.APPROVED,
                timeout_duration=timedelta(hours=duration_hours),
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        execute_response = await self._parent._moderation_service.execute_approved_action(
            guild.id,
            ExecuteApprovedActionRequest.from_dict(
                {"case_id": case_id, "idempotency_key": f"admin-approve:{case_id}:{uuid4().hex}"}
            ),
        )
        await self._parent._moderation_notifier.notify_outcome(
            case, succeeded=execute_response.status.value == "executed"
        )
        card = Card(
            kind="fetched" if execute_response.status.value == "executed" else "failed",
            title="🦴 Moderation Case Approved",
            description=f"Case `{case_id}` executed with status `{execute_response.status.value}`.",
            fields=(CardField("Duration", f"{duration_hours}h", inline=True),),
        )
        await self._parent.finish(
            interaction, action="fetch_moderation_approve", actor_id=actor_id,
            embed=render_card(card), detail={"case_id": case_id, "status": execute_response.status.value},
        )

    @app_commands.command(name="reject", description="Reject a moderation case")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def reject(self, interaction: discord.Interaction, case_id: str, reason: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_moderation_reject")
        if context is None:
            return
        guild, actor_id = context
        try:
            await self._parent._moderation_service.record_human_decision(
                guild.id, case_id, reviewer_id=actor_id, decision=ApprovalDecision.REJECTED,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        card = Card(
            kind="fetched", title="🦴 Moderation Case Rejected",
            description=f"Case `{case_id}` rejected.",
            fields=(CardField("Reason", reason, inline=False),),
        )
        await self._parent.finish(
            interaction, action="fetch_moderation_reject", actor_id=actor_id,
            embed=render_card(card), detail={"case_id": case_id, "reason": reason},
        )

    @app_commands.command(name="history", description="Fetch a member's moderation history")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def history(self, interaction: discord.Interaction, member: discord.Member) -> None:
        context = await self._parent.authorize(interaction, "fetch_moderation_history")
        if context is None:
            return
        guild, actor_id = context
        result = await self._parent._moderation_service.get_member_moderation_history(
            guild.id,
            GetMemberModerationHistoryQuery.from_dict(
                {"guild_id": str(guild.id), "member_id": str(member.id)}
            ),
        )
        card = Card(
            kind="fetched", title="🦴 Fetched: Member Moderation History",
            description=f"{len(result.cases)} case(s) for {member.mention}.",
            fields=tuple(CardField(f"Case {i + 1}", case_id, inline=False) for i, case_id in enumerate(result.cases)),
        )
        await self._parent.finish(
            interaction, action="fetch_moderation_history", actor_id=actor_id,
            embed=render_card(card), detail={"member_id": member.id, "count": len(result.cases)},
        )

    @app_commands.command(name="case", description="Inspect one moderation case")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def case(self, interaction: discord.Interaction, case_id: str) -> None:
        context = await self._parent.authorize(interaction, "fetch_moderation_case")
        if context is None:
            return
        guild, actor_id = context
        case = await self._parent._moderation_service.get_case(guild.id, case_id)
        if case is None:
            await interaction.edit_original_response(content=f"No case found for `{case_id}`.")
            return
        card = Card(
            kind="fetched", title="🦴 Fetched: Moderation Case",
            description=f"Case `{case.case_id}`.",
            fields=(
                CardField("Status", case.status.value, inline=True),
                CardField("Member", str(case.member_id), inline=True),
                CardField("Recommended action", case.recommended_action or "—", inline=False),
            ),
        )
        await self._parent.finish(
            interaction, action="fetch_moderation_case", actor_id=actor_id,
            embed=render_card(card), detail={"case_id": case.case_id},
        )
```

Task 1's `get_case` addition resolves this command cleanly: `case_id` is
the command's real, only parameter (no `incident_id`/`case_id` naming
mismatch, since `get_case` is genuinely keyed by `case_id`), and the
returned `ModerationCase` carries `member_id`/`recommended_action` that
`get_incident`/`get_incident_status` never exposed.

Add the required imports to the top of `discord/bot.py`: `from uuid import
uuid4` (if not already imported under a different alias — check first, the
file may already import `uuid4` for `_boot_id`), `from krubit.contracts.moderation
import (ExecuteApprovedActionRequest, GetMemberModerationHistoryQuery,
GetPendingReviewsQuery)`, `from krubit.domain.moderation import
ApprovalDecision`, `from krubit.discord.moderation_notifier import
ModerationNotifier`, `from krubit.services.moderation import ActionExecutor,
ModerationService`. (`GetIncidentQuery`/`GetIncidentStatusQuery` are no
longer needed by this file, since `case` now uses `get_case` instead.)

Thread `moderation_executor: ActionExecutor` through `KrubitBot.__init__`
and `FetchCommands.__init__` per Step 1's findings, constructing
`self._moderation_service = ModerationService(service.store,
moderation_executor)` and `self._moderation_notifier =
ModerationNotifier(...)` in `FetchCommands.__init__`, and add
`self.add_command(ModerationCommands(self))` immediately after the existing
`self.add_command(SniffCommands(self))` line.

**Wiring the pending-review notification.** `request_human_approval` is
only ever invoked over HTTP (by Zariya/KAI-Studio callers, per the locked
contract — no Discord command in this slice calls it), so the
`notify_pending_review` trigger point belongs in
`src/krubit/web/moderation_adapter.py`, the single place every
`request_human_approval` call passes through — not inside
`services/moderation.py` (which stays framework-independent) and not
inside `ModerationCommands` (which never calls this operation). Extend
`build_moderation_adapter_route`'s signature with an optional
`notifier: ModerationNotifier | None = None` parameter. Inside `_handle`
(or the calling closure), after a successful dispatch, check
`if operation == "request_human_approval" and notifier is not None:` and,
using the response's `case_id`, call `case = await
service.get_case(guild_id, response_payload_case_id); if case is not
None: await notifier.notify_pending_review(case)` — wrapped so a
notification failure (already internally caught and logged by
`ModerationNotifier._send`) can never affect the HTTP response already
being returned. In `web/wiring.py`'s moderation-route-building block
(Task 4), construct `moderation_notifier = ModerationNotifier(bot.get_guild,
settings.moderation_channel_id)` is not possible at that point (no `bot`
exists yet, same boot-order problem as the executor) — instead thread a
notifier that resolves guilds lazily the same way
`LazyDiscordActionExecutor` does. Add a `bind(bot)` method to
`ModerationNotifier` itself (mirroring `LazyDiscordActionExecutor`'s
pattern): construct it with `guild_lookup=None` initially,
`bind(bot)` sets `self._guild_lookup = bot.get_guild`, and `_send` becomes
a no-op (matching the "channel not configured" no-op path already defined)
when `self._guild_lookup is None`. Construct one `ModerationNotifier`
instance in `__main__.py` alongside `moderation_executor`, pass it to both
`build_callback_routes(..., notifier=moderation_notifier)` and
`KrubitBot(..., moderation_notifier=moderation_notifier)`, and call
`moderation_notifier.bind(bot)` in the same place `moderation_executor.bind(bot)`
is called.

This changes Task 5's `ModerationNotifier.__init__` signature from
`(guild_lookup: Callable[[int], discord.Guild | None], channel_id: int |
None)` to `(channel_id: int | None)` with `guild_lookup` set later via
`bind`, matching `LazyDiscordActionExecutor`'s shape exactly. Go back and
adjust Task 5's implementation and tests to construct
`ModerationNotifier(channel_id=555)` then call `.bind(lambda gid: guild)`
(or `.bind(guild_lookup_fn)`) before invoking `notify_pending_review`/
`notify_outcome`, rather than passing `guild_lookup` at construction time.

Update `__main__.py`'s `KrubitBot(...)` construction call to pass
`moderation_executor=moderation_executor, moderation_notifier=moderation_notifier`
(both from Task 4/this task, `LazyDiscordActionExecutor`/`ModerationNotifier`
instances), and update Task 4's `web/wiring.py` change to also accept and
thread through `notifier: ModerationNotifier | None = None` alongside
`executor`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_commands.py -v`
Expected: PASS, all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/krubit/discord/bot.py src/krubit/__main__.py tests/test_moderation_commands.py
git commit -m "feat: add /fetch moderation command group (pending, approve, reject, history, case)"
```

---

### Task 7: Cross-cutting integration test and full verification

**Files:**
- Modify: `tests/test_moderation_commands.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: no new production code — one test proving the full chain from
  `record_incident` through Discord-command approval to a real (mocked)
  `member.timeout()` call and a `#mod-room` post.

- [ ] **Step 1: Write the full end-to-end test**

```python
# append to tests/test_moderation_commands.py
@pytest.mark.asyncio
async def test_full_chain_approve_calls_discord_timeout_and_notifies(store: SQLiteStore) -> None:
    """record_incident -> submit_action_recommendation -> request_human_approval
    -> record_human_decision (approve) -> execute_approved_action, using the
    REAL DiscordActionExecutor (mocked discord.Client) instead of FakeExecutor,
    proving the full chain reaches member.timeout() and the notifier fires.
    """
    from unittest.mock import AsyncMock, MagicMock

    from krubit.contracts.moderation import RequestHumanApprovalRequest, SubmitActionRecommendationRequest
    from krubit.discord.moderation_executor import DiscordActionExecutor
    from krubit.discord.moderation_notifier import ModerationNotifier
    from krubit.domain.moderation import ApprovalDecision
    from datetime import timedelta

    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = DiscordActionExecutor(bot)
    service = ModerationService(store, executor)

    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:1",
            }
        ),
    )
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    case = await service.record_human_decision(
        100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    execute_response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict(
            {"case_id": record_response.case_id, "idempotency_key": "idem:4"}
        ),
    )
    assert execute_response.status.value == "executed"
    member.timeout.assert_awaited_once()

    notify_guild = MagicMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=True
    )
    notify_guild.get_channel.return_value = channel
    notify_guild.me = MagicMock()
    notifier = ModerationNotifier(guild_lookup=lambda gid: notify_guild, channel_id=555)
    await notifier.notify_outcome(case, succeeded=True)
    channel.send.assert_awaited_once()
```

- [ ] **Step 2: Run the moderation-scoped test suite**

Run: `uv run --offline pytest tests/test_moderation_domain.py
tests/test_moderation_contract.py tests/test_moderation_storage.py
tests/test_moderation_service.py tests/test_moderation_adapter.py
tests/test_moderation_executor.py tests/test_moderation_notifier.py
tests/test_moderation_commands.py tests/test_config.py
tests/test_wiring_oauth.py tests/test_wiring_deauthorization.py
tests/test_wiring_security.py -v`
Expected: PASS, all tests green.

- [ ] **Step 3: Run the full project test suite to confirm no regressions**

Run: `uv run --offline pytest`
Expected: PASS. A known, pre-existing, unrelated failure in
`tests/test_activity_privacy_structural_safety.py` (from slice 2's schema
changes, documented in slices 2/3's builds) may still be present — this is
not this slice's regression; report it honestly if seen, don't treat it as
new. If the test runner appears to hang, retry with a hard timeout wrapper
(`timeout 60 uv run --offline pytest ...`) before concluding there's a real
problem — this has been an intermittent, non-deterministic environment
issue (not a code bug) throughout this project's development.

- [ ] **Step 4: Commit**

```bash
git add tests/test_moderation_commands.py
git commit -m "test: add full-chain end-to-end test through Discord command approval to real timeout execution"
```
