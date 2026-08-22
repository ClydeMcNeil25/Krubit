# Moderation Storage Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist `ModerationCase`, enforce idempotency and legal state
transitions, and implement all 11 Zariya-facing contract operations (plus one
internal admin-reserved operation) as real async methods against SQLite —
slice 2 of 4 of the Zariya-Krubit moderation adapter (Phase 5+6
reconciliation).

**Architecture:** Two new SQLite tables (`moderation_cases`,
`moderation_action_receipts`) plus one idempotency-cache table
(`moderation_idempotency_keys`), row-decoding helpers in
`storage/moderation_rows.py`, CRUD methods added directly to `SQLiteStore` in
`storage/sqlite.py` (matching the existing `incidents` table's precedent),
and a new `services/moderation.py` housing `ModerationService` — one async
method per contract operation, each idempotency-checked, transition-validated
via the existing `domain.moderation.transition()`, and persisted inside one
`_write_transaction()`. A small contract amendment (Task 1) adds `guild_id`
to four read operations that were missing it in slice 1 — a real
multi-tenancy gap, not a style change.

**Tech Stack:** Python 3.12+, `aiosqlite`, `pytest` + `pytest-asyncio`
(already a dependency — see `tests/test_watchdog_storage.py`), stdlib
`dataclasses`/`uuid`/`json`.

## Global Constraints

- All datetimes UTC-aware; reject naive datetimes (existing `_require_aware`
  pattern, reused from `krubit.domain.watchdog`).
- `case_id` is generated as `case:<uuid4 hex>`; `receipt_id` is generated as
  `receipt:<uuid4 hex>` — both globally unique via their own `UNIQUE INDEX`,
  not just guild-scoped, because several locked contract requests carry only
  `case_id`/`receipt_id`, not `guild_id`.
- Idempotency key is scoped `(guild_id, operation, idempotency_key)`. For the
  five case-bound operations whose request has no `guild_id` field
  (`submit_action_recommendation`, `request_human_approval`,
  `execute_approved_action`, `close_incident`, `submit_appeal`), resolve
  `guild_id` by looking up the case via `case_id` FIRST, then do the
  idempotency check — never the reverse.
- Every mutating service method: idempotency check → validate → persist
  (inside one `_write_transaction()`) → cache response → return. Never
  re-execute on a cache hit.
- `ActionExecutor.execute()` is called **outside** any `_write_transaction()`
  — a DB transaction must never be held open across a call that could be a
  network call.
- No new third-party dependencies.
- Reuse `krubit.domain.moderation.transition()` for every status change; do
  not duplicate the legal-transition table.

---

## File Structure

- Modify: `src/krubit/contracts/moderation.py` — add `guild_id` to four read
  operations (Task 1).
- Modify: `tests/test_moderation_contract.py` — updated round-trip tests for
  the four amended classes (Task 1).
- Modify: `src/krubit/storage/sqlite.py` — three new tables in `_initialize`,
  plus CRUD methods (Tasks 2, 4).
- Create: `src/krubit/storage/moderation_rows.py` — row-decoding helpers
  (Task 4).
- Modify: `src/krubit/domain/moderation.py` — `ModerationCase.recommended_action`
  becomes optional, add `ActionReceipt`, add `UnknownIncidentError` (Task 3).
- Create: `src/krubit/services/moderation.py` — `ActionExecutor` protocol,
  `ModerationService` (Tasks 5, 6, 7).
- Create: `tests/test_moderation_storage.py` — schema + CRUD tests (Tasks 2, 4).
- Create: `tests/test_moderation_service.py` — service-layer tests (Tasks 5,
  6, 7, 8).

---

### Task 1: Contract amendment — add `guild_id` to four read operations

**Files:**
- Modify: `src/krubit/contracts/moderation.py:426-540` (the
  `GetMemberModerationHistoryQuery/Result`, `GetIncidentQuery/Result`,
  `GetIncidentStatusQuery/Result`, `GetActionReceiptQuery` classes — leave
  `GetActionReceiptResult` and `GetPendingReviewsQuery/Result` unchanged,
  they already carry enough identity)
- Modify: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: existing `_required_text`, `_required_id`, `_status`,
  `_tuple_of_str`, `ModerationContractError` (all already in the file).
- Produces: `GetMemberModerationHistoryQuery(guild_id: str, member_id: str)`,
  `GetMemberModerationHistoryResult(guild_id: str, member_id: str, cases:
  tuple[str, ...])`, `GetIncidentQuery(guild_id: str, incident_id: str)`,
  `GetIncidentResult(guild_id: str, incident_id: str, case_id: str)`,
  `GetIncidentStatusQuery(guild_id: str, incident_id: str)`,
  `GetIncidentStatusResult(guild_id: str, incident_id: str, status:
  ModerationStatus)`, `GetActionReceiptQuery(guild_id: str, receipt_id: str)`.

- [ ] **Step 1: Write the failing tests**

Replace the four existing tests for these classes in
`tests/test_moderation_contract.py` with:

```python
def test_get_member_moderation_history_round_trips():
    query = GetMemberModerationHistoryQuery.from_dict(
        {"guild_id": "100", "member_id": "200"}
    )
    assert query.to_dict() == {"guild_id": "100", "member_id": "200"}

    result_payload = {"guild_id": "100", "member_id": "200", "cases": ["case:1", "case:2"]}
    result = GetMemberModerationHistoryResult.from_dict(result_payload)
    assert result.cases == ("case:1", "case:2")
    assert result.to_dict() == result_payload


def test_get_incident_round_trips():
    query = GetIncidentQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
    assert query.to_dict() == {"guild_id": "100", "incident_id": "incident:1"}

    result_payload = {"guild_id": "100", "incident_id": "incident:1", "case_id": "case:1"}
    result = GetIncidentResult.from_dict(result_payload)
    assert result.to_dict() == result_payload


def test_get_incident_status_round_trips():
    query = GetIncidentStatusQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
    assert query.to_dict() == {"guild_id": "100", "incident_id": "incident:1"}

    result_payload = {"guild_id": "100", "incident_id": "incident:1", "status": "closed"}
    result = GetIncidentStatusResult.from_dict(result_payload)
    assert result.status.value == "closed"
    assert result.to_dict() == result_payload


def test_get_action_receipt_round_trips():
    query = GetActionReceiptQuery.from_dict({"guild_id": "100", "receipt_id": "receipt:1"})
    assert query.to_dict() == {"guild_id": "100", "receipt_id": "receipt:1"}

    result_payload = {"receipt_id": "receipt:1", "case_id": "case:1", "succeeded": True}
    result = GetActionReceiptResult.from_dict(result_payload)
    assert result.to_dict() == result_payload
```

`GetActionReceiptResult` is unchanged (still no `guild_id` — `receipt_id` is
already globally unique via its own index added in Task 2, so its result
doesn't need it; only the *query* gains `guild_id` for consistency with the
other three).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_contract.py -v`
Expected: FAIL — `to_dict()` output missing `guild_id`, or `from_dict` raises
`ModerationContractError: guild_id is required`.

- [ ] **Step 3: Implement the amendment**

Replace the four query/result class bodies in
`src/krubit/contracts/moderation.py` (lines ~425-507) with:

```python
@dataclass(frozen=True, slots=True)
class GetMemberModerationHistoryQuery:
    guild_id: str
    member_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetMemberModerationHistoryQuery:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            member_id=_required_text(payload, "member_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "member_id": self.member_id}


@dataclass(frozen=True, slots=True)
class GetMemberModerationHistoryResult:
    guild_id: str
    member_id: str
    cases: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetMemberModerationHistoryResult:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            member_id=_required_text(payload, "member_id"),
            cases=_tuple_of_str(payload, "cases"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "member_id": self.member_id, "cases": list(self.cases)}


@dataclass(frozen=True, slots=True)
class GetIncidentQuery:
    guild_id: str
    incident_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetIncidentQuery:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            incident_id=_required_id(payload, "incident_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "incident_id": self.incident_id}


@dataclass(frozen=True, slots=True)
class GetIncidentResult:
    guild_id: str
    incident_id: str
    case_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetIncidentResult:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            incident_id=_required_id(payload, "incident_id"),
            case_id=_required_id(payload, "case_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "incident_id": self.incident_id, "case_id": self.case_id}


@dataclass(frozen=True, slots=True)
class GetIncidentStatusQuery:
    guild_id: str
    incident_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetIncidentStatusQuery:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            incident_id=_required_id(payload, "incident_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "incident_id": self.incident_id}


@dataclass(frozen=True, slots=True)
class GetIncidentStatusResult:
    guild_id: str
    incident_id: str
    status: ModerationStatus

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetIncidentStatusResult:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            incident_id=_required_id(payload, "incident_id"),
            status=_status(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "incident_id": self.incident_id, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class GetActionReceiptQuery:
    guild_id: str
    receipt_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> GetActionReceiptQuery:
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            receipt_id=_required_text(payload, "receipt_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "receipt_id": self.receipt_id}
```

Leave `GetActionReceiptResult` and `GetPendingReviewsQuery`/`Result`
untouched — do not paste them again.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_contract.py -v`
Expected: PASS, all tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/contracts/moderation.py tests/test_moderation_contract.py
git commit -m "feat: add guild_id to moderation read-op contracts (multi-tenancy fix)"
```

---

### Task 2: Schema — three new tables in `storage/sqlite.py`

**Files:**
- Modify: `src/krubit/storage/sqlite.py` (inside `_initialize`'s
  `executescript` call, immediately before the closing `"""` after
  `idx_activity_receipts_guild_created`)
- Test: `tests/test_moderation_storage.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: three tables (`moderation_cases`, `moderation_action_receipts`,
  `moderation_idempotency_keys`) and their indexes, queryable via
  `PRAGMA table_info(...)` in tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_moderation_storage.py
"""Integration tests for the moderation storage service (slice 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from krubit.storage.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def _table_names(store: SQLiteStore) -> set[str]:
    cursor = await store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


@pytest.mark.asyncio
async def test_moderation_tables_exist_after_initialize(store: SQLiteStore) -> None:
    names = await _table_names(store)
    assert "moderation_cases" in names
    assert "moderation_action_receipts" in names
    assert "moderation_idempotency_keys" in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --offline pytest tests/test_moderation_storage.py -v`
Expected: FAIL — the three table names are absent.

- [ ] **Step 3: Add the schema**

In `src/krubit/storage/sqlite.py`, find the existing block ending in:

```python
            CREATE INDEX IF NOT EXISTS idx_activity_receipts_guild_created
                ON activity_receipts (guild_id, created_at DESC);
            """
        )
```

Insert the following immediately before the closing `"""` (after the
`idx_activity_receipts_guild_created` line, still inside the same
triple-quoted script):

```sql

            CREATE TABLE IF NOT EXISTS moderation_cases (
                guild_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                member_id INTEGER NOT NULL,
                report_timestamp TEXT NOT NULL,
                offense_number INTEGER NOT NULL,
                recommended_action TEXT,
                executed_action TEXT,
                action_expiration TEXT,
                status TEXT NOT NULL,
                review_deadline TEXT,
                reviewer_id INTEGER,
                reviewer_decision TEXT,
                appeal_status TEXT NOT NULL,
                close_timestamp TEXT,
                PRIMARY KEY (guild_id, case_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_cases_case_id
                ON moderation_cases (case_id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_cases_guild_incident
                ON moderation_cases (guild_id, incident_id);

            CREATE INDEX IF NOT EXISTS idx_moderation_cases_guild_member
                ON moderation_cases (guild_id, member_id);

            CREATE INDEX IF NOT EXISTS idx_moderation_cases_guild_status
                ON moderation_cases (guild_id, status);

            CREATE TABLE IF NOT EXISTS moderation_action_receipts (
                guild_id INTEGER NOT NULL,
                receipt_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, receipt_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_action_receipts_receipt_id
                ON moderation_action_receipts (receipt_id);

            CREATE INDEX IF NOT EXISTS idx_moderation_action_receipts_case
                ON moderation_action_receipts (guild_id, case_id);

            CREATE TABLE IF NOT EXISTS moderation_idempotency_keys (
                guild_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                case_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, operation, idempotency_key)
            );
```

(The `idx_moderation_action_receipts_receipt_id` unique index is not named
in the design spec's schema block but follows directly from the same
"point 1" reasoning already documented there — `GetActionReceiptQuery` after
Task 1 carries `guild_id` too, but the receipt lookup itself still resolves
`receipt_id` to a row before any guild check, so a global unique index keeps
that lookup unambiguous the same way `case_id`'s does.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --offline pytest tests/test_moderation_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/storage/sqlite.py tests/test_moderation_storage.py
git commit -m "feat: add moderation_cases, moderation_action_receipts, moderation_idempotency_keys tables"
```

---

### Task 3: Domain changes — optional `recommended_action`, `ActionReceipt`, `UnknownIncidentError`

**Files:**
- Modify: `src/krubit/domain/moderation.py`
- Modify: `tests/test_moderation_domain.py`

**Interfaces:**
- Consumes: existing `_require_aware`, `_require_positive_id`, `_require_text`
  (already imported from `krubit.domain.watchdog`).
- Produces: `ModerationCase.recommended_action: str | None` (was `str`);
  `ActionReceipt(receipt_id: str, succeeded: bool, detail: str)` frozen
  dataclass; `UnknownIncidentError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_domain.py
from krubit.domain.moderation import ActionReceipt, UnknownIncidentError


def test_case_without_recommended_action_constructs():
    case = _case(recommended_action=None)
    assert case.recommended_action is None


def test_case_with_recommended_action_still_validates_length():
    with pytest.raises(ValueError, match="recommended_action"):
        _case(recommended_action="x" * 501)


def test_action_receipt_constructs():
    receipt = ActionReceipt(receipt_id="receipt:1", succeeded=True, detail="applied 24h timeout")
    assert receipt.succeeded is True


def test_unknown_incident_error_is_value_error():
    assert issubclass(UnknownIncidentError, ValueError)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_domain.py -v`
Expected: FAIL — `_case(recommended_action=None)` raises `ValueError` today
(the field is currently required non-blank text); `ActionReceipt`/
`UnknownIncidentError` don't exist yet.

- [ ] **Step 3: Implement the changes**

In `src/krubit/domain/moderation.py`, change the `ModerationCase` field and
its validation (line 97 and lines 115):

```python
    recommended_action: str | None
```

```python
        if self.recommended_action is not None:
            _require_text(
                "recommended_action", self.recommended_action, limit=_MAX_ACTION_LENGTH
            )
```

(replacing the old unconditional `_require_text("recommended_action", ...)`
call).

Append to the end of the file:

```python
class UnknownIncidentError(ValueError):
    """Raised when `record_incident` references an `incident_id` that has no
    matching Phase 3 `krubit.domain.watchdog.Incident` row."""


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """The outcome of one `ActionExecutor.execute()` call.

    `succeeded=False` still produces a receipt — a failed execution attempt
    is itself evidence, not an absence of one. Only a crash between the
    executor returning and this receipt being persisted leaves
    `receipt_state` null on the case, per the design doc.
    """

    receipt_id: str
    succeeded: bool
    detail: str

    def __post_init__(self) -> None:
        _require_text("receipt_id", self.receipt_id, limit=_MAX_ID_LENGTH)
        _require_text("detail", self.detail, limit=_MAX_ACTION_LENGTH)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_domain.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/domain/moderation.py tests/test_moderation_domain.py
git commit -m "feat: make ModerationCase.recommended_action optional, add ActionReceipt and UnknownIncidentError"
```

---

### Task 4: Storage row helpers and `SQLiteStore` CRUD methods

**Files:**
- Create: `src/krubit/storage/moderation_rows.py`
- Modify: `src/krubit/storage/sqlite.py` (append methods after the existing
  `list_recent_incidents` method, following that method's file location)
- Modify: `tests/test_moderation_storage.py`

**Interfaces:**
- Consumes: `krubit.domain.moderation.{ModerationCase, ModerationStatus,
  ApprovalDecision, AppealStatus, ActionReceipt}`; existing
  `_require_guild_id` from `storage/sqlite.py`.
- Produces (in `storage/moderation_rows.py`):
  `moderation_case_from_row(row: aiosqlite.Row | None) -> ModerationCase | None`,
  `action_receipt_from_row(row: aiosqlite.Row | None) -> ActionReceipt | None`.
  Produces (on `SQLiteStore`): `save_moderation_case(case: ModerationCase) ->
  ModerationCase`, `get_moderation_case(case_id: str) -> ModerationCase | None`,
  `get_moderation_case_by_incident(guild_id: int, incident_id: str) ->
  ModerationCase | None`, `count_moderation_cases_for_member(guild_id: int,
  member_id: int) -> int`, `save_action_receipt(guild_id: int, receipt:
  ActionReceipt, case_id: str) -> None`, `get_action_receipt_by_id(receipt_id:
  str) -> tuple[ActionReceipt, str] | None` (returns `(receipt, case_id)`),
  `get_moderation_idempotency_response(guild_id: int, operation: str,
  idempotency_key: str) -> str | None`, `save_moderation_idempotency_response(
  guild_id: int, operation: str, idempotency_key: str, case_id: str,
  response_json: str) -> None`, `list_pending_review_cases(guild_id: int) ->
  tuple[ModerationCase, ...]` (cases with `status == "approval_required"`).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_storage.py
from datetime import UTC, datetime

from krubit.domain.moderation import (
    ActionReceipt,
    AppealStatus,
    ModerationCase,
    ModerationStatus,
)


def _case(**overrides: object) -> ModerationCase:
    fields: dict[str, object] = dict(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        offense_number=1,
        recommended_action=None,
        executed_action=None,
        action_expiration=None,
        status=ModerationStatus.RECORDED,
        review_deadline=None,
        reviewer_id=None,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )
    fields.update(overrides)
    return ModerationCase(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_save_and_get_moderation_case_round_trips(store: SQLiteStore) -> None:
    saved = await store.save_moderation_case(_case())
    assert saved.case_id == "case:1"
    fetched = await store.get_moderation_case("case:1")
    assert fetched == saved


@pytest.mark.asyncio
async def test_get_moderation_case_by_incident(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case())
    fetched = await store.get_moderation_case_by_incident(100, "incident:1")
    assert fetched is not None
    assert fetched.case_id == "case:1"
    assert await store.get_moderation_case_by_incident(100, "incident:missing") is None


@pytest.mark.asyncio
async def test_count_moderation_cases_for_member_excludes_overturned(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case(case_id="case:1", incident_id="incident:1"))
    await store.save_moderation_case(
        _case(case_id="case:2", incident_id="incident:2", appeal_status=AppealStatus.OVERTURNED)
    )
    assert await store.count_moderation_cases_for_member(100, 200) == 1


@pytest.mark.asyncio
async def test_save_and_get_action_receipt(store: SQLiteStore) -> None:
    await store.save_moderation_case(_case())
    receipt = ActionReceipt(receipt_id="receipt:1", succeeded=True, detail="applied 24h timeout")
    await store.save_action_receipt(100, receipt, "case:1")
    fetched = await store.get_action_receipt_by_id("receipt:1")
    assert fetched is not None
    assert fetched[0] == receipt
    assert fetched[1] == "case:1"
    assert await store.get_action_receipt_by_id("receipt:missing") is None


@pytest.mark.asyncio
async def test_idempotency_cache_round_trips(store: SQLiteStore) -> None:
    assert await store.get_moderation_idempotency_response(100, "record_incident", "idem:1") is None
    await store.save_moderation_idempotency_response(
        100, "record_incident", "idem:1", "case:1", '{"case_id": "case:1"}'
    )
    cached = await store.get_moderation_idempotency_response(100, "record_incident", "idem:1")
    assert cached == '{"case_id": "case:1"}'


@pytest.mark.asyncio
async def test_list_pending_review_cases(store: SQLiteStore) -> None:
    await store.save_moderation_case(
        _case(case_id="case:1", incident_id="incident:1", status=ModerationStatus.RECORDED)
    )
    await store.save_moderation_case(
        _case(
            case_id="case:2",
            incident_id="incident:2",
            status=ModerationStatus.APPROVAL_REQUIRED,
        )
    )
    pending = await store.list_pending_review_cases(100)
    assert [case.case_id for case in pending] == ["case:2"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_storage.py -v`
Expected: FAIL — `AttributeError: 'SQLiteStore' object has no attribute
'save_moderation_case'`.

- [ ] **Step 3: Implement `storage/moderation_rows.py`**

```python
"""Row-decoding helpers for the moderation storage service (Phase 5+6 slice 2).

Mirrors `storage/watchdog_rows.py`'s convention: isolate column-to-value-object
mapping so `SQLiteStore` stays focused on queries and transactions.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from krubit.domain.moderation import (
    ActionReceipt,
    AppealStatus,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def moderation_case_from_row(row: aiosqlite.Row | None) -> ModerationCase | None:
    if row is None:
        return None
    reviewer_id = row["reviewer_id"]
    reviewer_decision = row["reviewer_decision"]
    return ModerationCase(
        case_id=str(row["case_id"]),
        incident_id=str(row["incident_id"]),
        guild_id=int(row["guild_id"]),
        member_id=int(row["member_id"]),
        report_timestamp=datetime.fromisoformat(str(row["report_timestamp"])),
        offense_number=int(row["offense_number"]),
        recommended_action=(
            None if row["recommended_action"] is None else str(row["recommended_action"])
        ),
        executed_action=(
            None if row["executed_action"] is None else str(row["executed_action"])
        ),
        action_expiration=_optional_datetime(row["action_expiration"]),
        status=ModerationStatus(str(row["status"])),
        review_deadline=_optional_datetime(row["review_deadline"]),
        reviewer_id=None if reviewer_id is None else int(reviewer_id),
        reviewer_decision=(
            None if reviewer_decision is None else ApprovalDecision(str(reviewer_decision))
        ),
        appeal_status=AppealStatus(str(row["appeal_status"])),
        close_timestamp=_optional_datetime(row["close_timestamp"]),
    )


def action_receipt_from_row(row: aiosqlite.Row | None) -> ActionReceipt | None:
    if row is None:
        return None
    return ActionReceipt(
        receipt_id=str(row["receipt_id"]),
        succeeded=bool(row["succeeded"]),
        detail=str(row["detail"]),
    )
```

- [ ] **Step 4: Implement the `SQLiteStore` methods**

Append to `src/krubit/storage/sqlite.py`, after the existing
`list_recent_incidents` method. Add this import near the file's other
`krubit.storage.*_rows` imports (find the existing `from
krubit.storage.watchdog_rows import ...` line and add a sibling import
directly below it):

```python
from krubit.storage.moderation_rows import action_receipt_from_row, moderation_case_from_row
```

Also add to the top-level imports:

```python
from krubit.domain.moderation import ActionReceipt, ModerationCase, ModerationStatus
```

Then append these methods to the `SQLiteStore` class body:

```python
    async def save_moderation_case(self, case: ModerationCase) -> ModerationCase:
        """Insert or replace one moderation case. Identity is `(guild_id, case_id)`."""
        _require_guild_id(case.guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO moderation_cases (
                    guild_id, case_id, incident_id, member_id, report_timestamp,
                    offense_number, recommended_action, executed_action, action_expiration,
                    status, review_deadline, reviewer_id, reviewer_decision, appeal_status,
                    close_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, case_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    member_id = excluded.member_id,
                    report_timestamp = excluded.report_timestamp,
                    offense_number = excluded.offense_number,
                    recommended_action = excluded.recommended_action,
                    executed_action = excluded.executed_action,
                    action_expiration = excluded.action_expiration,
                    status = excluded.status,
                    review_deadline = excluded.review_deadline,
                    reviewer_id = excluded.reviewer_id,
                    reviewer_decision = excluded.reviewer_decision,
                    appeal_status = excluded.appeal_status,
                    close_timestamp = excluded.close_timestamp
                """,
                (
                    case.guild_id,
                    case.case_id,
                    case.incident_id,
                    case.member_id,
                    case.report_timestamp.isoformat(),
                    case.offense_number,
                    case.recommended_action,
                    case.executed_action,
                    case.action_expiration.isoformat() if case.action_expiration else None,
                    case.status.value,
                    case.review_deadline.isoformat() if case.review_deadline else None,
                    case.reviewer_id,
                    case.reviewer_decision.value if case.reviewer_decision else None,
                    case.appeal_status.value,
                    case.close_timestamp.isoformat() if case.close_timestamp else None,
                ),
            )
        saved = await self.get_moderation_case(case.case_id)
        if saved is None:
            raise RuntimeError("saved moderation case could not be read")
        return saved

    async def get_moderation_case(self, case_id: str) -> ModerationCase | None:
        cursor = await self._connection.execute(
            """
            SELECT guild_id, case_id, incident_id, member_id, report_timestamp,
                   offense_number, recommended_action, executed_action, action_expiration,
                   status, review_deadline, reviewer_id, reviewer_decision, appeal_status,
                   close_timestamp
            FROM moderation_cases
            WHERE case_id = ?
            """,
            (case_id,),
        )
        row = await cursor.fetchone()
        return moderation_case_from_row(row)

    async def get_moderation_case_by_incident(
        self, guild_id: int, incident_id: str
    ) -> ModerationCase | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, case_id, incident_id, member_id, report_timestamp,
                   offense_number, recommended_action, executed_action, action_expiration,
                   status, review_deadline, reviewer_id, reviewer_decision, appeal_status,
                   close_timestamp
            FROM moderation_cases
            WHERE guild_id = ? AND incident_id = ?
            """,
            (guild_id, incident_id),
        )
        row = await cursor.fetchone()
        return moderation_case_from_row(row)

    async def count_moderation_cases_for_member(self, guild_id: int, member_id: int) -> int:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT COUNT(*) AS total FROM moderation_cases
            WHERE guild_id = ? AND member_id = ? AND appeal_status != 'overturned'
            """,
            (guild_id, member_id),
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row is not None else 0

    async def list_pending_review_cases(self, guild_id: int) -> tuple[ModerationCase, ...]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, case_id, incident_id, member_id, report_timestamp,
                   offense_number, recommended_action, executed_action, action_expiration,
                   status, review_deadline, reviewer_id, reviewer_decision, appeal_status,
                   close_timestamp
            FROM moderation_cases
            WHERE guild_id = ? AND status = ?
            ORDER BY report_timestamp DESC, case_id DESC
            """,
            (guild_id, ModerationStatus.APPROVAL_REQUIRED.value),
        )
        rows = await cursor.fetchall()
        return tuple(cast(ModerationCase, moderation_case_from_row(row)) for row in rows)

    async def save_action_receipt(
        self, guild_id: int, receipt: ActionReceipt, case_id: str
    ) -> None:
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO moderation_action_receipts (
                    guild_id, receipt_id, case_id, succeeded, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    receipt.receipt_id,
                    case_id,
                    1 if receipt.succeeded else 0,
                    receipt.detail,
                    datetime.now(UTC).isoformat(),
                ),
            )

    async def get_action_receipt_by_id(self, receipt_id: str) -> tuple[ActionReceipt, str] | None:
        cursor = await self._connection.execute(
            "SELECT receipt_id, case_id, succeeded, detail FROM moderation_action_receipts "
            "WHERE receipt_id = ?",
            (receipt_id,),
        )
        row = await cursor.fetchone()
        receipt = action_receipt_from_row(row)
        if receipt is None or row is None:
            return None
        return receipt, str(row["case_id"])

    async def get_moderation_idempotency_response(
        self, guild_id: int, operation: str, idempotency_key: str
    ) -> str | None:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            "SELECT response_json FROM moderation_idempotency_keys "
            "WHERE guild_id = ? AND operation = ? AND idempotency_key = ?",
            (guild_id, operation, idempotency_key),
        )
        row = await cursor.fetchone()
        return None if row is None else str(row["response_json"])

    async def save_moderation_idempotency_response(
        self,
        guild_id: int,
        operation: str,
        idempotency_key: str,
        case_id: str,
        response_json: str,
    ) -> None:
        _require_guild_id(guild_id)
        async with self._write_transaction():
            await self._connection.execute(
                """
                INSERT INTO moderation_idempotency_keys (
                    guild_id, operation, idempotency_key, case_id, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, operation, idempotency_key, case_id, response_json, datetime.now(UTC).isoformat()),
            )
```

Note: `datetime` and `UTC` and `cast` are already imported at the top of
`sqlite.py` (used throughout the existing file) — do not re-import them.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_storage.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/krubit/storage/moderation_rows.py src/krubit/storage/sqlite.py tests/test_moderation_storage.py
git commit -m "feat: add moderation case/receipt/idempotency CRUD to SQLiteStore"
```

---

### Task 5: `ModerationService` write path A — record, recommend, request approval

**Files:**
- Create: `src/krubit/services/moderation.py`
- Create: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: `SQLiteStore` methods from Task 4; `krubit.domain.moderation.{
  ModerationCase, ModerationStatus, transition, IllegalTransitionError,
  UnknownIncidentError}`; `krubit.contracts.moderation.{RecordIncidentRequest,
  RecordIncidentResponse, SubmitActionRecommendationRequest,
  SubmitActionRecommendationResponse, RequestHumanApprovalRequest,
  RequestHumanApprovalResponse}`.
- Produces: `ActionExecutor` protocol; `ModerationService.__init__(self,
  store: SQLiteStore, executor: ActionExecutor)`;
  `async def record_incident(self, guild_id: int, request:
  RecordIncidentRequest) -> RecordIncidentResponse`; `async def
  submit_action_recommendation(self, guild_id: int, request:
  SubmitActionRecommendationRequest) -> SubmitActionRecommendationResponse`;
  `async def request_human_approval(self, guild_id: int, request:
  RequestHumanApprovalRequest) -> RequestHumanApprovalResponse`.

`guild_id` is passed explicitly as the first argument to every service
method rather than trusted from the request body, because five of the six
mutating contract requests don't carry `guild_id` at all (Task 1's amendment
only touched read operations) — the caller (the future transport layer,
slice 3) is responsible for resolving which guild a request belongs to
(e.g. from an authenticated channel/webhook context) and passing it in.
`record_incident`'s own request DOES carry `guild_id`; this method still
takes the explicit parameter for a uniform signature across all methods, and
raises `ModerationContractError`-adjacent `ValueError` if the two disagree.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moderation_service.py
"""Service-layer tests for the moderation storage service (slice 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.contracts.moderation import (
    RecordIncidentRequest,
    RequestHumanApprovalRequest,
    SubmitActionRecommendationRequest,
)
from krubit.domain.moderation import ActionReceipt, ModerationStatus, UnknownIncidentError
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.moderation import ActionExecutor, ModerationService
from krubit.storage.sqlite import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


class FakeExecutor:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[tuple[str, str]] = []

    async def execute(self, case: object, execution_key: str) -> ActionReceipt:
        self.calls.append((case.case_id, execution_key))  # type: ignore[attr-defined]
        if not self.succeed:
            raise RuntimeError("simulated executor failure")
        return ActionReceipt(receipt_id="receipt:fake", succeeded=True, detail="ok")


async def _seed_incident(store: SQLiteStore, *, guild_id: int = 100, incident_id: str = "incident:1") -> None:
    # Phase 3 never persists a full EvidencePacket to storage (a known,
    # documented build gap) — `Incident.evidence_packet_id` is just a text
    # reference, not a foreign key to real packet storage. So seeding an
    # incident for these tests only needs `store.record_incident`, not an
    # `EvidencePacket`.
    await store.record_incident(
        Incident(
            guild_id=guild_id,
            incident_id=incident_id,
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=NOW,
            evidence_packet_id=incident_id,
            recommended_action="review",
            acknowledged_by=None,
        )
    )


@pytest.mark.asyncio
async def test_record_incident_creates_case(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    request = RecordIncidentRequest.from_dict(
        {
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        }
    )
    response = await service.record_incident(100, request)
    assert response.status is ModerationStatus.RECORDED
    assert response.duplicate is False
    case = await store.get_moderation_case(response.case_id)
    assert case is not None
    assert case.recommended_action is None
    assert case.offense_number == 1


@pytest.mark.asyncio
async def test_record_incident_unknown_incident_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    request = RecordIncidentRequest.from_dict(
        {
            "incident_id": "incident:missing",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        }
    )
    with pytest.raises(UnknownIncidentError):
        await service.record_incident(100, request)


@pytest.mark.asyncio
async def test_record_incident_duplicate_idempotency_key_returns_cached(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    request = RecordIncidentRequest.from_dict(
        {
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        }
    )
    first = await service.record_incident(100, request)
    second = await service.record_incident(100, request)
    assert second.case_id == first.case_id
    assert second.duplicate is True


@pytest.mark.asyncio
async def test_submit_action_recommendation_sets_field(store: SQLiteStore) -> None:
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
    response = await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": "idem:2",
            }
        ),
    )
    assert response.status is ModerationStatus.RECORDED
    case = await store.get_moderation_case(record_response.case_id)
    assert case is not None
    assert case.recommended_action == "24h timeout"


@pytest.mark.asyncio
async def test_request_human_approval_transitions_case(store: SQLiteStore) -> None:
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
    await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {"case_id": record_response.case_id, "recommended_action": "24h timeout", "idempotency_key": "idem:2"}
        ),
    )
    response = await service.request_human_approval(
        100,
        RequestHumanApprovalRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "review_deadline": "2026-01-02T00:00:00+00:00",
                "idempotency_key": "idem:3",
            }
        ),
    )
    assert response.status is ModerationStatus.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_request_human_approval_without_recommendation_raises(store: SQLiteStore) -> None:
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
    with pytest.raises(ValueError, match="recommended_action"):
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
```

`_seed_incident` calls `store.save_evidence_packet` and `store.record_incident`
— both already exist on `SQLiteStore` from Phase 3 (verify their exact
signatures against `src/krubit/storage/sqlite.py` before running this test;
they are unchanged by this plan).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'krubit.services.moderation'`.

- [ ] **Step 3: Implement `services/moderation.py`**

```python
"""Moderation case lifecycle service: implements the 11 Zariya-facing
contract operations (plus one internal admin-reserved operation) against
real SQLite storage. No Discord or HTTP knowledge — matches
`services/incident_evidence.py`'s framework-independent convention.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Protocol

from krubit.contracts.moderation import (
    RecordIncidentRequest,
    RecordIncidentResponse,
    RequestHumanApprovalRequest,
    RequestHumanApprovalResponse,
    SubmitActionRecommendationRequest,
    SubmitActionRecommendationResponse,
)
from krubit.domain.moderation import (
    ActionReceipt,
    ModerationCase,
    ModerationStatus,
    UnknownIncidentError,
    transition,
)
from krubit.storage.sqlite import SQLiteStore

_RECORD_INCIDENT = "record_incident"
_SUBMIT_ACTION_RECOMMENDATION = "submit_action_recommendation"
_REQUEST_HUMAN_APPROVAL = "request_human_approval"


class ActionExecutor(Protocol):
    """Caller-supplied Discord-mutation performer, injected into
    `ModerationService`. A real implementation lands in slice 4; this slice
    tests against a fake. Mirrors `SignedWebhook`'s injected-protocol shape
    in `krubit.web.callbacks`.
    """

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt: ...


def _new_case_id() -> str:
    return f"case:{uuid.uuid4().hex}"


class ModerationService:
    def __init__(self, store: SQLiteStore, executor: ActionExecutor) -> None:
        self._store = store
        self._executor = executor

    async def record_incident(
        self, guild_id: int, request: RecordIncidentRequest
    ) -> RecordIncidentResponse:
        if request.guild_id != guild_id:
            raise ValueError("request.guild_id does not match the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            guild_id, _RECORD_INCIDENT, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return RecordIncidentResponse.from_dict(payload | {"duplicate": True})

        existing_incident = await self._store.get_incident(guild_id, request.incident_id)
        if existing_incident is None:
            raise UnknownIncidentError(f"unknown incident_id {request.incident_id!r}")

        existing_case = await self._store.get_moderation_case_by_incident(
            guild_id, request.incident_id
        )
        if existing_case is not None:
            response = RecordIncidentResponse(
                case_id=existing_case.case_id,
                status=existing_case.status,
                duplicate=True,
                receipt_state=None,
            )
            await self._cache_response(
                guild_id, _RECORD_INCIDENT, request.idempotency_key, response.case_id, response
            )
            return response

        offense_number = 1 + await self._store.count_moderation_cases_for_member(
            guild_id, request.member_id
        )
        case = ModerationCase(
            case_id=_new_case_id(),
            incident_id=request.incident_id,
            guild_id=guild_id,
            member_id=request.member_id,
            report_timestamp=request.report_timestamp,
            offense_number=offense_number,
            recommended_action=None,
            executed_action=None,
            action_expiration=None,
            status=ModerationStatus.RECORDED,
            review_deadline=None,
            reviewer_id=None,
            reviewer_decision=None,
            appeal_status=AppealStatus.NONE,
            close_timestamp=None,
        )
        await self._store.save_moderation_case(case)
        response = RecordIncidentResponse(
            case_id=case.case_id, status=case.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            guild_id, _RECORD_INCIDENT, request.idempotency_key, response.case_id, response
        )
        return response

    async def submit_action_recommendation(
        self, guild_id: int, request: SubmitActionRecommendationRequest
    ) -> SubmitActionRecommendationResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _SUBMIT_ACTION_RECOMMENDATION, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return SubmitActionRecommendationResponse.from_dict(payload | {"duplicate": True})

        if case.status is not ModerationStatus.RECORDED:
            raise ValueError(
                f"submit_action_recommendation requires status RECORDED, case is {case.status}"
            )
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=request.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=case.status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = SubmitActionRecommendationResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _SUBMIT_ACTION_RECOMMENDATION, request.idempotency_key, response.case_id, response
        )
        return response

    async def request_human_approval(
        self, guild_id: int, request: RequestHumanApprovalRequest
    ) -> RequestHumanApprovalResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _REQUEST_HUMAN_APPROVAL, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return RequestHumanApprovalResponse.from_dict(payload | {"duplicate": True})

        if case.recommended_action is None:
            raise ValueError("recommended_action must be set before requesting approval")
        new_status = transition(case.status, ModerationStatus.APPROVAL_REQUIRED)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=request.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = RequestHumanApprovalResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _REQUEST_HUMAN_APPROVAL, request.idempotency_key, response.case_id, response
        )
        return response

    async def _cache_response(
        self, guild_id: int, operation: str, idempotency_key: str, case_id: str, response: object
    ) -> None:
        await self._store.save_moderation_idempotency_response(
            guild_id, operation, idempotency_key, case_id, json.dumps(response.to_dict())  # type: ignore[attr-defined]
        )
```

Add `from krubit.domain.moderation import AppealStatus` to the imports (it's
used in `record_incident`'s `ModerationCase(...)` construction).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/krubit/services/moderation.py tests/test_moderation_service.py
git commit -m "feat: implement record_incident, submit_action_recommendation, request_human_approval service methods"
```

---

### Task 6: `ModerationService` write path B — decision, execute, close, appeal

**Files:**
- Modify: `src/krubit/services/moderation.py`
- Modify: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: everything from Task 5, plus
  `krubit.domain.moderation.ApprovalDecision`;
  `krubit.contracts.moderation.{ExecuteApprovedActionRequest,
  ExecuteApprovedActionResponse, CloseIncidentRequest, CloseIncidentResponse,
  SubmitAppealRequest, SubmitAppealResponse}`;
  `krubit.domain.moderation.AppealStatus`.
- Produces: `async def record_human_decision(self, case_id: str, *,
  reviewer_id: int, decision: ApprovalDecision) -> ModerationCase` (internal,
  no idempotency key — admin-command-driven, slice 4); `async def
  execute_approved_action(self, guild_id: int, request:
  ExecuteApprovedActionRequest) -> ExecuteApprovedActionResponse`; `async def
  close_incident(self, guild_id: int, request: CloseIncidentRequest) ->
  CloseIncidentResponse`; `async def submit_appeal(self, guild_id: int,
  request: SubmitAppealRequest) -> SubmitAppealResponse`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_service.py
from krubit.contracts.moderation import (
    CloseIncidentRequest,
    ExecuteApprovedActionRequest,
    SubmitAppealRequest,
)
from krubit.domain.moderation import ApprovalDecision


async def _approved_case(store: SQLiteStore, service: ModerationService) -> str:
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
    await service.record_human_decision(
        record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED
    )
    return record_response.case_id


@pytest.mark.asyncio
async def test_record_human_decision_approves(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    case_id = await _approved_case(store, service)
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.APPROVED
    assert case.reviewer_id == 999


@pytest.mark.asyncio
async def test_execute_approved_action_success(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=True)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    response = await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    assert response.status is ModerationStatus.EXECUTED
    assert response.receipt_state == "receipt:fake"
    assert executor.calls == [(case_id, "idem:4")]


@pytest.mark.asyncio
async def test_execute_approved_action_failure(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=False)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    response = await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    assert response.status is ModerationStatus.EXECUTION_FAILED
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_close_incident_from_executed(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    response = await service.close_incident(
        100,
        CloseIncidentRequest.from_dict(
            {"case_id": case_id, "decision": "resolved", "idempotency_key": "idem:5"}
        ),
    )
    assert response.status is ModerationStatus.CLOSED


@pytest.mark.asyncio
async def test_close_incident_illegal_from_recorded_raises(store: SQLiteStore) -> None:
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
    from krubit.domain.moderation import IllegalTransitionError

    with pytest.raises(IllegalTransitionError):
        await service.close_incident(
            100,
            CloseIncidentRequest.from_dict(
                {"case_id": record_response.case_id, "decision": "n/a", "idempotency_key": "idem:5"}
            ),
        )


@pytest.mark.asyncio
async def test_submit_appeal_sets_appeal_status(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    response = await service.submit_appeal(
        100,
        SubmitAppealRequest.from_dict(
            {"case_id": case_id, "reason": "member disputes the timeout", "idempotency_key": "idem:6"}
        ),
    )
    assert response.appeal_status.value == "submitted"
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.EXECUTED
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: FAIL — `AttributeError: 'ModerationService' object has no attribute
'record_human_decision'`.

- [ ] **Step 3: Implement the methods**

Append to `ModerationService` in `src/krubit/services/moderation.py` (add
`_EXECUTE_APPROVED_ACTION = "execute_approved_action"`,
`_CLOSE_INCIDENT = "close_incident"`, `_SUBMIT_APPEAL = "submit_appeal"` next
to the module's other operation-name constants, and add
`ExecuteApprovedActionRequest, ExecuteApprovedActionResponse,
CloseIncidentRequest, CloseIncidentResponse, SubmitAppealRequest,
SubmitAppealResponse` to the `contracts.moderation` import, and
`ApprovalDecision, AppealStatus, IllegalTransitionError` to the
`domain.moderation` import):

```python
    async def record_human_decision(
        self, case_id: str, *, reviewer_id: int, decision: ApprovalDecision
    ) -> ModerationCase:
        """Internal — not part of the 11-operation Zariya contract. Reserved
        for slice 4's `/fetch admin` approval commands; added now only so
        this slice's own tests can drive a case through the full lifecycle.
        """
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            raise ValueError(f"unknown case_id {case_id!r}")
        target = (
            ModerationStatus.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ModerationStatus.REJECTED
        )
        new_status = transition(case.status, target)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=reviewer_id,
            reviewer_decision=decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        return await self._store.save_moderation_case(updated)

    async def execute_approved_action(
        self, guild_id: int, request: ExecuteApprovedActionRequest
    ) -> ExecuteApprovedActionResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _EXECUTE_APPROVED_ACTION, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return ExecuteApprovedActionResponse.from_dict(payload | {"duplicate": True})

        if case.status is not ModerationStatus.APPROVED:
            raise ValueError(
                f"execute_approved_action requires status APPROVED, case is {case.status}"
            )

        try:
            receipt = await self._executor.execute(case, request.idempotency_key)
            new_status = ModerationStatus.EXECUTED
            executed_action = case.recommended_action
        except Exception:
            receipt = ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}", succeeded=False, detail="executor raised"
            )
            new_status = ModerationStatus.EXECUTION_FAILED
            executed_action = None

        transition(case.status, new_status)
        await self._store.save_action_receipt(case.guild_id, receipt, case.case_id)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = ExecuteApprovedActionResponse(
            case_id=updated.case_id,
            status=updated.status,
            duplicate=False,
            receipt_state=receipt.receipt_id,
        )
        await self._cache_response(
            case.guild_id, _EXECUTE_APPROVED_ACTION, request.idempotency_key, response.case_id, response
        )
        return response

    async def close_incident(
        self, guild_id: int, request: CloseIncidentRequest
    ) -> CloseIncidentResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _CLOSE_INCIDENT, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return CloseIncidentResponse.from_dict(payload | {"duplicate": True})

        new_status = transition(case.status, ModerationStatus.CLOSED)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=datetime.now(UTC),
        )
        await self._store.save_moderation_case(updated)
        response = CloseIncidentResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _CLOSE_INCIDENT, request.idempotency_key, response.case_id, response
        )
        return response

    async def submit_appeal(
        self, guild_id: int, request: SubmitAppealRequest
    ) -> SubmitAppealResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _SUBMIT_APPEAL, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return SubmitAppealResponse.from_dict(payload | {"duplicate": True})

        if case.appeal_status is not AppealStatus.NONE:
            raise ValueError("an appeal has already been submitted for this case")
        if case.status not in (ModerationStatus.EXECUTED, ModerationStatus.CLOSED):
            raise ValueError(
                f"submit_appeal requires status EXECUTED or CLOSED, case is {case.status}"
            )
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=case.status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=AppealStatus.SUBMITTED,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = SubmitAppealResponse(
            case_id=updated.case_id, appeal_status=updated.appeal_status, duplicate=False
        )
        await self._cache_response(
            case.guild_id, _SUBMIT_APPEAL, request.idempotency_key, response.case_id, response
        )
        return response
```

Note `execute_approved_action`'s `transition(case.status, new_status)` call
on the line after the try/except: this both validates the transition is
legal (raising `IllegalTransitionError` if somehow not, though `APPROVED ->
EXECUTED`/`EXECUTION_FAILED` are always legal per the table) and keeps every
status change routed through the single `transition()` function per the
Global Constraints, even though `new_status` was already decided by the
try/except outcome above it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: PASS (13 tests total)

- [ ] **Step 5: Commit**

```bash
git add src/krubit/services/moderation.py tests/test_moderation_service.py
git commit -m "feat: implement record_human_decision, execute_approved_action, close_incident, submit_appeal"
```

---

### Task 7: `ModerationService` read path — five query operations

**Files:**
- Modify: `src/krubit/services/moderation.py`
- Modify: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: `krubit.contracts.moderation.{
  GetMemberModerationHistoryQuery, GetMemberModerationHistoryResult,
  GetIncidentQuery, GetIncidentResult, GetIncidentStatusQuery,
  GetIncidentStatusResult, GetActionReceiptQuery, GetActionReceiptResult,
  GetPendingReviewsQuery, GetPendingReviewsResult}` (all amended in Task 1
  where applicable); `SQLiteStore.{get_moderation_case_by_incident,
  get_action_receipt_by_id, list_pending_review_cases}` from Task 4. Needs a
  new `SQLiteStore.list_moderation_cases_for_member(guild_id: int, member_id:
  int) -> tuple[ModerationCase, ...]` method, not added in Task 4 — add it in
  this task's Step 3 alongside the service method that needs it.
- Produces: `async def get_member_moderation_history(self, guild_id: int,
  query: GetMemberModerationHistoryQuery) ->
  GetMemberModerationHistoryResult`; `async def get_incident(self, guild_id:
  int, query: GetIncidentQuery) -> GetIncidentResult`; `async def
  get_incident_status(self, guild_id: int, query: GetIncidentStatusQuery) ->
  GetIncidentStatusResult`; `async def get_action_receipt(self, guild_id:
  int, query: GetActionReceiptQuery) -> GetActionReceiptResult`; `async def
  get_pending_reviews(self, guild_id: int, query: GetPendingReviewsQuery) ->
  GetPendingReviewsResult`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_service.py
from krubit.contracts.moderation import (
    GetActionReceiptQuery,
    GetIncidentQuery,
    GetIncidentStatusQuery,
    GetMemberModerationHistoryQuery,
    GetPendingReviewsQuery,
)


@pytest.mark.asyncio
async def test_get_member_moderation_history(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    case_id = await _approved_case(store, service)
    result = await service.get_member_moderation_history(
        100, GetMemberModerationHistoryQuery.from_dict({"guild_id": "100", "member_id": "200"})
    )
    assert result.cases == (case_id,)


@pytest.mark.asyncio
async def test_get_incident_and_status(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    case_id = await _approved_case(store, service)
    incident_result = await service.get_incident(
        100, GetIncidentQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
    )
    assert incident_result.case_id == case_id
    status_result = await service.get_incident_status(
        100, GetIncidentStatusQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
    )
    assert status_result.status is ModerationStatus.APPROVED


@pytest.mark.asyncio
async def test_get_action_receipt(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=True)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    execute_response = await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    result = await service.get_action_receipt(
        100,
        GetActionReceiptQuery.from_dict(
            {"guild_id": "100", "receipt_id": execute_response.receipt_state}
        ),
    )
    assert result.case_id == case_id
    assert result.succeeded is True


@pytest.mark.asyncio
async def test_get_pending_reviews(store: SQLiteStore) -> None:
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
    result = await service.get_pending_reviews(
        100, GetPendingReviewsQuery.from_dict({"guild_id": "100"})
    )
    assert result.cases == (record_response.case_id,)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: FAIL — `AttributeError: 'ModerationService' object has no attribute
'get_member_moderation_history'`.

- [ ] **Step 3: Implement the methods**

Add to `src/krubit/storage/sqlite.py`, after `count_moderation_cases_for_member`:

```python
    async def list_moderation_cases_for_member(
        self, guild_id: int, member_id: int
    ) -> tuple[ModerationCase, ...]:
        _require_guild_id(guild_id)
        cursor = await self._connection.execute(
            """
            SELECT guild_id, case_id, incident_id, member_id, report_timestamp,
                   offense_number, recommended_action, executed_action, action_expiration,
                   status, review_deadline, reviewer_id, reviewer_decision, appeal_status,
                   close_timestamp
            FROM moderation_cases
            WHERE guild_id = ? AND member_id = ?
            ORDER BY report_timestamp DESC, case_id DESC
            """,
            (guild_id, member_id),
        )
        rows = await cursor.fetchall()
        return tuple(cast(ModerationCase, moderation_case_from_row(row)) for row in rows)
```

Add the five read methods to `ModerationService` in
`src/krubit/services/moderation.py` (add `GetActionReceiptQuery,
GetActionReceiptResult, GetIncidentQuery, GetIncidentResult,
GetIncidentStatusQuery, GetIncidentStatusResult,
GetMemberModerationHistoryQuery, GetMemberModerationHistoryResult,
GetPendingReviewsQuery, GetPendingReviewsResult` to the `contracts.moderation`
import):

```python
    async def get_member_moderation_history(
        self, guild_id: int, query: GetMemberModerationHistoryQuery
    ) -> GetMemberModerationHistoryResult:
        cases = await self._store.list_moderation_cases_for_member(
            int(query.guild_id), int(query.member_id)
        )
        return GetMemberModerationHistoryResult(
            guild_id=query.guild_id,
            member_id=query.member_id,
            cases=tuple(case.case_id for case in cases),
        )

    async def get_incident(self, guild_id: int, query: GetIncidentQuery) -> GetIncidentResult:
        case = await self._store.get_moderation_case_by_incident(
            int(query.guild_id), query.incident_id
        )
        if case is None:
            raise ValueError(f"no case found for incident_id {query.incident_id!r}")
        return GetIncidentResult(
            guild_id=query.guild_id, incident_id=query.incident_id, case_id=case.case_id
        )

    async def get_incident_status(
        self, guild_id: int, query: GetIncidentStatusQuery
    ) -> GetIncidentStatusResult:
        case = await self._store.get_moderation_case_by_incident(
            int(query.guild_id), query.incident_id
        )
        if case is None:
            raise ValueError(f"no case found for incident_id {query.incident_id!r}")
        return GetIncidentStatusResult(
            guild_id=query.guild_id, incident_id=query.incident_id, status=case.status
        )

    async def get_action_receipt(
        self, guild_id: int, query: GetActionReceiptQuery
    ) -> GetActionReceiptResult:
        found = await self._store.get_action_receipt_by_id(query.receipt_id)
        if found is None:
            raise ValueError(f"no receipt found for receipt_id {query.receipt_id!r}")
        receipt, case_id = found
        return GetActionReceiptResult(
            receipt_id=receipt.receipt_id, case_id=case_id, succeeded=receipt.succeeded
        )

    async def get_pending_reviews(
        self, guild_id: int, query: GetPendingReviewsQuery
    ) -> GetPendingReviewsResult:
        cases = await self._store.list_pending_review_cases(int(query.guild_id))
        return GetPendingReviewsResult(
            guild_id=query.guild_id, cases=tuple(case.case_id for case in cases)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: PASS (17 tests total)

- [ ] **Step 5: Commit**

```bash
git add src/krubit/services/moderation.py src/krubit/storage/sqlite.py tests/test_moderation_service.py
git commit -m "feat: implement the five moderation read-operation service methods"
```

---

### Task 8: Cross-cutting integration tests and full verification

**Files:**
- Modify: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: everything from Tasks 5-7.
- Produces: no new production code — proves the design spec's remaining
  cross-cutting guarantees (cross-operation idempotency-key independence,
  concurrent identical-key requests) that per-method tests in Tasks 5-7
  didn't individually cover.

- [ ] **Step 1: Write the cross-cutting tests**

```python
# append to tests/test_moderation_service.py
import asyncio


@pytest.mark.asyncio
async def test_same_idempotency_key_across_two_operations_does_not_collide(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    shared_key = "idem:shared"
    record_response = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:1",
                "guild_id": "100",
                "member_id": "200",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": shared_key,
            }
        ),
    )
    recommend_response = await service.submit_action_recommendation(
        100,
        SubmitActionRecommendationRequest.from_dict(
            {
                "case_id": record_response.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": shared_key,
            }
        ),
    )
    assert recommend_response.duplicate is False
    assert recommend_response.case_id == record_response.case_id


@pytest.mark.asyncio
async def test_concurrent_identical_key_requests_produce_one_case(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    request = RecordIncidentRequest.from_dict(
        {
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:concurrent",
        }
    )
    results = await asyncio.gather(
        service.record_incident(100, request), service.record_incident(100, request)
    )
    assert results[0].case_id == results[1].case_id
    cases = await store.list_moderation_cases_for_member(100, 200)
    assert len(cases) == 1
```

- [ ] **Step 2: Run the moderation-scoped test suite**

Run: `uv run --offline pytest tests/test_moderation_domain.py tests/test_moderation_contract.py tests/test_moderation_storage.py tests/test_moderation_service.py -v`
Expected: PASS, all tests green.

- [ ] **Step 3: Run the full project test suite to confirm no regressions**

Run: `uv run --offline pytest`
Expected: PASS. If a pre-existing environment crash unrelated to this
feature reproduces (as documented in slice 1's build — a Windows
OPENSSL_Applink native crash under `cryptography` import), report it
honestly per that same precedent rather than treating it as a regression;
the moderation-scoped run in Step 2 is the regression-relevant evidence for
this feature's own code.

- [ ] **Step 4: Commit**

```bash
git add tests/test_moderation_service.py
git commit -m "test: add cross-operation idempotency independence and concurrency tests"
```
