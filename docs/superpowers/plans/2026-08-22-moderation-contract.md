# Moderation Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the domain lifecycle model and typed request/response contract for
Krubit's moderation adapter (Phase 5+6 reconciliation, slice 1 of 4) — no storage,
no Discord execution, no `/fetch admin` commands, no Zariya client.

**Architecture:** Two new modules follow existing sibling patterns exactly:
`domain/moderation.py` mirrors `domain/watchdog.py` (frozen `slots=True`
dataclasses, `StrEnum` states, `__post_init__` validation via shared helpers) and
adds a `ModerationCase` lifecycle entity plus a state-transition table/function.
`contracts/moderation.py` mirrors `contracts/zariya.py` (`*_SCHEMA` constants,
`from_dict`/`to_dict`, a `ValueError` subclass for malformed payloads) and adds
one request/response dataclass pair per write operation and one query/result pair
per read operation named in the handoff.

**Tech Stack:** Python 3.12+ (`StrEnum`, `from __future__ import annotations`),
stdlib `dataclasses`/`datetime`/`enum`, `pytest`.

## Global Constraints

- All datetimes are UTC-aware; reject naive datetimes (matches `_require_aware`
  in `domain/watchdog.py` and `_timestamp` in `contracts/zariya.py`).
- Every mutating request dataclass has a required, non-empty `idempotency_key: str`.
- `ModerationCase` must reference an existing `incident_id` as a plain string field
  only — no live DB join or `Incident` import/construction in this slice.
- Reuse `krubit.domain.watchdog._require_positive_id`, `_require_text`, and
  `_require_aware` rather than redefining them (import them; do not copy them).
- Reuse `krubit.security.redaction.redact` for any free-text field that leaves the
  module via `to_dict()`, exactly as `contracts/zariya.py`'s `_evidence()` does.
- `ModerationStatus` values are exactly:
  `recorded, approval_required, approved, rejected, executed, execution_failed,
  duplicate, expired, closed` — no additions, no renames.
- No new third-party dependencies.

---

## File Structure

- Create: `src/krubit/domain/moderation.py` — `ModerationStatus`, `ApprovalDecision`,
  `AppealStatus`, `ModerationCase`, `IllegalTransitionError`, `transition()`.
- Create: `tests/test_moderation_domain.py` — tests for the above.
- Create: `src/krubit/contracts/moderation.py` — `ModerationContractError`, all
  request/response and query/result dataclasses, re-exports `IllegalTransitionError`.
- Create: `tests/test_moderation_contract.py` — tests for the above.

---

### Task 1: Domain lifecycle model — `ModerationStatus`, `ApprovalDecision`, `AppealStatus`, `transition()`

**Files:**
- Create: `src/krubit/domain/moderation.py`
- Test: `tests/test_moderation_domain.py`

**Interfaces:**
- Consumes: `krubit.domain.watchdog._require_positive_id(name: str, value: int) -> None`,
  `_require_text(name: str, value: str, *, limit: int) -> None`,
  `_require_aware(name: str, value: datetime) -> None` (import directly from
  `krubit.domain.watchdog`, do not redefine).
- Produces: `ModerationStatus(StrEnum)` with the 9 values listed in Global
  Constraints; `ApprovalDecision(StrEnum)` with `APPROVED = "approved"`,
  `REJECTED = "rejected"`; `AppealStatus(StrEnum)` with `NONE = "none"`,
  `SUBMITTED = "submitted"`, `UPHELD = "upheld"`, `OVERTURNED = "overturned"`;
  `IllegalTransitionError(ValueError)`; `transition(current: ModerationStatus,
  target: ModerationStatus) -> ModerationStatus`.

- [ ] **Step 1: Write the failing tests for the state machine**

