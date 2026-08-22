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
