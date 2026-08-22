# Moderation Contract Design (Phase 5+6 reconciliation, slice 1 of 4)

**Status:** Approved
**Scope:** Shared request/response contract and lifecycle state machine for the
Krubit moderation adapter described in the Zariya–Krubit Moderation Integration
Handoff. No storage, no Discord execution, no `/fetch admin` commands, no Zariya
client. Those are separate later slices.

## Background

The handoff describes governing the Zariya-to-Krubit moderation relationship
through a typed adapter, in five phases. This project's own roadmap
(`docs/roadmaps/2026-08-03-krubit-phase-rollout.md`) already reserves this space
as **Phase 5: Configurable Light Management** (bounded autonomous execution
authority) and **Phase 6: Zariya Companion Bridge and Supervised Protection**
(governed action proposals, incident handoff, `#mod-room`-style projections) —
neither has been built. This design treats the handoff's 5 phases as sub-slices
of the existing Phase 5+6 slots, following the same reconciliation pattern
already used for Phase 5/7/8/9 in that roadmap's 2026-08-16 update.

This slice is the handoff's own **Phase 1: shared contract**. It defines types,
lifecycle states, and idempotency rules, with contract tests, before any
execution logic exists.

Existing building blocks this design reuses rather than duplicates:

- `domain/watchdog.py` — `Incident` (evidence-only, `RiskBand.INCIDENT` only,
  `acknowledged_by` but no execution/approval/appeal state) and
  `EvidencePacket` (redacted evidence, signals, message links, event IDs).
  Phase 3's design intentionally keeps `Incident` execution-free; this design
  does not add lifecycle fields to it.
- `contracts/zariya.py` — the `ZariyaSignal` Phase 0 contract pattern:
  `*_SCHEMA` version string, `from_dict`/`to_dict`, a `*ContractError(ValueError)`
  subclass, UTC-normalized ISO-8601 timestamps, `redact()` applied to evidence
  before it leaves the module.
- `security/redaction.py` — `redact()`, reused for any free-text field in the
  new contract exactly as `EvidencePacket.to_storage_dict()` and
  `ZariyaSignal.to_dict()` already do.

## Data model — `domain/moderation.py`

New module, same conventions as `domain/watchdog.py`: frozen `slots=True`
dataclasses, `StrEnum` states, `__post_init__` validation using the existing
`_require_positive_id` / `_require_text` / `_require_aware` helpers (imported,
not re-implemented).

### `ModerationCase`

The lifecycle entity. Keyed by `case_id`. References an existing Phase 3
`Incident` by `incident_id` — a `ModerationCase` cannot exist without a prior
`Incident`; nothing in this contract creates evidence, it only tracks decisions
made about evidence that already exists.

Fields (drawn from the handoff's "minimum incident record," excluding what
`Incident`/`EvidencePacket` already own — occurrence timestamp, category/
severity signals, evidence reference):

```
case_id: str
incident_id: str          # foreign key to domain.watchdog.Incident
guild_id: int
member_id: int
report_timestamp: datetime
offense_number: int       # 1-based; overturned/dismissed cases never increment this
recommended_action: str
executed_action: str | None
action_expiration: datetime | None
status: ModerationStatus
review_deadline: datetime | None
reviewer_id: int | None
reviewer_decision: ApprovalDecision | None
appeal_status: AppealStatus
close_timestamp: datetime | None
```

Validation rules enforced in `__post_init__`:

- `offense_number >= 1`.
- `reviewer_decision` set only if `reviewer_id` is set and vice versa.
- `close_timestamp` set only when `status == ModerationStatus.closed`.
- `executed_action` set only when `status == ModerationStatus.executed` (an
  `execution_failed` case has an attempted action but no confirmed executed
  one, so it stays `None` there).
- All datetimes UTC-aware, via `_require_aware`.

### `ModerationStatus(StrEnum)`

Exactly the nine values the handoff requires:

```
recorded, approval_required, approved, rejected,
executed, execution_failed, duplicate, expired, closed
```

### `ApprovalDecision(StrEnum)`: `approved`, `rejected`

### `AppealStatus(StrEnum)`: `none`, `submitted`, `upheld`, `overturned`

### State machine

A plain data table, not enum methods (matches this codebase's preference for
small pure functions over behavior-heavy types):

