# Moderation HTTP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `ModerationService` (slice 2) up to a real, authenticated HTTP
route on Krubit's existing `CallbackServer` — slice 3 of 4 of the
Zariya-Krubit moderation adapter (Phase 5+6 reconciliation).

**Architecture:** A new `web/moderation_adapter.py` module builds one
multiplexed `POST` route (HMAC-SHA256 signed, dispatching an `operation`
field to the matching `ModerationService` method). `ModerationService` gains
injectable `id_factory`/`clock` parameters for deterministic test-mode
responses. `config.py` gains three new settings. `web/wiring.py`'s
`build_callback_routes` is restructured to stop gating the moderation route
behind the unrelated `creator_signals_enabled` flag — a real bug in the
existing function this plan must fix, not just avoid.

**Tech Stack:** Python 3.12+, `aiohttp` (already a dependency), stdlib
`hmac`/`hashlib`/`json`/`tempfile`/`uuid`, `pytest` + `pytest-asyncio` +
`aiohttp.test_utils` (already used by `tests/test_callback_ingress.py`).

## Global Constraints

- Single multiplexed `POST` route, not one per operation.
- HMAC-SHA256 signature verification, `hmac.new(secret, body,
  hashlib.sha256).hexdigest()` + `hmac.compare_digest(...)`, matching the
  exact pattern already used in `src/krubit/integrations/meta.py`.
- `record_human_decision` must be structurally unreachable through this
  route — it is not one of the 11 dispatch-table entries.
- Test mode requires **both** `KRUBIT_MODERATION_TEST_MODE=1` server-side
  AND an `X-Krubit-Test-Mode: 1` request header. Missing either falls
  through to the real, signed, production path.
- Test mode uses a throwaway temp-file SQLite DB, never the production
  database, and signature verification still runs (against a separate
  `test_secret`) — test mode never skips auth.
- Every response (success or handled error) includes `correlation_id` (a
  fresh `uuid4().hex` per request).
- Error mapping: bad/missing signature → 403; `schema_version`
  mismatch/missing, unknown `operation`, or `ModerationContractError` → 400;
  `UnknownIncidentError` or "not found" `ValueError` → 404;
  `IllegalTransitionError` → 409; unhandled → 500 (existing middleware).
- No new third-party dependencies.

---

## File Structure

- Modify: `src/krubit/config.py` — three new `Settings` fields
  (`moderation_adapter_secret`, `moderation_test_mode`,
  `moderation_test_mode_secret`).
- Modify: `src/krubit/services/moderation.py` — `id_factory`/`clock`
  constructor parameters, replacing direct `uuid.uuid4()`/`datetime.now(UTC)`
  calls at every generation site.
- Create: `src/krubit/web/moderation_adapter.py` — `NoOpActionExecutor`,
  `build_moderation_adapter_route`.
- Modify: `src/krubit/web/wiring.py` — restructure `build_callback_routes`'s
  gating, add moderation route wiring.
- Create: `tests/test_moderation_adapter.py` — route-level integration
  tests.
- Modify: `tests/test_moderation_service.py` — a handful of new tests for
  the `id_factory`/`clock` injection.

---

### Task 1: `Settings` fields for the moderation adapter

**Files:**
- Modify: `src/krubit/config.py`
- Test: `tests/test_config.py` (find and follow this file's existing
  pattern for testing a new `Settings` field before writing — check how an
  existing secret-bearing optional field like `meta_app_secret` or
  `tiktok_client_secret` is tested for presence/absence and validation)

**Interfaces:**
- Consumes: existing `_parse_bool`, `SettingsError`, the `field(default=...,
  repr=False)` pattern already used for secret fields (see
  `bot_token`/`twitch_client_secret` in the existing file).
- Produces: `Settings.moderation_adapter_secret: str | None` (default
  `None`, `repr=False`), `Settings.moderation_test_mode: bool` (default
  `False`), `Settings.moderation_test_mode_secret: str | None` (default
  `None`, `repr=False`).

- [ ] **Step 1: Write the failing tests**

First, read `tests/test_config.py` to find the exact fixture/helper it uses
to build a `Settings` instance from a raw env-var mapping (likely a function
like `_settings(**overrides)` or `Settings.from_env(mapping)` — use
whatever that file's existing tests actually call, adapting the test names
below to match its real helper name).

