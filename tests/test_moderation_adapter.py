"""Integration tests for the moderation HTTP transport (slice 3)."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest.mock
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krubit.contracts.moderation import RecordIncidentRequest
from krubit.domain.moderation import ActionReceipt, ApprovalDecision
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.moderation import ModerationService
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackServer
from krubit.web.moderation_adapter import build_moderation_adapter_route

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


async def _post(
    client: TestClient[web.Request, web.Application], payload: dict[str, object]
) -> web.ClientResponse:
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


async def test_test_mode_reuses_temp_db_within_a_session_but_not_across_sessions(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    # Test mode is session-scoped (Finding 1): requests sharing an
    # X-Krubit-Test-Session-Id build/reuse ONE temp-file store, so a fresh
    # temp DB is only created the first time a given session id is seen.
    # Two requests under the SAME (unique, test-local) session id must share
    # a single store; a request under a DIFFERENT session id must get its
    # own fresh store. Uses unique session ids rather than the "default"
    # fallback because _TEST_MODE_SESSIONS is a module-level cache shared
    # across the whole test module -- reusing "default" here would make this
    # test's outcome depend on what other tests already populated it with.
    paths: list[str] = []
    original_mkstemp = tempfile.mkstemp

    def _tracking_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, path = original_mkstemp(*args, **kwargs)
        paths.append(path)
        return fd, path

    body = json.dumps(
        {"operation": "record_incident", "schema_version": "krubit.moderation-contract.v1"}
    ).encode("utf-8")
    with unittest.mock.patch("tempfile.mkstemp", side_effect=_tracking_mkstemp):
        for _ in range(2):
            await test_mode_client.post(
                "/moderation/adapter",
                data=body,
                headers={
                    "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
                    "X-Krubit-Test-Mode": "1",
                    "X-Krubit-Test-Session-Id": "reuse-session",
                },
            )
        await test_mode_client.post(
            "/moderation/adapter",
            data=body,
            headers={
                "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
                "X-Krubit-Test-Mode": "1",
                "X-Krubit-Test-Session-Id": "other-session",
            },
        )
    # First two requests (same "reuse-session" id) share one store; the third
    # request (a different session id) gets a fresh one.
    assert len(paths) == 2
    assert paths[0] != paths[1]


async def test_test_mode_session_persists_state_across_requests(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    # Two record_incident calls with the SAME idempotency_key in the SAME
    # test session: the second must see the first's persisted state and
    # return duplicate=True referencing the same case_id, proving the
    # underlying store survives across separate HTTP requests within one
    # session (Finding 1).
    session_headers = {"X-Krubit-Test-Session-Id": "lifecycle-session"}

    async def _post_test_mode(payload: dict[str, object]) -> web.ClientResponse:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
            "X-Krubit-Test-Mode": "1",
            **session_headers,
        }
        return await test_mode_client.post("/moderation/adapter", data=body, headers=headers)

    # Seed the test-mode session's own isolated store directly (it has no
    # pre-seeded incidents, unlike the production `store` fixture).
    from krubit.web.moderation_adapter import _get_or_build_test_mode_service

    seeded_service = await _get_or_build_test_mode_service("lifecycle-session")
    await seeded_service._store.record_incident(
        Incident(
            guild_id=100,
            incident_id="incident:session-1",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=NOW,
            evidence_packet_id="incident:session-1",
            recommended_action="review",
            acknowledged_by=None,
        )
    )

    payload = {
        "operation": "record_incident",
        "schema_version": "krubit.moderation-contract.v1",
        "incident_id": "incident:session-1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
        "idempotency_key": "idem:session-1",
    }
    first = await _post_test_mode(payload)
    assert first.status == 200
    first_payload = await first.json()
    assert first_payload["duplicate"] is False

    second = await _post_test_mode(payload)
    assert second.status == 200
    second_payload = await second.json()
    assert second_payload["duplicate"] is True
    assert second_payload["case_id"] == first_payload["case_id"]


async def test_test_mode_different_sessions_do_not_share_state(
    test_mode_client: TestClient[web.Request, web.Application],
) -> None:
    from krubit.web.moderation_adapter import _get_or_build_test_mode_service

    for session_id in ("session-a", "session-b"):
        seeded_service = await _get_or_build_test_mode_service(session_id)
        await seeded_service._store.record_incident(
            Incident(
                guild_id=100,
                incident_id="incident:shared",
                kind=IncidentKind.MEMBER,
                band=RiskBand.INCIDENT,
                opened_at=NOW,
                evidence_packet_id="incident:shared",
                recommended_action="review",
                acknowledged_by=None,
            )
        )

    payload = {
        "operation": "record_incident",
        "schema_version": "krubit.moderation-contract.v1",
        "incident_id": "incident:shared",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
        "idempotency_key": "idem:cross-session",
    }
    body = json.dumps(payload).encode("utf-8")

    async def _post_session(session_id: str) -> web.ClientResponse:
        headers = {
            "X-Krubit-Moderation-Signature": _sign_with(body, TEST_SECRET),
            "X-Krubit-Test-Mode": "1",
            "X-Krubit-Test-Session-Id": session_id,
        }
        return await test_mode_client.post("/moderation/adapter", data=body, headers=headers)

    first = await _post_session("session-a")
    assert first.status == 200
    assert (await first.json())["duplicate"] is False

    # A different session id must NOT see session-a's state -- it has its
    # own isolated store, so the same idempotency key produces a fresh
    # (non-duplicate) record instead of a duplicate.
    second = await _post_session("session-b")
    assert second.status == 200
    assert (await second.json())["duplicate"] is False


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


async def _build_approved_case(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> str:
    """Drive a case up through APPROVED, using real HTTP calls for every step
    the contract exposes and dropping to a directly-constructed
    `ModerationService` (sharing the same underlying store as `client`) only
    for `record_human_decision`, which is intentionally unreachable via HTTP.
    """
    record_response = await _post(
        client,
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:1",
            "guild_id": "100",
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:approved-1",
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
            "idempotency_key": "idem:approved-2",
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
            "idempotency_key": "idem:approved-3",
        },
    )
    assert approval_response.status == 200

    service = ModerationService(store, FakeExecutor())
    await service.record_human_decision(
        100,
        case_id,
        reviewer_id=999,
        decision=ApprovalDecision.APPROVED,
        timeout_duration=timedelta(hours=24),
    )
    return case_id


async def test_execute_approved_action_round_trips_through_http(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> None:
    case_id = await _build_approved_case(store, client)
    response = await _post(
        client,
        {
            "operation": "execute_approved_action",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "idempotency_key": "idem:execute-1",
        },
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["status"] == "executed"
    assert payload["duplicate"] is False


async def test_close_incident_round_trips_through_http(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> None:
    case_id = await _build_approved_case(store, client)
    execute_response = await _post(
        client,
        {
            "operation": "execute_approved_action",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "idempotency_key": "idem:close-execute",
        },
    )
    assert execute_response.status == 200

    close_response = await _post(
        client,
        {
            "operation": "close_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "decision": "resolved",
            "idempotency_key": "idem:close-1",
        },
    )
    assert close_response.status == 200
    payload = await close_response.json()
    assert payload["status"] == "closed"


async def test_submit_appeal_round_trips_through_http(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> None:
    case_id = await _build_approved_case(store, client)
    execute_response = await _post(
        client,
        {
            "operation": "execute_approved_action",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "idempotency_key": "idem:appeal-execute",
        },
    )
    assert execute_response.status == 200

    appeal_response = await _post(
        client,
        {
            "operation": "submit_appeal",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "reason": "I was wrongly flagged",
            "idempotency_key": "idem:appeal-1",
        },
    )
    assert appeal_response.status == 200
    payload = await appeal_response.json()
    assert payload["appeal_status"] == "submitted"


async def test_get_member_moderation_history_round_trips_through_http(
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
            "idempotency_key": "idem:history-1",
        },
    )
    assert record_response.status == 200
    case_id = (await record_response.json())["case_id"]

    history_response = await _post(
        client,
        {
            "operation": "get_member_moderation_history",
            "schema_version": "krubit.moderation-contract.v1",
            "guild_id": "100",
            "member_id": "200",
        },
    )
    assert history_response.status == 200
    payload = await history_response.json()
    assert case_id in payload["cases"]


async def test_get_incident_round_trips_through_http(
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
            "idempotency_key": "idem:getincident-1",
        },
    )
    assert record_response.status == 200
    case_id = (await record_response.json())["case_id"]

    get_response = await _post(
        client,
        {
            "operation": "get_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "guild_id": "100",
            "incident_id": "incident:1",
        },
    )
    assert get_response.status == 200
    payload = await get_response.json()
    assert payload["case_id"] == case_id


async def test_get_action_receipt_round_trips_through_http(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> None:
    case_id = await _build_approved_case(store, client)
    execute_response = await _post(
        client,
        {
            "operation": "execute_approved_action",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "idempotency_key": "idem:receipt-execute",
        },
    )
    assert execute_response.status == 200
    receipt_id = (await execute_response.json())["receipt_state"]

    receipt_response = await _post(
        client,
        {
            "operation": "get_action_receipt",
            "schema_version": "krubit.moderation-contract.v1",
            "guild_id": "100",
            "receipt_id": receipt_id,
        },
    )
    assert receipt_response.status == 200
    payload = await receipt_response.json()
    assert payload["case_id"] == case_id
    assert payload["succeeded"] is True


async def test_get_pending_reviews_round_trips_through_http(
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
            "idempotency_key": "idem:pending-1",
        },
    )
    assert record_response.status == 200
    case_id = (await record_response.json())["case_id"]

    await _post(
        client,
        {
            "operation": "submit_action_recommendation",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "recommended_action": "24h timeout",
            "idempotency_key": "idem:pending-2",
        },
    )
    await _post(
        client,
        {
            "operation": "request_human_approval",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "review_deadline": "2026-01-02T00:00:00+00:00",
            "idempotency_key": "idem:pending-3",
        },
    )

    pending_response = await _post(
        client,
        {
            "operation": "get_pending_reviews",
            "schema_version": "krubit.moderation-contract.v1",
            "guild_id": "100",
        },
    )
    assert pending_response.status == 200
    payload = await pending_response.json()
    assert case_id in payload["cases"]


async def test_illegal_transition_maps_to_409(
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
            "idempotency_key": "idem:illegal-1",
        },
    )
    assert record_response.status == 200
    case_id = (await record_response.json())["case_id"]

    # Case is still RECORDED -- close_incident requires it to already be
    # EXECUTED/EXECUTION_FAILED, so this must be an IllegalTransitionError
    # mapped to 409, not a generic 400/500.
    close_response = await _post(
        client,
        {
            "operation": "close_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": case_id,
            "guild_id": "100",
            "decision": "resolved",
            "idempotency_key": "idem:illegal-2",
        },
    )
    assert close_response.status == 409


async def test_malformed_body_maps_to_400(
    client: TestClient[web.Request, web.Application],
) -> None:
    # submit_action_recommendation requires recommended_action; omit it to
    # trigger ModerationContractError -> 400.
    response = await _post(
        client,
        {
            "operation": "submit_action_recommendation",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": "case:whatever",
            "guild_id": "100",
            "idempotency_key": "idem:malformed-1",
        },
    )
    assert response.status == 400


async def test_cross_tenant_write_returns_404_not_400(
    store: SQLiteStore, client: TestClient[web.Request, web.Application]
) -> None:
    # Seed a real case in a DIFFERENT guild (200), then attempt a write op
    # against it while authenticated as guild 100. The service raises a
    # "does not belong to the authenticated guild_id" ValueError; the HTTP
    # layer must classify this as 404 (case doesn't exist for THIS guild),
    # not 400 (which would leak that the case exists in another guild).
    await store.record_incident(
        Incident(
            guild_id=200,
            incident_id="incident:other-guild",
            kind=IncidentKind.MEMBER,
            band=RiskBand.INCIDENT,
            opened_at=NOW,
            evidence_packet_id="incident:other-guild",
            recommended_action="review",
            acknowledged_by=None,
        )
    )
    other_guild_service = ModerationService(store, FakeExecutor())
    other_case = await other_guild_service.record_incident(
        200,
        RecordIncidentRequest.from_dict(
            {
                "incident_id": "incident:other-guild",
                "guild_id": "200",
                "member_id": "300",
                "report_timestamp": "2026-01-01T00:00:00+00:00",
                "idempotency_key": "idem:other-guild-1",
            }
        ),
    )

    response = await _post(
        client,
        {
            "operation": "execute_approved_action",
            "schema_version": "krubit.moderation-contract.v1",
            "case_id": other_case.case_id,
            "guild_id": "100",
            "idempotency_key": "idem:cross-tenant-1",
        },
    )
    assert response.status == 404


async def test_non_numeric_guild_id_returns_400_not_crash(
    client: TestClient[web.Request, web.Application],
) -> None:
    response = await _post(
        client,
        {
            "operation": "record_incident",
            "schema_version": "krubit.moderation-contract.v1",
            "incident_id": "incident:1",
            "guild_id": {"a": 1},
            "member_id": "200",
            "report_timestamp": "2026-01-01T00:00:00+00:00",
            "idempotency_key": "idem:badguild-1",
        },
    )
    assert response.status == 400