```python
_LEGAL_TRANSITIONS: dict[ModerationStatus, frozenset[ModerationStatus]] = {
    ModerationStatus.recorded: frozenset({ModerationStatus.approval_required, ModerationStatus.duplicate}),
    ModerationStatus.approval_required: frozenset({ModerationStatus.approved, ModerationStatus.rejected, ModerationStatus.expired}),
    ModerationStatus.approved: frozenset({ModerationStatus.executed, ModerationStatus.execution_failed}),
    ModerationStatus.rejected: frozenset({ModerationStatus.closed}),
    ModerationStatus.executed: frozenset({ModerationStatus.closed}),
    ModerationStatus.execution_failed: frozenset({ModerationStatus.approval_required, ModerationStatus.closed}),
    ModerationStatus.duplicate: frozenset(),   # terminal: dedup response only, not a real case
    ModerationStatus.expired: frozenset({ModerationStatus.closed}),
    ModerationStatus.closed: frozenset(),      # terminal; reopen is a distinct future capability, not a transition
}


def transition(current: ModerationStatus, target: ModerationStatus) -> ModerationStatus:
    if target not in _LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(f"{current} -> {target} is not a legal transition")
    return target
```

`execution_failed -> approval_required` covers the handoff's "a failed or
missing Discord receipt leaves the action unresolved and visible for review."

## Contract — `contracts/moderation.py`

Same pattern as `contracts/zariya.py`: one `*_SCHEMA` constant per top-level
message type, `from_dict`/`to_dict` classmethods/methods, a
`ModerationContractError(ValueError)` for malformed payloads, and a distinct
`IllegalTransitionError(ValueError)` (raised by `domain/moderation.transition`,
re-exported here) so callers can tell "malformed request" apart from "well-formed
request, illegal state transition."

### Write/workflow operations (one request + one response dataclass each)

- `RecordIncidentRequest` / `RecordIncidentResponse`
- `SubmitActionRecommendationRequest` / `...Response`
- `RequestHumanApprovalRequest` / `...Response`
- `ExecuteApprovedActionRequest` / `...Response`
- `CloseIncidentRequest` / `...Response`
- `SubmitAppealRequest` / `...Response`

Every mutating request dataclass carries a required `idempotency_key: str`
(non-empty, validated in `__post_init__` via `_required_text`-equivalent).
Every response carries `case_id: str`, `status: ModerationStatus`,
`duplicate: bool`, and `receipt_state: str | None` (opaque reference to a
Discord action receipt id; receipt storage itself is a later slice).

**Idempotency rule for this slice:** the contract defines the *shape* only —
`duplicate=True` responses must carry the original `case_id` they matched, and
a contract test asserts that two `from_dict` round-trips of the same
`idempotency_key` produce dataclasses that compare equal on every field except
timestamp-of-construction. Actual store-and-look-up dedup logic is out of scope
here (Phase 2 of the handoff / storage slice).

### Read operations (typed query/result pairs, no idempotency key needed)

- `get_member_moderation_history(member_id)`
- `get_incident(incident_id)`
- `get_incident_status(incident_id)`
- `get_action_receipt(receipt_id)`
- `get_pending_reviews()`

Each gets a `*Query` dataclass (input) and a `*Result` dataclass (output) in
the same module, following the same `from_dict`/`to_dict` pattern. No live
implementation — these are type contracts only in this slice; a later slice
wires them to storage.

## Error handling

- `ModerationContractError(ValueError)` — malformed/incomplete payload
  (missing required field, bad timestamp, empty idempotency key). Parallels
  `SignalContractError`.
- `IllegalTransitionError(ValueError)` — well-formed request, but the
  requested status transition isn't legal from the case's current status.
  Defined in `domain/moderation.py`, re-exported from `contracts/moderation.py`
  for callers who only import the contract module.

Both are `ValueError` subclasses so existing exception-handling patterns in
this codebase (which generally catch `ValueError` at the boundary) keep
working without a new base exception type.

## Testing

New file `tests/test_moderation_contract.py`:

- Round-trip `to_dict()` -> `from_dict()` for every request/response and
  query/result dataclass.
- Every entry in `_LEGAL_TRANSITIONS`: each legal transition succeeds, and for
  every status, every non-listed target raises `IllegalTransitionError`
  (exhaustive, not spot-checked — generated from the table itself so the test
  and the implementation can't silently drift apart).
- `idempotency_key` required and non-empty on every mutating request; omitting
  it raises `ModerationContractError`.
- `ModerationCase.__post_init__` validation: offense_number floor,
  reviewer_id/reviewer_decision pairing, close_timestamp only when closed,
  executed_action only when status is `executed`.
- A `ModerationCase` referencing an `incident_id` is a plain string field at
  this layer — no live DB join exists yet, so the test only asserts the field
  type/shape, not referential integrity against a real `Incident` row.

## Explicit non-goals for this slice

No persistence/storage, no Discord adapter or action execution, no
`/fetch admin` commands, no `#mod-room` posting, no Zariya-side client, no
real idempotency-key deduplication (store-and-look-up). Each is a separate
spec once this slice's exit criteria are met.

## Exit criteria

All contract tests pass; every status in the handoff's required set is
represented; every request/response type round-trips through `to_dict`/
`from_dict` without loss; `ModerationCase` cannot be constructed in a state
that violates the field-pairing rules above.
