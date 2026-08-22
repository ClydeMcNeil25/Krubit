# Moderation HTTP Transport Design (Phase 5+6 reconciliation, slice 3 of 4)

**Status:** Approved
**Scope:** One authenticated HTTP route wrapping `ModerationService` (slice 2),
wired into Krubit's already-running `CallbackServer`. No real Discord
executor, no `/fetch admin` commands, no `#mod-room` posting — those remain
slice 4.

## Background

Slice 1 built the typed contract, slice 2 built the storage-backed
`ModerationService` implementing all 11 Zariya-facing operations plus one
internal admin-reserved operation. KAI-Studio (Zariya's side, built
independently in Codex under `src/kshq_next/adapters/krubit_moderation/`)
has aligned to the locked contract and is waiting on a real transport before
it can compose against Krubit for anything beyond local type-checking.

Prior conversation with KAI-Studio settled several open questions from the
original transport handoff:

- Transport: HTTP, reusing the existing `aiohttp`-based `CallbackServer`
  (`krubit.web.callbacks`), not a second server process. Confirmed still
  correct — `src/krubit/__main__.py` genuinely starts this server in
  production (`callback_server.start()`), contradicting an earlier,
  outdated Phase 2 operations note that claimed it was never started.
- Route shape: one multiplexed `POST` route, not one per operation.
- Auth: HMAC-SHA256 signature per request, reusing the exact
  verify-before-ingest pattern `build_signed_webhook_route` already
  establishes in `web/callbacks.py`.
- Test fixture: a test-mode flag on the *real* route (not a separate mock
  endpoint), with strict fail-closed safeguards — see below.

## 1. Route — new module `web/moderation_adapter.py`

```python
def build_moderation_adapter_route(
    *,
    path: str,
    header_name: str,
    secret: str,
    service: ModerationService,
    test_secret: str | None,
    test_service_factory: Callable[[], Awaitable[ModerationService]] | None,
) -> CallbackRoute:
```

A single `POST` route. Request body: `{"operation": "<name>", "schema_version":
"krubit.moderation-contract.v1", ...contract-request-fields}`. The handler:

1. Reads the raw body (already enforced by `_read_body_then_handle`'s 1 MiB
   cap before this runs, matching every other route in the file).
2. Determines test mode (see §2) and picks the real or test-mode
   `ModerationService` accordingly.
3. Verifies the HMAC-SHA256 signature over the raw body against the header
   named `header_name`, using `secret` (or `test_secret` in test mode) — a
   failed or missing signature returns `403` immediately, before any body
   parsing, matching `build_signed_webhook_route`'s existing
   verify-before-ingest order.
4. Parses the body as JSON, validates `schema_version` equals
   `MODERATION_CONTRACT_SCHEMA` (`400` on mismatch), and dispatches
   `operation` to the matching `ModerationService` method via a fixed
   dispatch table of the 11 Zariya-facing operation names (never
   `record_human_decision` — that name is not in the table at all, so it is
   structurally unreachable through this route, matching slice 2's own
   "internal, not part of the 11-operation Zariya contract" boundary).
5. `from_dict`-parses the remaining body fields into the operation's
   contract request/query dataclass, calls the matching `ModerationService`
   method, and returns `to_dict()` as JSON with `200`.
6. Every response (success or handled error) adds `correlation_id` (a fresh
   `uuid4().hex` generated once per request) to the JSON body.

## 2. Test mode — real route, isolated backing, fails closed

Honored only when **both** conditions hold:

- Server-side: `KRUBIT_MODERATION_TEST_MODE=1` (new env var, default off,
  parsed the same way `KRUBIT_WATCHDOG_ENABLED` and similar flags already
  are in `config.py`). This variable must never be set in real deployment
  configuration — it exists only for KAI-Studio's own integration-test
  environment.
- Request-side: a header, `X-Krubit-Test-Mode: 1`.

Missing either signal routes through the real, signed, production path —
never a silent downgrade from missing one signal. When both are present:

- The handler builds a fresh `ModerationService` per request (via
  `test_service_factory`), backed by a throwaway temp-file SQLite database
  (`tempfile.mkstemp` + `SQLiteStore.open` + `initialize()`), never the
  production database.
- The injected `ActionExecutor` in test mode always returns a canned,
  deterministic `ActionReceipt` and never performs any real work — this is
  structurally guaranteed in this slice regardless, since no real Discord
  executor exists yet, but the test-mode factory makes it explicit rather
  than incidental so it stays true once slice 4 adds a real one.
- Signature verification still runs, against `test_secret` (a separate,
  independently configured secret from the production one) — test mode does
  not skip authentication, it only swaps the backing store and executor.

**Determinism for KAI-Studio's assertions:** `ModerationService`'s
constructor (from slice 2) gains two new optional parameters:

```python
def __init__(
    self,
    store: SQLiteStore,
    executor: ActionExecutor,
    *,
    id_factory: Callable[[], str] = lambda: f"case:{uuid.uuid4().hex}",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
```

