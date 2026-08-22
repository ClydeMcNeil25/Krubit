# Moderation Storage Service Design (Phase 5+6 reconciliation, slice 2 of 4)

**Status:** Approved
**Scope:** Persist `ModerationCase`, enforce idempotency and legal state
transitions, implement all 11 contract operations as real async methods
against SQLite, plus one internal (non-Zariya-exposed) human-decision method
needed to complete the lifecycle. No HTTP transport (slice 3), no real
Discord mutation (an injected `ActionExecutor` protocol stands in; a real
implementation is slice 4), no `/fetch admin` commands, no `#mod-room`
posting.

## Background

Slice 1 ([2026-08-22-moderation-contract-design.md](2026-08-22-moderation-contract-design.md))
built the typed contract and lifecycle types only — no persistence. KAI-Studio
(Zariya's side, built independently in Codex under
`src/kshq_next/adapters/krubit_moderation/`) has aligned to slice 1's locked
wire shapes and is waiting on a real transport. Before transport can mean
anything, the operations it will carry need a real implementation behind
them — that's this slice.

KAI-Studio's design review (relayed 2026-08-22) found eight real gaps in the
first draft of this slice, all resolved below. The most important is #3: the
original draft had no receipt table at all, even though `get_action_receipt`
is one of the five locked read operations.

## Schema — two new tables in `storage/sqlite.py`

Following the exact `incidents` table precedent already in that file
(`_write_transaction()`, upsert via `INSERT ... ON CONFLICT DO UPDATE`,
`_require_guild_id`):

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

**Case identity (point 1):** `case_id` is generated as `case:<uuid4 hex>` —
globally unique, backed by its own `UNIQUE INDEX` in addition to the
`(guild_id, case_id)` primary key. Every case-bound contract request
(`ExecuteApprovedActionRequest`, `CloseIncidentRequest`, `SubmitAppealRequest`,
`GetIncidentResult`, `GetActionReceiptQuery`, ...) carries only `case_id`, not
`guild_id` — that's the locked slice-1 shape, unchanged. The unique index on
`case_id` alone makes lookup-by-case_id-only unambiguous without touching any
locked wire type; `guild_id` is read off the resolved row afterward for
isolation checks.

**Idempotency scope (point 2):** key is `(guild_id, operation,
idempotency_key)`. Reusing the same key value across two different
operations no longer collides — each gets its own row instead of one op
returning the other's cached response with the wrong shape. Lookup and
insert happen inside the same `_write_transaction()` call as the case
mutation, so a duplicate-key hit and a fresh insert can never race against
each other on the same connection.

**Incident linkage (point 7):** `record_incident` must validate the
referenced `incident_id` exists (`SQLiteStore.get_incident(guild_id,
incident_id)` returns non-null) before creating a case, raising
`UnknownIncidentError` otherwise. `UNIQUE (guild_id, incident_id)` on
`moderation_cases` enforces at most one case per incident as a DB-level
backstop against idempotency-key races (two different keys both racing to
record the same incident).

## Domain change: `ModerationCase.recommended_action` becomes optional

`domain/moderation.py`'s `ModerationCase.recommended_action: str | None`
(was `str`). This is a domain-internal type — never serialized directly to
Zariya (only `case_id`/`status`/`duplicate`/`receipt_state` cross the wire) —
so changing it does not touch any locked slice-1 contract shape. `__post_init__`
validation: when not `None`, the same length/blank checks as before apply.