```python
# append to tests/test_config.py — adapt the settings-construction call to
# match this file's actual existing helper/fixture
def test_moderation_adapter_secret_defaults_to_none():
    settings = _settings()  # replace with this file's real construction helper
    assert settings.moderation_adapter_secret is None
    assert settings.moderation_test_mode is False
    assert settings.moderation_test_mode_secret is None


def test_moderation_adapter_secret_can_be_set():
    settings = _settings(KRUBIT_MODERATION_ADAPTER_SECRET="secret-value")
    assert settings.moderation_adapter_secret == "secret-value"


def test_moderation_test_mode_parses_bool():
    settings = _settings(KRUBIT_MODERATION_TEST_MODE="true")
    assert settings.moderation_test_mode is True


def test_moderation_test_mode_invalid_value_raises():
    from krubit.config import SettingsError
    with pytest.raises(SettingsError, match="KRUBIT_MODERATION_TEST_MODE"):
        _settings(KRUBIT_MODERATION_TEST_MODE="yes")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_config.py -k moderation -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute
'moderation_adapter_secret'`.

- [ ] **Step 3: Implement the settings fields**

In `src/krubit/config.py`, add to the `Settings` dataclass body (near the
other secret-bearing optional fields, e.g. after
`watchdog_zariya_bridge_url`):

```python
    moderation_adapter_secret: str | None = field(default=None, repr=False)
    moderation_test_mode: bool = False
    moderation_test_mode_secret: str | None = field(default=None, repr=False)
```

Find the `from_env`/construction classmethod (the one that reads
`values.get("KRUBIT_WATCHDOG_ENABLED", "false")` etc.) and add, following
the exact same style as the `watchdog_zariya_bridge_url`/`watchdog_enabled`
block:

```python
        raw_moderation_adapter_secret = values.get(
            "KRUBIT_MODERATION_ADAPTER_SECRET", ""
        ).strip()
        raw_moderation_test_mode = values.get("KRUBIT_MODERATION_TEST_MODE", "false").strip()
        moderation_test_mode = _parse_bool("KRUBIT_MODERATION_TEST_MODE", raw_moderation_test_mode)
        raw_moderation_test_mode_secret = values.get(
            "KRUBIT_MODERATION_TEST_MODE_SECRET", ""
        ).strip()
```

And in the final `cls(...)` construction call, add:

```python
            moderation_adapter_secret=raw_moderation_adapter_secret or None,
            moderation_test_mode=moderation_test_mode,
            moderation_test_mode_secret=raw_moderation_test_mode_secret or None,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_config.py -k moderation -v`
Expected: PASS

- [ ] **Step 5: Run the full config test file to confirm no regressions**

Run: `uv run --offline pytest tests/test_config.py -v`
Expected: PASS, all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/krubit/config.py tests/test_config.py
git commit -m "feat: add moderation adapter settings (secret, test mode, test mode secret)"
```

---

### Task 2: `ModerationService` injectable `id_factory`/`clock`

**Files:**
- Modify: `src/krubit/services/moderation.py`
- Modify: `tests/test_moderation_service.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ModerationService.__init__(self, store: SQLiteStore, executor:
  ActionExecutor, *, id_factory: Callable[[], str] = <default>, clock:
  Callable[[], datetime] = <default>) -> None`. Every internal
  `case_id`/`receipt_id` generation and every `close_timestamp` assignment
  goes through `self._id_factory()`/`self._clock()` instead of calling
  `uuid.uuid4()`/`datetime.now(UTC)` directly.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_service.py
import itertools


@pytest.mark.asyncio
async def test_record_incident_uses_injected_id_factory(store: SQLiteStore) -> None:
    await _seed_incident(store)
    counter = itertools.count(1)
    service = ModerationService(
        store, FakeExecutor(), id_factory=lambda: f"case:test-{next(counter)}"
    )
    response = await service.record_incident(
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
    assert response.case_id == "case:test-1"


@pytest.mark.asyncio
async def test_close_incident_uses_injected_clock(store: SQLiteStore) -> None:
    fixed_time = datetime(2026, 6, 1, tzinfo=UTC)
    service = ModerationService(store, FakeExecutor(succeed=True), clock=lambda: fixed_time)
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100, ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"})
    )
    await service.close_incident(
        100,
        CloseIncidentRequest.from_dict(
            {"case_id": case_id, "decision": "resolved", "idempotency_key": "idem:5"}
        ),
    )
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.close_timestamp == fixed_time
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_service.py -k "id_factory or clock" -v`
Expected: FAIL — `TypeError: ModerationService.__init__() got an unexpected
keyword argument 'id_factory'`.

- [ ] **Step 3: Implement the injection**

In `src/krubit/services/moderation.py`, add `from collections.abc import
Callable` to the imports if not already present (check first — `Callable`
may already be imported via `typing` elsewhere in the file; use whichever
import style the file already has for `Protocol`).