Production wiring uses the defaults (unchanged behavior from slice 2). Test
mode injects a deterministic `id_factory` (e.g. an incrementing counter
formatted as `case:test-{n}`) and a fixed or advancing `clock`, so KAI-Studio's
integration tests get reproducible `case_id`/`receipt_id`/timestamp values
without needing to ignore generated fields in assertions. Every internal
`case_id`/`receipt_id` generation call site in `services/moderation.py`
(`_new_case_id`, the two `receipt:{uuid4}` sites added in slice 2's final
review fix, and any `datetime.now(UTC)` call for `close_timestamp`) is
updated to go through `self._id_factory`/`self._clock` instead of calling
`uuid.uuid4()`/`datetime.now(UTC)` directly.

## 3. Error mapping

| Condition | Status |
|---|---|
| Missing/invalid HMAC signature | `403` |
| `schema_version` mismatch or missing | `400` |
| Unknown `operation` value, or `ModerationContractError` from `from_dict` | `400` |
| `UnknownIncidentError`, or an operation's own "unknown case_id"/"no case found"/"no receipt found" `ValueError` | `404` |
| `IllegalTransitionError` | `409` |
| Any other unhandled exception | `500` (existing `_redacted_errors_middleware`, unchanged) |

Every mapped response (`400`/`403`/`404`/`409`) is JSON:
`{"error": "<short machine-readable reason>", "correlation_id": "<hex>"}`,
extending the same `correlation_id` guarantee the existing middleware
already provides for `500`s to every handled error path too. The route
handler logs each mapped error the same redacted way the middleware logs
`500`s (exception type name and correlation id only, never the exception
string or request body, which could carry a signed-but-malformed payload).

Distinguishing "unknown case_id" (`404`) from "wrong tenant" (also `404`,
not `403`) is deliberate — matching the existing `services/moderation.py`
cross-tenant checks, which already raise a plain `ValueError` for both
so a caller cannot distinguish "doesn't exist" from "exists in another
guild" (avoiding confirming existence across tenants).

## 4. Health

Reuse the existing `build_health_route()` unchanged — liveness only, no
downstream check, matching this codebase's established health-check
convention (see its docstring in `web/callbacks.py`). No moderation-specific
readiness endpoint in this slice.

## 5. Wiring — `web/wiring.py`

`build_callback_routes` gains one new conditionally-included route, added
only when `settings.moderation_adapter_secret` is set (mirroring the exact
gating pattern already used for the Meta/TikTok OAuth routes in that
function). Two new `Settings` fields in `config.py`:

- `KRUBIT_MODERATION_ADAPTER_SECRET` — required for the route to be added at
  all; absent means the route simply isn't registered (same "flag absent =
  feature off" pattern as every other optional integration in this file).
- `KRUBIT_MODERATION_TEST_MODE` — boolean, default `false`, parsed via the
  existing `_parse_bool` helper already used for `KRUBIT_WATCHDOG_ENABLED`
  and siblings.
- `KRUBIT_MODERATION_TEST_MODE_SECRET` — required only when
  `KRUBIT_MODERATION_TEST_MODE` is true; the separate signing secret for
  test-mode requests.

## Testing

`tests/test_moderation_adapter.py`, using `aiohttp.test_utils` (check the
existing `tests/test_*_callback*.py` or similar files for this codebase's
established aiohttp-route test convention before writing new ones) against a
real `CallbackServer`/`build_moderation_adapter_route` instance, never
mocked:

- Valid signed request for each of the 11 operations round-trips correctly.
- Missing/invalid signature → `403`.
- Missing/mismatched `schema_version` → `400`.
- Unknown `operation` value → `400`.
- Each error-mapped exception type from `ModerationService` → its mapped
  status code, each carrying a `correlation_id`.
- `record_human_decision` is structurally unreachable: no `operation` value
  routes to it (a dispatch-table completeness test asserting exactly the 11
  contract operation names are present, nothing else).
- Test mode: request with the test header but server flag off → falls
  through to production path (and fails signature verification against the
  production secret, proving no silent bypass). Server flag on but header
  absent → same. Both present → isolated temp DB used (assert two
  consecutive test-mode requests against different `ModerationService`
  instances, e.g. by checking the temp DB file differs), deterministic
  `case_id`/`receipt_id` values assert exact equality (not just
  well-formedness) across repeated runs.
- Full lifecycle exercised through the HTTP layer at least once
  (`record_incident` → `submit_action_recommendation` →
  `request_human_approval` → ... → `close_incident`), proving the dispatch
  table and `from_dict`/`to_dict` round-trip hold end to end, not just
  per-operation in isolation.

## Non-goals for this slice

No real Discord executor. Production wiring in this slice constructs
`ModerationService` with a `NoOpActionExecutor` — a small `ActionExecutor`
implementation, added in `web/wiring.py` alongside the route wiring, whose
`execute()` always returns `ActionReceipt(succeeded=False, detail="Discord
execution not yet implemented (slice 4)")`. This means `execute_approved_action`
is reachable and safely testable end-to-end through the real HTTP route in
this slice, but every real-world call to it always lands in
`EXECUTION_FAILED` with an honest reason, never a silent no-op success — a
real Discord executor replacing `NoOpActionExecutor` is slice 4's job. No
`/fetch admin` commands. No `#mod-room` posting. No moderation-specific
readiness endpoint beyond the existing generic health route.
