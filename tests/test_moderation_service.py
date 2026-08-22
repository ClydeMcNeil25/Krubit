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
async def test_submit_action_recommendation_wrong_guild_raises(store: SQLiteStore) -> None:
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
    with pytest.raises(ValueError, match="guild_id"):
        await service.submit_action_recommendation(
            999,
            SubmitActionRecommendationRequest.from_dict(
                {
                    "case_id": record_response.case_id,
                    "recommended_action": "24h timeout",
                    "idempotency_key": "idem:2",
                }
            ),
        )


@pytest.mark.asyncio
async def test_request_human_approval_wrong_guild_raises(store: SQLiteStore) -> None:
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
    with pytest.raises(ValueError, match="guild_id"):
        await service.request_human_approval(
            999,
            RequestHumanApprovalRequest.from_dict(
                {
                    "case_id": record_response.case_id,
                    "review_deadline": "2026-01-02T00:00:00+00:00",
                    "idempotency_key": "idem:3",
                }
            ),
        )


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