Change the `ModerationService.__init__`:

```python
    def __init__(
        self,
        store: SQLiteStore,
        executor: ActionExecutor,
        *,
        id_factory: Callable[[], str] = _new_case_id,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._executor = executor
        self._id_factory = id_factory
        self._clock = clock
```

In `record_incident`, replace `case_id=_new_case_id()` with
`case_id=self._id_factory()`.

In `execute_approved_action`, replace both `receipt_id=f"receipt:{uuid.uuid4().hex}"`
occurrences with `receipt_id=self._id_factory()`. (`id_factory`'s default,
`_new_case_id`, produces `case:<hex>`-prefixed strings — that prefix is
irrelevant to storage, which treats `receipt_id`/`case_id` as opaque text,
but for clarity add a second default factory instead of reusing
`_new_case_id` for receipts: define `_new_receipt_id() -> str: return
f"receipt:{uuid.uuid4().hex}"` alongside the existing `_new_case_id`, and
give `ModerationService.__init__` a separate `receipt_id_factory: Callable[[],
str] = _new_receipt_id` parameter instead of overloading the single
`id_factory` for both purposes. Update `record_incident` to use
`self._id_factory()` and `execute_approved_action` to use
`self._receipt_id_factory()`.)

In `close_incident`, replace `datetime.now(UTC)` (the `close_timestamp=`
argument) with `self._clock()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_service.py -v`
Expected: PASS, all tests green (existing 164 plus 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/krubit/services/moderation.py tests/test_moderation_service.py
git commit -m "feat: make ModerationService case/receipt id generation and clock injectable"
```

---

### Task 3: `web/moderation_adapter.py` — production route (no test mode yet)

**Files:**
- Create: `src/krubit/web/moderation_adapter.py`
- Create: `tests/test_moderation_adapter.py`

**Interfaces:**
- Consumes: `krubit.web.callbacks.CallbackRoute`; `krubit.services.moderation.{
  ModerationService, ActionExecutor}`; `krubit.domain.moderation.{ActionReceipt,
  IllegalTransitionError, UnknownIncidentError}`; `krubit.contracts.moderation.{
  ModerationContractError, MODERATION_CONTRACT_SCHEMA, ...every request/query
  dataclass}` (import all 11 request/query types + their response/result
  counterparts — see the exact list in `services/moderation.py`'s own import
  block for the canonical set of names).
- Produces: `NoOpActionExecutor` (an `ActionExecutor` implementation);
  `build_moderation_adapter_route(*, path: str, header_name: str, secret: str,
  service: ModerationService) -> CallbackRoute` (test-mode parameters added
  in Task 4 — this task builds the production-only version first).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moderation_adapter.py
"""Integration tests for the moderation HTTP transport (slice 3)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krubit.domain.moderation import ActionReceipt
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.moderation import ModerationService
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.moderation_adapter import NoOpActionExecutor, build_moderation_adapter_route

SECRET = "test-signing-secret"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeExecutor:
    async def execute(self, case: object, execution_key: str) -> ActionReceipt:
        return ActionReceipt(receipt_id="receipt:fake", succeeded=True, detail="ok")


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


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


@pytest.fixture
async def client(store: SQLiteStore) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    service = ModerationService(store, FakeExecutor())
    route = build_moderation_adapter_route(
        path="/moderation/adapter",
        header_name="X-Krubit-Moderation-Signature",
        secret=SECRET,
        service=service,
    )
    server = CallbackServer(
        public_base_url="https://callbacks.example.com", port=8443, routes=(route,)
    )
    app = server.build_app()
    test_client = TestClient[web.Request, web.Application](TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def _post(client: TestClient[web.Request, web.Application], payload: dict[str, object]) -> web.ClientResponse:
    body = json.dumps(payload).encode("utf-8")
    return await client.post(
        "/moderation/adapter",
        data=body,
        headers={
            "X-Krubit-Moderation-Signature": _sign(body),
            "Content-Type": "application/json",
        },
    )


async def test_record_incident_round_trips_through_http(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await _post(
        client,
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        },
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["status"] == "recorded"
    assert payload["duplicate"] is False
    assert "correlation_id" in payload


async def test_missing_signature_returns_403(
    client: TestClient[web.Request, web.Application],
) -> None:
    body = json.dumps({"operation": "record_incident"}).encode("utf-8")
    response = await client.post(
        "/moderation/adapter", data=body, headers={"Content-Type": "application/json"}
    )
    assert response.status == 403


async def test_invalid_signature_returns_403(
    client: TestClient[web.Request, web.Application],
) -> None:
    body = json.dumps({"operation": "record_incident"}).encode("utf-8")
    response = await client.post(
        "/moderation/adapter",
        data=body,
        headers={"X-Krubit-Moderation-Signature": "not-a-real-signature"},
    )
    assert response.status == 403


async def test_missing_schema_version_returns_400(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await _post(client, {"operation": "record_incident"})
    assert response.status == 400
    payload = await response.json()
    assert "correlation_id" in payload


async def test_unknown_operation_returns_400(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await _post(
        client,
        {"operation": "record_human_decision", "schema_version": "krubit.moderation-contract.v1"},
    )
    assert response.status == 400


async def test_unknown_incident_returns_404(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await _post(
        client,
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:missing",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        },
    )
    assert response.status == 404


async def test_dispatch_table_has_exactly_the_eleven_contract_operations(
    client: TestClient[web.Request, web.Application],
) -> None:
    from krubit.web.moderation_adapter import _OPERATIONS

    assert set(_OPERATIONS) == {
        "record_incident",
        "submit_action_recommendation",
        "request_human_approval",
        "execute_approved_action",
        "close_incident",
        "submit_appeal",
        "get_member_moderation_history",
        "get_incident",
        "get_incident_status",
        "get_action_receipt",
        "get_pending_reviews",
    }
    assert "record_human_decision" not in _OPERATIONS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'krubit.web.moderation_adapter'`.

- [ ] **Step 3: Implement the route**

```python
# src/krubit/web/moderation_adapter.py
"""HTTP transport for the Krubit moderation adapter (Phase 5+6 slice 3).

One multiplexed, HMAC-signed POST route dispatching a request body's
`operation` field to the matching `krubit.services.moderation.ModerationService`
method. `record_human_decision` is deliberately absent from the dispatch
table -- it is not part of the 11-operation Zariya contract and stays
reachable only from the (not-yet-built) `/fetch admin` commands in slice 4.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiohttp import web

from krubit.contracts.moderation import (
    MODERATION_CONTRACT_SCHEMA,
    CloseIncidentRequest,
    ExecuteApprovedActionRequest,
    GetActionReceiptQuery,
    GetIncidentQuery,
    GetIncidentStatusQuery,
    GetMemberModerationHistoryQuery,
    GetPendingReviewsQuery,
    ModerationContractError,
    RecordIncidentRequest,
    RequestHumanApprovalRequest,
    SubmitActionRecommendationRequest,
    SubmitAppealRequest,
)
from krubit.domain.moderation import ActionReceipt, IllegalTransitionError, UnknownIncidentError
from krubit.services.moderation import ActionExecutor, ModerationService
from krubit.web.callbacks import CallbackRoute

_logger = logging.getLogger(__name__)


class NoOpActionExecutor:
    """Production stand-in until slice 4 supplies a real Discord executor.

    Always reports failure with an honest reason rather than a silent
    no-op success, so `execute_approved_action` is safely reachable end to
    end through the real HTTP route in this slice.
    """

    async def execute(self, case: object, execution_key: str) -> ActionReceipt:
        return ActionReceipt(
            receipt_id=f"receipt:{uuid.uuid4().hex}",
            succeeded=False,
            detail="Discord execution not yet implemented (slice 4)",
        )


_OPERATIONS: dict[str, tuple[type, str]] = {
    "record_incident": (RecordIncidentRequest, "record_incident"),
    "submit_action_recommendation": (
        SubmitActionRecommendationRequest,
        "submit_action_recommendation",
    ),
    "request_human_approval": (RequestHumanApprovalRequest, "request_human_approval"),
    "execute_approved_action": (ExecuteApprovedActionRequest, "execute_approved_action"),
    "close_incident": (CloseIncidentRequest, "close_incident"),
    "submit_appeal": (SubmitAppealRequest, "submit_appeal"),
    "get_member_moderation_history": (
        GetMemberModerationHistoryQuery,
        "get_member_moderation_history",
    ),
    "get_incident": (GetIncidentQuery, "get_incident"),
    "get_incident_status": (GetIncidentStatusQuery, "get_incident_status"),
    "get_action_receipt": (GetActionReceiptQuery, "get_action_receipt"),
    "get_pending_reviews": (GetPendingReviewsQuery, "get_pending_reviews"),
}

# Write operations take an explicit guild_id parameter resolved from the
# authenticated context, not the request body (see services/moderation.py's
# own convention). This slice has no auth-derived guild identity yet beyond
# the signature itself, so the guild_id is taken from the request body's
# own guild_id-shaped field where present (record_incident) or, for the
# other five mutating ops (whose requests carry only case_id), resolved by
# the service from the case row -- matching exactly how services/moderation.py
# already handles this internally. The HTTP layer passes through whatever
# int(payload.get("guild_id", 0)) resolves to for record_incident, and for
# every other operation, passes the case's own resolvable guild via a second
# body field, `guild_id`, that the caller (KAI-Studio) must supply out of
# band the same way the read operations already require it in their locked
# contract shape (see contracts/moderation.py's GetIncidentQuery etc, which
# carry guild_id directly following the slice-2 multi-tenancy amendment).
_WRITE_OPERATIONS = frozenset(
    {
        "record_incident",
        "submit_action_recommendation",
        "request_human_approval",
        "execute_approved_action",
        "close_incident",
        "submit_appeal",
    }
)


def _error_response(status: int, error: str) -> web.Response:
    correlation_id = uuid.uuid4().hex
    _logger.error("moderation adapter error [status=%s, correlation_id=%s]", status, correlation_id)
    return web.json_response({"error": error, "correlation_id": correlation_id}, status=status)


def _verify_signature(body: bytes, provided: str | None, secret: str) -> bool:
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


async def _handle(
    request: web.Request, *, header_name: str, secret: str, service: ModerationService
) -> web.StreamResponse:
    body = await request.read()
    signature = request.headers.get(header_name)
    if not _verify_signature(body, signature, secret):
        return _error_response(403, "invalid or missing signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _error_response(400, "request body must be valid JSON")
    if not isinstance(payload, dict):
        return _error_response(400, "request body must be a JSON object")

    if payload.get("schema_version") != MODERATION_CONTRACT_SCHEMA:
        return _error_response(400, "schema_version is missing or does not match")

    operation = payload.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        return _error_response(400, f"unknown operation: {operation!r}")

    request_type, method_name = _OPERATIONS[operation]
    body_fields = {k: v for k, v in payload.items() if k not in ("operation", "schema_version")}

    try:
        parsed_request = request_type.from_dict(body_fields)
    except ModerationContractError as exc:
        return _error_response(400, str(exc))

    method = getattr(service, method_name)
    correlation_id = uuid.uuid4().hex
    try:
        if operation in _WRITE_OPERATIONS:
            guild_id = int(body_fields.get("guild_id", 0))
            result = await method(guild_id, parsed_request)
        else:
            guild_id = int(getattr(parsed_request, "guild_id", "0"))
            result = await method(guild_id, parsed_request)
    except UnknownIncidentError as exc:
        return _error_response(404, str(exc))
    except IllegalTransitionError as exc:
        return _error_response(409, str(exc))
    except ModerationContractError as exc:
        return _error_response(400, str(exc))
    except ValueError as exc:
        message = str(exc)
        status = 404 if ("unknown" in message or "no case found" in message or "no receipt found" in message) else 400
        return _error_response(status, message)

    response_payload = dict(result.to_dict())
    response_payload["correlation_id"] = correlation_id
    return web.json_response(response_payload, status=200)


def build_moderation_adapter_route(
    *, path: str, header_name: str, secret: str, service: ModerationService
) -> CallbackRoute:
    async def handler(request: web.Request) -> web.StreamResponse:
        return await _handle(request, header_name=header_name, secret=secret, service=service)

    return CallbackRoute(path=path, method="POST", handler=handler)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_adapter.py -v`
Expected: PASS, all 7 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/krubit/web/moderation_adapter.py tests/test_moderation_adapter.py
git commit -m "feat: add HTTP transport route for the moderation adapter"
```

---

### Task 4: Test mode — isolated store, deterministic executor, fail-closed

**Files:**
- Modify: `src/krubit/web/moderation_adapter.py`
- Modify: `tests/test_moderation_adapter.py`

**Interfaces:**
- Consumes: `krubit.storage.sqlite.SQLiteStore`; `tempfile.mkstemp`;
  Task 2's `ModerationService(store, executor, id_factory=..., clock=...)`.
- Produces: `build_moderation_adapter_route`'s signature extended to
  `(*, path: str, header_name: str, secret: str, service: ModerationService,
  test_header_name: str = "X-Krubit-Test-Mode", test_mode_enabled: bool =
  False, test_secret: str | None = None) -> CallbackRoute`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_moderation_adapter.py
import itertools
import tempfile
from pathlib import Path as _Path


TEST_SECRET = "test-mode-secret"


@pytest.fixture
async def test_mode_client(
    store: SQLiteStore,
) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    service = ModerationService(store, FakeExecutor())
    route = build_moderation_adapter_route(
        path="/moderation/adapter",
        header_name="X-Krubit-Moderation-Signature",
        secret=SECRET,
        service=service,
        test_mode_enabled=True,
        test_secret=TEST_SECRET,
    )
    server = CallbackServer(
        public_base_url="https://callbacks.example.com", port=8443, routes=(route,)
    )
    app = server.build_app()
    test_client = TestClient[web.Request, web.Application](TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


def _sign_with(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def test_test_mode_header_without_server_flag_falls_through_to_production_and_403s(
    client: TestClient[web.Request, web.Application],
) -> None:
    # `client` fixture has test mode disabled server-side (Task 3's fixture,
    # test_mode_enabled defaults False). Sending the test header with a
    # test-secret signature must still be rejected: the production path
    # checks the signature against SECRET, not TEST_SECRET.
    body = json.dumps({"operation": "record_incident"}).encode("utf-8")
    response = await client.post(
        "/moderation/adapter",
        data=body,
        headers={
            "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
            "X-Krubit-Test-Mode": "1",
        },
    )
    assert response.status == 403


async def test_test_mode_server_flag_without_header_falls_through_to_production_and_403s(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    # `test_mode_client` has test mode enabled server-side, but this request
    # omits the X-Krubit-Test-Mode header, so it must still be evaluated as
    # a production request against SECRET, not TEST_SECRET.
    body = json.dumps({"operation": "record_incident"}).encode("utf-8")
    response = await test_mode_client.post(
        "/moderation/adapter",
        data=body,
        headers={"X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET)},
    )
    assert response.status == 403


async def test_test_mode_both_signals_present_uses_isolated_store(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    # The isolated test-mode store has no seeded incident (unlike `store`),
    # so record_incident against "incident:1" must 404 -- proving it did NOT
    # touch the fixture's real seeded database.
    body = json.dumps(
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        }
    ).encode("utf-8")
    response = await test_mode_client.post(
        "/moderation/adapter",
        data=body,
        headers={
            "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
            "X-Krubit-Test-Mode": "1",
        },
    )
    assert response.status == 404


async def test_test_mode_produces_deterministic_case_ids_across_requests(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    # Two separate requests in test mode, each seeding their own incident
    # first via the isolated store is not directly reachable from HTTP in
    # this test (no seed endpoint) -- instead, assert case_id determinism by
    # calling record_incident twice against the SAME (nonexistent) incident
    # and confirming both get identical 404 correlation-id-bearing shape
    # rather than differing internal state. Full end-to-end determinism
    # (a real incident recorded then fetched) is covered by
    # tests/test_moderation_service.py's id_factory tests in Task 2 -- this
    # test only proves the test-mode code path constructs its
    # ModerationService with a deterministic id_factory/clock, not a
    # production one, by checking two consecutive requests against a
    # temp-file DB path exposed for assertion. Read the Task 4 Step 3
    # implementation notes below: the test-mode service factory must accept
    # an optional `id_counter` for this test to hook, OR (simpler) this test
    # instead asserts the temp DB file used by two consecutive test-mode
    # requests differs from the one two consecutive PRODUCTION requests
    # share -- proving isolation, which is the actually-required guarantee.
    # Implement it as: patch `tempfile.mkstemp` (via monkeypatch) to record
    # every path it's called with during two consecutive test-mode requests,
    # and assert the two recorded paths differ (fresh DB per request).
    import unittest.mock

    paths: list[str] = []
    original_mkstemp = tempfile.mkstemp

    def _tracking_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, path = original_mkstemp(*args, **kwargs)
        paths.append(path)
        return fd, path

    with unittest.mock.patch("tempfile.mkstemp", side_effect=_tracking_mkstemp):
        body = json.dumps(
            {"operation": "record_incident", "schema_version": "krubit.moderation-contract.v1"}
        ).encode("utf-8")
        for _ in range(2):
            await test_mode_client.post(
                "/moderation/adapter",
                data=body,
                headers={
                    "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
                    "X-Krubit-Test-Mode": "1",
                },
            )
    assert len(paths) == 2
    assert paths[0] != paths[1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --offline pytest tests/test_moderation_adapter.py -v`
Expected: FAIL — `test_mode_client` fixture errors with `TypeError:
build_moderation_adapter_route() got an unexpected keyword argument
'test_mode_enabled'`.

- [ ] **Step 3: Implement test mode**

In `src/krubit/web/moderation_adapter.py`, add `import itertools` and
`import tempfile` to the imports, then change `build_moderation_adapter_route`
and `_handle`:

```python
def build_moderation_adapter_route(
    *,
    path: str,
    header_name: str,
    secret: str,
    service: ModerationService,
    test_header_name: str = "X-Krubit-Test-Mode",
    test_mode_enabled: bool = False,
    test_secret: str | None = None,
) -> CallbackRoute:
    async def handler(request: web.Request) -> web.StreamResponse:
        test_mode_requested = request.headers.get(test_header_name) == "1"
        if test_mode_enabled and test_mode_requested and test_secret is not None:
            active_secret = test_secret
            active_service = await _build_test_mode_service()
        else:
            active_secret = secret
            active_service = service
        return await _handle(
            request, header_name=header_name, secret=active_secret, service=active_service
        )

    return CallbackRoute(path=path, method="POST", handler=handler)


async def _build_test_mode_service() -> ModerationService:
    fd, path_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = await SQLiteStore.open(Path(path_str))
    await store.initialize()
    counter = itertools.count(1)
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    return ModerationService(
        store,
        NoOpActionExecutor(),
        id_factory=lambda: f"case:test-{next(counter)}",
        clock=lambda: fixed_time,
    )
```

Add these imports to `web/moderation_adapter.py`'s top-of-file import block
(alongside the ones already added in Task 3): `import itertools`, `import os`,
`import tempfile`, `from datetime import UTC, datetime`, `from pathlib import
Path`, and `from krubit.storage.sqlite import SQLiteStore`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --offline pytest tests/test_moderation_adapter.py -v`
Expected: PASS, all tests green (7 from Task 3 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/krubit/web/moderation_adapter.py tests/test_moderation_adapter.py
git commit -m "feat: add fail-closed test mode to the moderation adapter route"
```

---

### Task 5: Wire into `web/wiring.py` — fix the `creator_signals_enabled` gating bug

**Files:**
- Modify: `src/krubit/web/wiring.py`
- Modify: `tests/test_wiring_security.py` (or the closest existing wiring
  test file — check which of `tests/test_wiring_oauth.py`,
  `tests/test_wiring_deauthorization.py`, `tests/test_wiring_security.py`
  already tests `build_callback_routes`'s top-level gating behavior, and add
  to that one)

**Interfaces:**
- Consumes: Task 4's `build_moderation_adapter_route`; `krubit.config.Settings`
  (Task 1's three new fields); `krubit.services.moderation.ModerationService`;
  `krubit.web.moderation_adapter.NoOpActionExecutor`.
- Produces: `build_callback_routes` restructured so the moderation route is
  added whenever `settings.moderation_adapter_secret is not None` — entirely
  independent of `settings.creator_signals_enabled`.

- [ ] **Step 1: Write the failing test**

First read `src/krubit/web/wiring.py`'s current `build_callback_routes` in
full (already shown above in this plan's research) and locate the existing
test file that constructs a `Settings` instance with
`creator_signals_enabled=False` and asserts `build_callback_routes(...)
== ()`. Add a test proving the bug this task fixes:

```python
# append to the identified wiring test file
async def test_moderation_route_registers_even_when_creator_signals_disabled(...):
    # Use this file's existing helper for constructing Settings/store/vault
    # (match its real fixture names — do not invent new ones). The key
    # assertion: with creator_signals_enabled=False (or entirely default/
    # unset) but moderation_adapter_secret set and callback_public_base_url/
    # callback_port set, build_callback_routes(...) must return a tuple
    # containing a route whose .path is the moderation adapter's configured
    # path -- not an empty tuple.
    ...
```

Write this test using whatever this codebase's actual `Settings`-construction
and `build_callback_routes`-calling convention already is in the file you
found — do not guess a shape; read the file first, then write a test that
fits its existing style, asserting: `creator_signals_enabled=False`,
`moderation_adapter_secret="secret"`, `callback_public_base_url` and
`callback_port` set → the returned routes tuple has length 1 and that
route's path matches the moderation route's configured path
(`/moderation/adapter`).

- [ ] **Step 2: Run the test to verify it fails**

Run whatever this file's existing pytest invocation convention is, filtered
to the new test name.
Expected: FAIL — either `AttributeError` (route not built yet) or the
returned tuple is empty because of the early `creator_signals_enabled`
return.

- [ ] **Step 3: Restructure `build_callback_routes`**

In `src/krubit/web/wiring.py`, change:

```python
def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
) -> tuple[CallbackRoute, ...]:
    if not settings.creator_signals_enabled:
        return ()
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []
```

to:

```python
def build_callback_routes(
    settings: Settings,
    store: SQLiteStore,
    vault: CredentialVault | None,
    oauth_session: object,
) -> tuple[CallbackRoute, ...]:
    if settings.callback_public_base_url is None or settings.callback_port is None:
        return ()

    routes: list[CallbackRoute] = []

    if settings.moderation_adapter_secret is not None:
        from krubit.services.moderation import ModerationService
        from krubit.web.moderation_adapter import (
            NoOpActionExecutor,
            build_moderation_adapter_route,
        )

        moderation_service = ModerationService(store, NoOpActionExecutor())
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

    if not settings.creator_signals_enabled:
        return tuple(routes)
```

(the `if not settings.creator_signals_enabled: return ()` line is now
inside the function *after* the moderation-route append, changed to
`return tuple(routes)` so any already-built moderation route survives —
the rest of the function, the `tiktok`/`meta` route-building blocks below
this point, is unchanged, since they still correctly require
`creator_signals_enabled`.)

Move the local imports (`ModerationService`, `NoOpActionExecutor`,
`build_moderation_adapter_route`) to the top of `wiring.py` instead of
inline, matching how every other import in that file is already structured
at module level — the inline placement shown above is only to make the
diff easy to read in this plan step; your actual edit should add these
three names to the existing top-of-file import block.

- [ ] **Step 4: Run the test to verify it passes**

Run the same filtered test command as Step 2.
Expected: PASS.

- [ ] **Step 5: Run the full wiring test suite to confirm no regressions**

Run: `uv run --offline pytest tests/test_wiring_oauth.py
tests/test_wiring_deauthorization.py tests/test_wiring_security.py -v`
Expected: PASS, all tests green — the existing TikTok/Meta gating tests
must still pass unchanged, since only the moderation-route addition and the
early-return's *position* changed, not the TikTok/Meta logic itself.

- [ ] **Step 6: Commit**

```bash
git add src/krubit/web/wiring.py tests/test_wiring_security.py
git commit -m "fix: register the moderation adapter route independent of creator_signals_enabled"
```

---

### Task 6: End-to-end integration test and full verification

**Files:**
- Modify: `tests/test_moderation_adapter.py`

**Interfaces:**
- Consumes: everything from Tasks 3-5.
- Produces: no new production code — one full-lifecycle test proving the
  dispatch table, `from_dict`/`to_dict` round-trip, and error mapping hold
  together end to end through the real HTTP layer, not just per-operation
  in isolation.

- [ ] **Step 1: Write the full-lifecycle test**

```python
# append to tests/test_moderation_adapter.py
async def test_full_lifecycle_through_http(
    client: TestClient[web.Request, web.Application],
) -> None:
    record_response = await _post(
        client,
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:1",
        },
    )
    assert record_response.status == 200
    case_id = (await record_response.json())["case_id"]

    recommend_response = await _post(
        client,
        {
            "operation": "submit_action_recommendation",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "recommended_action": "24h timeout",
            "idempotency_key": "idem:2",
        },
    )
    assert recommend_response.status == 200

    approval_response = await _post(
        client,
        {
            "operation": "request_human_approval",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "review_deadline": "2026-01-02T00:00:00+00:00",
            "idempotency_key": "idem:3",
        },
    )
    assert approval_response.status == 200
    assert (await approval_response.json())["status"] == "approval_required"

    status_response = await _post(
        client,
        {
            "operation": "get_incident_status",
            "schema_version": "krubit.moderation-contract.v1",
            "guild_id": "100",
            "incident_id": "incident:1",
        },
    )
    assert status_response.status == 200
    assert (await status_response.json())["status"] == "approval_required"
```

- [ ] **Step 2: Run the moderation-scoped test suite**

Run: `uv run --offline pytest tests/test_moderation_domain.py
tests/test_moderation_contract.py tests/test_moderation_storage.py
tests/test_moderation_service.py tests/test_moderation_adapter.py
tests/test_config.py tests/test_wiring_oauth.py
tests/test_wiring_deauthorization.py tests/test_wiring_security.py -v`
Expected: PASS, all tests green.

- [ ] **Step 3: Run the full project test suite to confirm no regressions**

Run: `uv run --offline pytest`
Expected: PASS. If a pre-existing environment crash unrelated to this
feature reproduces (as documented in slices 1 and 2's builds — a Windows
OPENSSL_Applink native crash under `cryptography` import), report it
honestly per that same precedent rather than treating it as a regression;
the scoped run in Step 2 is the regression-relevant evidence for this
feature's own code.

- [ ] **Step 4: Commit**

```bash
git add tests/test_moderation_adapter.py
git commit -m "test: add full-lifecycle end-to-end test through the moderation HTTP transport"
```