```python
# tests/test_moderation_domain.py
from __future__ import annotations

import pytest

from krubit.domain.moderation import (
    IllegalTransitionError,
    ModerationStatus,
    transition,
)

ALL_STATUSES = list(ModerationStatus)

LEGAL_PAIRS = {
    (ModerationStatus.RECORDED, ModerationStatus.APPROVAL_REQUIRED),
    (ModerationStatus.RECORDED, ModerationStatus.DUPLICATE),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.APPROVED),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.REJECTED),
    (ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.EXPIRED),
    (ModerationStatus.APPROVED, ModerationStatus.EXECUTED),
    (ModerationStatus.APPROVED, ModerationStatus.EXECUTION_FAILED),
    (ModerationStatus.REJECTED, ModerationStatus.CLOSED),
    (ModerationStatus.EXECUTED, ModerationStatus.CLOSED),
    (ModerationStatus.EXECUTION_FAILED, ModerationStatus.APPROVAL_REQUIRED),
    (ModerationStatus.EXECUTION_FAILED, ModerationStatus.CLOSED),
    (ModerationStatus.EXPIRED, ModerationStatus.CLOSED),
}


def test_status_enum_has_exactly_nine_required_values():
    assert {status.value for status in ModerationStatus} == {
        "recorded",
        "approval_required",
        "approved",
        "rejected",
        "executed",
        "execution_failed",
        "duplicate",
        "expired",
        "closed",
    }


@pytest.mark.parametrize("current,target", sorted(LEGAL_PAIRS, key=lambda p: (p[0], p[1])))
def test_legal_transitions_succeed(current, target):
    assert transition(current, target) is target


@pytest.mark.parametrize(
    "current,target",
    [
        (current, target)
        for current in ALL_STATUSES
        for target in ALL_STATUSES
        if (current, target) not in LEGAL_PAIRS
    ],
)
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalTransitionError):
        transition(current, target)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_moderation_domain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'krubit.domain.moderation'`

- [ ] **Step 3: Implement the state machine module**

```python
# src/krubit/domain/moderation.py
"""Moderation case lifecycle: pure value objects and a legal-transition table.

This module tracks *decisions made about* evidence that already exists as a
Phase 3 `krubit.domain.watchdog.Incident` — it does not create evidence and
does not execute Discord actions. See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md.
"""

from __future__ import annotations

from enum import StrEnum


class ModerationStatus(StrEnum):
    RECORDED = "recorded"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    CLOSED = "closed"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class AppealStatus(StrEnum):
    NONE = "none"
    SUBMITTED = "submitted"
    UPHELD = "upheld"
    OVERTURNED = "overturned"


class IllegalTransitionError(ValueError):
    """Raised when a well-formed request asks for a status transition that
    isn't legal from the case's current status."""


_LEGAL_TRANSITIONS: dict[ModerationStatus, frozenset[ModerationStatus]] = {
    ModerationStatus.RECORDED: frozenset(
        {ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.DUPLICATE}
    ),
    ModerationStatus.APPROVAL_REQUIRED: frozenset(
        {ModerationStatus.APPROVED, ModerationStatus.REJECTED, ModerationStatus.EXPIRED}
    ),
    ModerationStatus.APPROVED: frozenset(
        {ModerationStatus.EXECUTED, ModerationStatus.EXECUTION_FAILED}
    ),
    ModerationStatus.REJECTED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.EXECUTED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.EXECUTION_FAILED: frozenset(
        {ModerationStatus.APPROVAL_REQUIRED, ModerationStatus.CLOSED}
    ),
    ModerationStatus.DUPLICATE: frozenset(),
    ModerationStatus.EXPIRED: frozenset({ModerationStatus.CLOSED}),
    ModerationStatus.CLOSED: frozenset(),
}


def transition(current: ModerationStatus, target: ModerationStatus) -> ModerationStatus:
    """Return `target` if `current -> target` is a legal lifecycle transition.

    Raises `IllegalTransitionError` otherwise. Pure function: no I/O, no clock
    reads, matching the sibling `krubit.domain.watchdog` module's convention.
    """
    if target not in _LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(f"{current} -> {target} is not a legal transition")
    return target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_moderation_domain.py -v`