**Point 4 resolution:** `record_incident` creates the case with
`recommended_action=None`. `submit_action_recommendation` is the only
operation that sets it, and is legal only while `status == RECORDED`
(enforced in the service layer, not the domain type — `ModerationCase`
itself doesn't know about operation ordering). `offense_number` is computed
deterministically: `1 + count(prior cases for (guild_id, member_id) where
appeal_status != OVERTURNED)` — no undocumented sentinel.

## New module: `services/moderation.py`

Mirrors `services/incident_evidence.py`'s shape: async functions/methods
taking `SQLiteStore` and domain types, no Discord or HTTP knowledge.

### `ActionExecutor` protocol

```python
class ActionExecutor(Protocol):
    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt: ...
```

Caller-supplied, mirroring `SignedWebhook`'s injected-protocol shape in
`web/callbacks.py`. `execution_key` (point 6) is the
`ExecuteApprovedActionRequest.idempotency_key` already on hand — reused, not
freshly generated — so a real Discord adapter (slice 4) can recognize a retry
after an external success but before SQLite persistence. The executor call
happens **outside** any `_write_transaction()` (a DB transaction cannot be
held open across a network call); the resulting transition, receipt row, and
idempotency-cache write happen in one transaction immediately after the
executor returns. This slice proves the key is threaded through correctly
against a fake executor; it does not claim exactly-once Discord behavior end
to end, since no real executor exists until slice 4.

```python
@dataclass(frozen=True, slots=True)
class ActionReceipt:
    receipt_id: str
    succeeded: bool
    detail: str
```

**Point 3 resolution — `receipt_state` defined exactly:** every contract
response's `receipt_state` field is the persisted `receipt_id` when one
exists for that case, `None` otherwise. A receipt row is written for both
success and failure outcomes of `execute_approved_action`; only a crash
between the executor call returning and the write completing leaves it
`None` (and the case stays `APPROVED`, unresolved, per slice 1's original
"a failed or missing Discord receipt leaves the action unresolved and
visible for review" rule).

### `ModerationService`

One class wrapping `SQLiteStore` and an injected `ActionExecutor`. One async
method per contract operation, each following the same shape: check the
idempotency table first (return the cached response verbatim on a hit,
never re-execute); else validate via `domain.moderation.transition()`;
persist inside `_write_transaction()`; cache the response; return it.

**Point 5 — explicit state matrix**, resolving a real gap: the original 11
Zariya-facing operations from the handoff never include an "approve" or
"reject" write op — `request_human_approval` only requests review;
recording the human's decision is a `/fetch admin` (Discord command,
slice 4) action, not a Zariya API call. This slice adds one **internal**
method, `record_human_decision(case_id, reviewer_id, decision)`, not part of
the 11-operation Zariya contract and not reachable through
`contracts/moderation.py` — reserved for slice 4's admin commands, added now
only so this slice's own tests can drive a case through the full lifecycle.

| Method | Transition | Precondition |
|---|---|---|
| `record_incident` | creates case at `RECORDED` | `incident_id` exists, no case yet for it |
| `submit_action_recommendation` | sets `recommended_action`, no status change | `status == RECORDED` |
| `request_human_approval` | `RECORDED → APPROVAL_REQUIRED` | `recommended_action` is set |
| `record_human_decision` (internal) | `APPROVAL_REQUIRED → APPROVED` or `→ REJECTED` | `status == APPROVAL_REQUIRED` |
| `execute_approved_action` | `APPROVED → EXECUTED` or `→ EXECUTION_FAILED` | `status == APPROVED` |
| `close_incident` | `→ CLOSED` | `status ∈ {REJECTED, EXECUTED, EXECUTION_FAILED, EXPIRED}` |
| `submit_appeal` | sets `appeal_status: NONE → SUBMITTED` only | `appeal_status == NONE`, `status ∈ {EXECUTED, CLOSED}` |

Every transition still goes through `domain.moderation.transition()` — this
table names the operation-level preconditions layered on top, not a
replacement for the already-locked legal-transition table.

### Storage row helpers: `storage/moderation_rows.py`

Mirrors `storage/watchdog_rows.py`: `moderation_case_from_row(row) ->
ModerationCase | None`, `action_receipt_from_row(row) -> ActionReceipt |
None`.

## Testing

`tests/test_moderation_service.py`, using a real in-memory `SQLiteStore`
(matching existing storage test conventions) and a fake `ActionExecutor`:

- Full lifecycle round-trip: `record_incident` → `submit_action_recommendation`
  → `request_human_approval` → `record_human_decision` (approve) →
  `execute_approved_action` (success) → `close_incident`.
- Duplicate call, same `(guild_id, operation, idempotency_key)`: cached
  response returned verbatim, no re-persist, no duplicate row.
- Same idempotency key reused across two different operations: independent
  rows, no collision, no wrong-shape response (point 2).
- Illegal transition at every layer (operation-level precondition and the
  underlying `transition()` call): rejected, stored state unchanged.
- `record_incident` against an unknown `incident_id`: raises
  `UnknownIncidentError`, no case created.
- Second `record_incident` for an already-cased `incident_id` (different
  idempotency key): rejected by the `UNIQUE(guild_id, incident_id)`
  constraint, surfaced as a clear service-level error, not a raw SQLite
  exception.
- `get_action_receipt` for an existing and a missing `receipt_id`.
- `execute_approved_action` success and failure paths against the fake
  executor, confirming `execution_key` equals the request's
  `idempotency_key` and `receipt_state` is set correctly in both outcomes.
- Concurrent identical-key requests via `asyncio.gather()`: both calls
  return an identical `case_id`/response, only one row persisted for that
  key — documented honestly as proving the single-connection serialization
  invariant this codebase's `_write_transaction()` already provides, not a
  true multi-writer stress test.

## Non-goals for this slice

No HTTP route, no auth, no real Discord executor, no `/fetch admin`, no
`#mod-room`. Those remain slice 3 (transport) and slice 4 (Discord adapter +
admin commands + Zariya client wiring on the Krubit side).