Expected: PASS (all parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add src/krubit/domain/moderation.py tests/test_moderation_domain.py
git commit -m "feat: add moderation status enums and legal-transition state machine"
```

---

### Task 2: `ModerationCase` dataclass with field-pairing validation

**Files:**
- Modify: `src/krubit/domain/moderation.py`
- Modify: `tests/test_moderation_domain.py`

**Interfaces:**
- Consumes: `ModerationStatus`, `ApprovalDecision`, `AppealStatus` (Task 1, same
  file); `krubit.domain.watchdog._require_positive_id`, `_require_text`,
  `_require_aware`.
- Produces: `ModerationCase` frozen dataclass with fields `case_id: str`,
  `incident_id: str`, `guild_id: int`, `member_id: int`,
  `report_timestamp: datetime`, `offense_number: int`,
  `recommended_action: str`, `executed_action: str | None`,
  `action_expiration: datetime | None`, `status: ModerationStatus`,
  `review_deadline: datetime | None`, `reviewer_id: int | None`,
  `reviewer_decision: ApprovalDecision | None`, `appeal_status: AppealStatus`,
  `close_timestamp: datetime | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_domain.py
from datetime import UTC, datetime

from krubit.domain.moderation import (
    ApprovalDecision,
    AppealStatus,
    ModerationCase,
)


def _case(**overrides):
    fields = dict(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        offense_number=1,
        recommended_action="24h timeout",
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
    return ModerationCase(**fields)


def test_valid_case_constructs():
    case = _case()
    assert case.status is ModerationStatus.RECORDED


def test_offense_number_must_be_at_least_one():
    with pytest.raises(ValueError, match="offense_number"):
        _case(offense_number=0)


def test_reviewer_id_and_decision_must_be_paired():
    with pytest.raises(ValueError, match="reviewer"):
        _case(reviewer_id=999, reviewer_decision=None)
    with pytest.raises(ValueError, match="reviewer"):
        _case(reviewer_id=None, reviewer_decision=ApprovalDecision.APPROVED)


def test_reviewer_pair_together_is_valid():
    case = _case(
        status=ModerationStatus.APPROVED,
        reviewer_id=999,
        reviewer_decision=ApprovalDecision.APPROVED,
    )
    assert case.reviewer_id == 999


def test_close_timestamp_requires_closed_status():
    with pytest.raises(ValueError, match="close_timestamp"):
        _case(close_timestamp=datetime(2026, 1, 2, tzinfo=UTC))


def test_close_timestamp_allowed_when_closed():
    case = _case(
        status=ModerationStatus.CLOSED,
        close_timestamp=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert case.status is ModerationStatus.CLOSED


def test_executed_action_only_allowed_when_status_executed():
    with pytest.raises(ValueError, match="executed_action"):
        _case(status=ModerationStatus.EXECUTION_FAILED, executed_action="24h timeout")


def test_executed_action_allowed_when_executed():
    case = _case(status=ModerationStatus.EXECUTED, executed_action="24h timeout")
    assert case.executed_action == "24h timeout"


def test_naive_report_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        _case(report_timestamp=datetime(2026, 1, 1))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_moderation_domain.py -v`
Expected: FAIL with `ImportError: cannot import name 'ModerationCase'`

- [ ] **Step 3: Implement `ModerationCase`**

Append to `src/krubit/domain/moderation.py`:

```python
from dataclasses import dataclass
from datetime import datetime

from krubit.domain.watchdog import _require_aware, _require_positive_id, _require_text

_MAX_ID_LENGTH = 64
_MAX_ACTION_LENGTH = 500


@dataclass(frozen=True, slots=True)
class ModerationCase:
    """Tracks the lifecycle decision made about an existing Phase 3 Incident.

    References `incident_id` as a plain string only — this slice does not
    join against a live `krubit.domain.watchdog.Incident` row.
    """

    case_id: str
    incident_id: str
    guild_id: int
    member_id: int
    report_timestamp: datetime
    offense_number: int
    recommended_action: str
    executed_action: str | None
    action_expiration: datetime | None
    status: ModerationStatus
    review_deadline: datetime | None
    reviewer_id: int | None
    reviewer_decision: ApprovalDecision | None
    appeal_status: AppealStatus
    close_timestamp: datetime | None

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id, limit=_MAX_ID_LENGTH)
        _require_text("incident_id", self.incident_id, limit=_MAX_ID_LENGTH)
        _require_positive_id("guild_id", self.guild_id)
        _require_positive_id("member_id", self.member_id)
        _require_aware("report_timestamp", self.report_timestamp)
        if self.offense_number < 1:
            raise ValueError("offense_number must be at least 1")
        _require_text("recommended_action", self.recommended_action, limit=_MAX_ACTION_LENGTH)
        if self.executed_action is not None:
            _require_text("executed_action", self.executed_action, limit=_MAX_ACTION_LENGTH)
            if self.status is not ModerationStatus.EXECUTED:
                raise ValueError("executed_action may only be set when status is executed")
        if self.action_expiration is not None:
            _require_aware("action_expiration", self.action_expiration)
        if type(self.status) is not ModerationStatus:
            raise ValueError("status must be a ModerationStatus")
        if self.review_deadline is not None:
            _require_aware("review_deadline", self.review_deadline)
        if (self.reviewer_id is None) != (self.reviewer_decision is None):
            raise ValueError("reviewer_id and reviewer_decision must be set together")
        if self.reviewer_id is not None:
            _require_positive_id("reviewer_id", self.reviewer_id)
        if type(self.appeal_status) is not AppealStatus:
            raise ValueError("appeal_status must be an AppealStatus")
        if self.close_timestamp is not None:
            _require_aware("close_timestamp", self.close_timestamp)
            if self.status is not ModerationStatus.CLOSED:
                raise ValueError("close_timestamp may only be set when status is closed")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_moderation_domain.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/domain/moderation.py tests/test_moderation_domain.py
git commit -m "feat: add ModerationCase lifecycle dataclass with field-pairing validation"
```

---

### Task 3: Contract module scaffolding — `ModerationContractError` and shared helpers

**Files:**
- Create: `src/krubit/contracts/moderation.py`
- Create: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: `krubit.domain.moderation.{ModerationStatus, ApprovalDecision,
  AppealStatus, IllegalTransitionError}`; `krubit.security.redaction.redact`.
- Produces: `ModerationContractError(ValueError)`; re-export
  `IllegalTransitionError`; internal helpers `_required_text(payload, key) -> str`,
  `_timestamp(value) -> datetime`, `_optional_timestamp(value) -> datetime | None`
  used by every request/response type in Tasks 4-5.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_moderation_contract.py
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krubit.contracts.moderation import ModerationContractError, _required_text, _timestamp


def test_required_text_raises_on_blank():
    with pytest.raises(ModerationContractError, match="idempotency_key"):
        _required_text({"idempotency_key": "  "}, "idempotency_key")


def test_required_text_returns_stripped_value():
    assert _required_text({"case_id": " case:1 "}, "case_id") == "case:1"


def test_timestamp_requires_timezone():
    with pytest.raises(ModerationContractError, match="timezone"):
        _timestamp("2026-01-01T00:00:00")


def test_timestamp_parses_and_normalizes_to_utc():
    parsed = _timestamp("2026-01-01T00:00:00+00:00")
    assert parsed == datetime(2026, 1, 1, tzinfo=UTC)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'krubit.contracts.moderation'`

- [ ] **Step 3: Implement the scaffolding**

```python
# src/krubit/contracts/moderation.py
"""Typed request/response contract for the Krubit moderation adapter.

Mirrors krubit.contracts.zariya's pattern (schema constant, from_dict/to_dict,
a ValueError subclass for malformed payloads). See
docs/superpowers/specs/2026-08-22-moderation-contract-design.md. This module
defines shape only: no storage, no Discord execution, no dedup lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from krubit.domain.moderation import (
    AppealStatus,
    ApprovalDecision,
    IllegalTransitionError,
    ModerationStatus,
)

__all__ = [
    "IllegalTransitionError",
    "ModerationContractError",
]


class ModerationContractError(ValueError):
    """Raised when a moderation adapter payload is malformed or incomplete."""


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ModerationContractError(f"{key} is required")
    return value


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModerationContractError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModerationContractError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/contracts/moderation.py tests/test_moderation_contract.py
git commit -m "feat: scaffold moderation contract module with shared parsing helpers"
```

---

### Task 4: Write/workflow request-response dataclasses (record, recommend, approve)

**Files:**
- Modify: `src/krubit/contracts/moderation.py`
- Modify: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: Task 3's `_required_text`, `_timestamp`, `_optional_timestamp`,
  `_iso`, `_optional_iso`, `ModerationContractError`; Task 1/2's
  `ModerationStatus`, `ApprovalDecision`.
- Produces: `RecordIncidentRequest`, `RecordIncidentResponse`,
  `SubmitActionRecommendationRequest`, `SubmitActionRecommendationResponse`,
  `RequestHumanApprovalRequest`, `RequestHumanApprovalResponse` — each with
  `from_dict(payload: Mapping[str, object]) -> Self` classmethod and
  `to_dict(self) -> dict[str, JSONValue]` method. Every `*Response` has
  `case_id: str`, `status: ModerationStatus`, `duplicate: bool`,
  `receipt_state: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_contract.py
from krubit.contracts.moderation import (
    ModerationContractError,
    RecordIncidentRequest,
    RecordIncidentResponse,
    RequestHumanApprovalRequest,
    RequestHumanApprovalResponse,
    SubmitActionRecommendationRequest,
    SubmitActionRecommendationResponse,
)
from krubit.domain.moderation import ModerationStatus


def test_record_incident_request_round_trips():
    payload = {
        "incident_id": "incident:1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
        "idempotency_key": "idem:1",
    }
    request = RecordIncidentRequest.from_dict(payload)
    assert request.to_dict() == {
        "incident_id": "incident:1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00Z",
        "idempotency_key": "idem:1",
    }


def test_record_incident_request_requires_idempotency_key():
    payload = {
        "incident_id": "incident:1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(ModerationContractError, match="idempotency_key"):
        RecordIncidentRequest.from_dict(payload)


def test_record_incident_response_round_trips():
    payload = {
        "case_id": "case:1",
        "status": "recorded",
        "duplicate": False,
        "receipt_state": None,
    }
    response = RecordIncidentResponse.from_dict(payload)
    assert response.status is ModerationStatus.RECORDED
    assert response.to_dict() == payload


def test_submit_action_recommendation_round_trips():
    payload = {
        "case_id": "case:1",
        "recommended_action": "24h timeout",
        "idempotency_key": "idem:2",
    }
    request = SubmitActionRecommendationRequest.from_dict(payload)
    assert request.to_dict() == payload

    response_payload = {
        "case_id": "case:1",
        "status": "approval_required",
        "duplicate": False,
        "receipt_state": None,
    }
    response = SubmitActionRecommendationResponse.from_dict(response_payload)
    assert response.to_dict() == response_payload


def test_request_human_approval_round_trips():
    payload = {
        "case_id": "case:1",
        "review_deadline": "2026-01-02T00:00:00+00:00",
        "idempotency_key": "idem:3",
    }
    request = RequestHumanApprovalRequest.from_dict(payload)
    assert request.to_dict() == payload

    response_payload = {
        "case_id": "case:1",
        "status": "approval_required",
        "duplicate": True,
        "receipt_state": None,
    }
    response = RequestHumanApprovalResponse.from_dict(response_payload)
    assert response.duplicate is True
    assert response.to_dict() == response_payload
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: FAIL with `ImportError: cannot import name 'RecordIncidentRequest'`

- [ ] **Step 3: Implement the dataclasses**

Append to `src/krubit/contracts/moderation.py`:

```python
from dataclasses import dataclass

from krubit.domain.models import JSONValue

_MAX_ID_LENGTH = 64
_MAX_ACTION_LENGTH = 500


def _status(payload: Mapping[str, object]) -> ModerationStatus:
    raw = _required_text(payload, "status")
    try:
        return ModerationStatus(raw)
    except ValueError as exc:
        raise ModerationContractError(f"unknown status: {raw!r}") from exc


def _duplicate(payload: Mapping[str, object]) -> bool:
    value = payload.get("duplicate")
    if not isinstance(value, bool):
        raise ModerationContractError("duplicate must be a boolean")
    return value


def _receipt_state(payload: Mapping[str, object]) -> str | None:
    value = payload.get("receipt_state")
    if value is None:
        return None
    return str(value)


@dataclass(frozen=True, slots=True)
class RecordIncidentRequest:
    incident_id: str
    guild_id: int
    member_id: int
    report_timestamp: datetime
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RecordIncidentRequest":
        raw_guild_id = _required_text(payload, "guild_id")
        raw_member_id = _required_text(payload, "member_id")
        if not raw_guild_id.isdigit() or int(raw_guild_id) <= 0:
            raise ModerationContractError("guild_id must be positive numeric text")
        if not raw_member_id.isdigit() or int(raw_member_id) <= 0:
            raise ModerationContractError("member_id must be positive numeric text")
        return cls(
            incident_id=_required_text(payload, "incident_id"),
            guild_id=int(raw_guild_id),
            member_id=int(raw_member_id),
            report_timestamp=_timestamp(payload.get("report_timestamp")),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "incident_id": self.incident_id,
            "guild_id": str(self.guild_id),
            "member_id": str(self.member_id),
            "report_timestamp": _iso(self.report_timestamp),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RecordIncidentResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RecordIncidentResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class SubmitActionRecommendationRequest:
    case_id: str
    recommended_action: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SubmitActionRecommendationRequest":
        return cls(
            case_id=_required_text(payload, "case_id"),
            recommended_action=_required_text(payload, "recommended_action"),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "recommended_action": self.recommended_action,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class SubmitActionRecommendationResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SubmitActionRecommendationResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class RequestHumanApprovalRequest:
    case_id: str
    review_deadline: datetime
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RequestHumanApprovalRequest":
        return cls(
            case_id=_required_text(payload, "case_id"),
            review_deadline=_timestamp(payload.get("review_deadline")),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "review_deadline": _iso(self.review_deadline),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RequestHumanApprovalResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RequestHumanApprovalResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }
```

Add these four class names to the module's `__all__` list.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/contracts/moderation.py tests/test_moderation_contract.py
git commit -m "feat: add record/recommend/approve moderation contract dataclasses"
```

---

### Task 5: Write/workflow request-response dataclasses (execute, close, appeal)

**Files:**
- Modify: `src/krubit/contracts/moderation.py`
- Modify: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: same helpers as Task 4, plus `AppealStatus`.
- Produces: `ExecuteApprovedActionRequest`, `ExecuteApprovedActionResponse`,
  `CloseIncidentRequest`, `CloseIncidentResponse`, `SubmitAppealRequest`,
  `SubmitAppealResponse` — same `from_dict`/`to_dict` pattern as Task 4.
  `CloseIncidentRequest.decision` is a free-text `str` (the human-readable close
  reason, per the handoff's `close_incident(incident_id, decision)`), distinct
  from `ApprovalDecision`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_contract.py
from krubit.contracts.moderation import (
    CloseIncidentRequest,
    CloseIncidentResponse,
    ExecuteApprovedActionRequest,
    ExecuteApprovedActionResponse,
    SubmitAppealRequest,
    SubmitAppealResponse,
)
from krubit.domain.moderation import AppealStatus


def test_execute_approved_action_round_trips():
    payload = {
        "case_id": "case:1",
        "idempotency_key": "idem:4",
    }
    request = ExecuteApprovedActionRequest.from_dict(payload)
    assert request.to_dict() == payload

    response_payload = {
        "case_id": "case:1",
        "status": "executed",
        "duplicate": False,
        "receipt_state": "receipt:1",
    }
    response = ExecuteApprovedActionResponse.from_dict(response_payload)
    assert response.receipt_state == "receipt:1"
    assert response.to_dict() == response_payload


def test_close_incident_round_trips():
    payload = {
        "case_id": "case:1",
        "decision": "resolved, no further action",
        "idempotency_key": "idem:5",
    }
    request = CloseIncidentRequest.from_dict(payload)
    assert request.to_dict() == payload

    response_payload = {
        "case_id": "case:1",
        "status": "closed",
        "duplicate": False,
        "receipt_state": None,
    }
    response = CloseIncidentResponse.from_dict(response_payload)
    assert response.to_dict() == response_payload


def test_submit_appeal_round_trips():
    payload = {
        "case_id": "case:1",
        "reason": "member disputes the timeout",
        "idempotency_key": "idem:6",
    }
    request = SubmitAppealRequest.from_dict(payload)
    assert request.to_dict() == payload

    response_payload = {
        "case_id": "case:1",
        "appeal_status": "submitted",
        "duplicate": False,
    }
    response = SubmitAppealResponse.from_dict(response_payload)
    assert response.appeal_status is AppealStatus.SUBMITTED
    assert response.to_dict() == response_payload
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: FAIL with `ImportError: cannot import name 'ExecuteApprovedActionRequest'`

- [ ] **Step 3: Implement the dataclasses**

Append to `src/krubit/contracts/moderation.py`:

```python
def _appeal_status(payload: Mapping[str, object]) -> AppealStatus:
    raw = _required_text(payload, "appeal_status")
    try:
        return AppealStatus(raw)
    except ValueError as exc:
        raise ModerationContractError(f"unknown appeal_status: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class ExecuteApprovedActionRequest:
    case_id: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExecuteApprovedActionRequest":
        return cls(
            case_id=_required_text(payload, "case_id"),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"case_id": self.case_id, "idempotency_key": self.idempotency_key}


@dataclass(frozen=True, slots=True)
class ExecuteApprovedActionResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExecuteApprovedActionResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class CloseIncidentRequest:
    case_id: str
    decision: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CloseIncidentRequest":
        return cls(
            case_id=_required_text(payload, "case_id"),
            decision=_required_text(payload, "decision"),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "decision": self.decision,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class CloseIncidentResponse:
    case_id: str
    status: ModerationStatus
    duplicate: bool
    receipt_state: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CloseIncidentResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            status=_status(payload),
            duplicate=_duplicate(payload),
            receipt_state=_receipt_state(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "duplicate": self.duplicate,
            "receipt_state": self.receipt_state,
        }


@dataclass(frozen=True, slots=True)
class SubmitAppealRequest:
    case_id: str
    reason: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SubmitAppealRequest":
        return cls(
            case_id=_required_text(payload, "case_id"),
            reason=_required_text(payload, "reason"),
            idempotency_key=_required_text(payload, "idempotency_key"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class SubmitAppealResponse:
    case_id: str
    appeal_status: AppealStatus
    duplicate: bool

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SubmitAppealResponse":
        return cls(
            case_id=_required_text(payload, "case_id"),
            appeal_status=_appeal_status(payload),
            duplicate=_duplicate(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "case_id": self.case_id,
            "appeal_status": self.appeal_status.value,
            "duplicate": self.duplicate,
        }
```

Add these six class names to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/contracts/moderation.py tests/test_moderation_contract.py
git commit -m "feat: add execute/close/appeal moderation contract dataclasses"
```

---

### Task 6: Read-operation query/result dataclasses

**Files:**
- Modify: `src/krubit/contracts/moderation.py`
- Modify: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: same helpers as Tasks 4-5.
- Produces: `GetMemberModerationHistoryQuery`/`Result`,
  `GetIncidentQuery`/`Result`, `GetIncidentStatusQuery`/`Result`,
  `GetActionReceiptQuery`/`Result`, `GetPendingReviewsQuery`/`Result` — same
  `from_dict`/`to_dict` pattern, no `idempotency_key` (reads are not mutating).
  `GetMemberModerationHistoryResult.cases` and `GetPendingReviewsResult.cases`
  are `tuple[str, ...]` of `case_id` values (this slice has no storage to join
  full `ModerationCase` records against; later slices replace this with real
  hydrated records without changing the query dataclasses).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_contract.py
from krubit.contracts.moderation import (
    GetActionReceiptQuery,
    GetActionReceiptResult,
    GetIncidentQuery,
    GetIncidentResult,
    GetIncidentStatusQuery,
    GetIncidentStatusResult,
    GetMemberModerationHistoryQuery,
    GetMemberModerationHistoryResult,
    GetPendingReviewsQuery,
    GetPendingReviewsResult,
)


def test_get_member_moderation_history_round_trips():
    query = GetMemberModerationHistoryQuery.from_dict({"member_id": "200"})
    assert query.to_dict() == {"member_id": "200"}

    result_payload = {"member_id": "200", "cases": ["case:1", "case:2"]}
    result = GetMemberModerationHistoryResult.from_dict(result_payload)
    assert result.cases == ("case:1", "case:2")
    assert result.to_dict() == result_payload


def test_get_incident_round_trips():
    query = GetIncidentQuery.from_dict({"incident_id": "incident:1"})
    assert query.to_dict() == {"incident_id": "incident:1"}

    result_payload = {"incident_id": "incident:1", "case_id": "case:1"}
    result = GetIncidentResult.from_dict(result_payload)
    assert result.to_dict() == result_payload


def test_get_incident_status_round_trips():
    query = GetIncidentStatusQuery.from_dict({"incident_id": "incident:1"})
    assert query.to_dict() == {"incident_id": "incident:1"}

    result_payload = {"incident_id": "incident:1", "status": "closed"}
    result = GetIncidentStatusResult.from_dict(result_payload)
    assert result.status.value == "closed"
    assert result.to_dict() == result_payload


def test_get_action_receipt_round_trips():
    query = GetActionReceiptQuery.from_dict({"receipt_id": "receipt:1"})
    assert query.to_dict() == {"receipt_id": "receipt:1"}

    result_payload = {"receipt_id": "receipt:1", "case_id": "case:1", "succeeded": True}
    result = GetActionReceiptResult.from_dict(result_payload)
    assert result.to_dict() == result_payload


def test_get_pending_reviews_round_trips():
    query = GetPendingReviewsQuery.from_dict({"guild_id": "100"})
    assert query.to_dict() == {"guild_id": "100"}

    result_payload = {"guild_id": "100", "cases": ["case:1"]}
    result = GetPendingReviewsResult.from_dict(result_payload)
    assert result.cases == ("case:1",)
    assert result.to_dict() == result_payload
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: FAIL with `ImportError: cannot import name 'GetMemberModerationHistoryQuery'`

- [ ] **Step 3: Implement the dataclasses**

Append to `src/krubit/contracts/moderation.py`:

```python
def _tuple_of_str(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModerationContractError(f"{key} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class GetMemberModerationHistoryQuery:
    member_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetMemberModerationHistoryQuery":
        return cls(member_id=_required_text(payload, "member_id"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"member_id": self.member_id}


@dataclass(frozen=True, slots=True)
class GetMemberModerationHistoryResult:
    member_id: str
    cases: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetMemberModerationHistoryResult":
        return cls(
            member_id=_required_text(payload, "member_id"),
            cases=_tuple_of_str(payload, "cases"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"member_id": self.member_id, "cases": list(self.cases)}


@dataclass(frozen=True, slots=True)
class GetIncidentQuery:
    incident_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetIncidentQuery":
        return cls(incident_id=_required_text(payload, "incident_id"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"incident_id": self.incident_id}


@dataclass(frozen=True, slots=True)
class GetIncidentResult:
    incident_id: str
    case_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetIncidentResult":
        return cls(
            incident_id=_required_text(payload, "incident_id"),
            case_id=_required_text(payload, "case_id"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"incident_id": self.incident_id, "case_id": self.case_id}


@dataclass(frozen=True, slots=True)
class GetIncidentStatusQuery:
    incident_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetIncidentStatusQuery":
        return cls(incident_id=_required_text(payload, "incident_id"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"incident_id": self.incident_id}


@dataclass(frozen=True, slots=True)
class GetIncidentStatusResult:
    incident_id: str
    status: ModerationStatus

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetIncidentStatusResult":
        return cls(
            incident_id=_required_text(payload, "incident_id"),
            status=_status(payload),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"incident_id": self.incident_id, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class GetActionReceiptQuery:
    receipt_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetActionReceiptQuery":
        return cls(receipt_id=_required_text(payload, "receipt_id"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"receipt_id": self.receipt_id}


@dataclass(frozen=True, slots=True)
class GetActionReceiptResult:
    receipt_id: str
    case_id: str
    succeeded: bool

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetActionReceiptResult":
        succeeded = payload.get("succeeded")
        if not isinstance(succeeded, bool):
            raise ModerationContractError("succeeded must be a boolean")
        return cls(
            receipt_id=_required_text(payload, "receipt_id"),
            case_id=_required_text(payload, "case_id"),
            succeeded=succeeded,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"receipt_id": self.receipt_id, "case_id": self.case_id, "succeeded": self.succeeded}


@dataclass(frozen=True, slots=True)
class GetPendingReviewsQuery:
    guild_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetPendingReviewsQuery":
        return cls(guild_id=_required_text(payload, "guild_id"))

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id}


@dataclass(frozen=True, slots=True)
class GetPendingReviewsResult:
    guild_id: str
    cases: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "GetPendingReviewsResult":
        return cls(
            guild_id=_required_text(payload, "guild_id"),
            cases=_tuple_of_str(payload, "cases"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {"guild_id": self.guild_id, "cases": list(self.cases)}
```

Add these ten class names to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_moderation_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/krubit/contracts/moderation.py tests/test_moderation_contract.py
git commit -m "feat: add moderation contract read-operation query/result dataclasses"
```

---

### Task 7: Idempotency round-trip contract test and full-suite verification

**Files:**
- Modify: `tests/test_moderation_contract.py`

**Interfaces:**
- Consumes: everything from Tasks 4-6.
- Produces: no new production code; a cross-cutting test proving the
  idempotency-key shape rule from the spec ("two identical requests must yield
  identical `case_id`" at the contract level — this test checks the request
  dataclasses themselves are deterministic on `from_dict`, since no storage
  exists yet to actually deduplicate).

- [ ] **Step 1: Write the idempotency shape test**

```python
# append to tests/test_moderation_contract.py
def test_identical_idempotency_key_produces_identical_request_dataclass():
    payload = {
        "incident_id": "incident:1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
        "idempotency_key": "idem:same",
    }
    first = RecordIncidentRequest.from_dict(payload)
    second = RecordIncidentRequest.from_dict(dict(payload))
    assert first == second


def test_duplicate_response_carries_original_case_id():
    payload = {
        "case_id": "case:original",
        "status": "recorded",
        "duplicate": True,
        "receipt_state": None,
    }
    response = RecordIncidentResponse.from_dict(payload)
    assert response.duplicate is True
    assert response.case_id == "case:original"
```

- [ ] **Step 2: Run the full moderation test suite**

Run: `uv run pytest tests/test_moderation_domain.py tests/test_moderation_contract.py -v`
Expected: PASS, all tests green

- [ ] **Step 3: Run the full project test suite to confirm no regressions**

Run: `uv run pytest`
Expected: PASS (existing tests unaffected — no existing file was modified except
the two new test files and two new source files)

- [ ] **Step 4: Commit**

```bash
git add tests/test_moderation_contract.py
git commit -m "test: add idempotency-key shape contract tests for moderation adapter"
```

---

### Task 8: Update roadmap doc to record this slice against Phase 5+6

**Files:**
- Modify: `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: a short status note under Phase 5 and Phase 6 pointing at the new
  spec/plan, following the existing "Implementation status (date): ..." pattern
  already used elsewhere in that file (e.g. Phase 1's line 117).

- [ ] **Step 1: Add a status note to Phase 5**

In `docs/roadmaps/2026-08-03-krubit-phase-rollout.md`, immediately under the
`## Phase 5: Configurable Light Management` heading, insert:

```markdown
**Implementation status (2026-08-22):** Slice 1 of 4 (shared moderation
contract — lifecycle state machine and typed request/response types, no
execution yet) is implemented; see
[2026-08-22-moderation-contract-design.md](../superpowers/specs/2026-08-22-moderation-contract-design.md).
This phase and Phase 6 are being built out together per the Zariya–Krubit
Moderation Integration Handoff, reconciled into these existing roadmap slots
rather than tracked as separate phases.
```

- [ ] **Step 2: Add the same cross-reference to Phase 6**

Immediately under the `## Phase 6: Zariya Companion Bridge and Supervised Protection`
heading, insert:

```markdown
**Implementation status (2026-08-22):** See Phase 5's status note above — this
phase and Phase 5 are being reconciled together against the Zariya–Krubit
Moderation Integration Handoff, starting with the shared contract slice.
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmaps/2026-08-03-krubit-phase-rollout.md
git commit -m "docs: cross-reference moderation contract slice against Phase 5/6"
```
