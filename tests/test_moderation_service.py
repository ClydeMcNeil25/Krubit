"""Service-layer tests for the moderation storage service (slice 2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krubit.contracts.moderation import (
    CloseIncidentRequest,
    ExecuteApprovedActionRequest,
    GetActionReceiptQuery,
    GetIncidentQuery,
    GetIncidentStatusQuery,
    GetMemberModerationHistoryQuery,
    GetPendingReviewsQuery,
    RecordIncidentRequest,
    RequestHumanApprovalRequest,
    SubmitActionRecommendationRequest,
    SubmitAppealRequest,
)
from krubit.domain.moderation import (
    ActionReceipt,
    ApprovalDecision,
    ModerationStatus,
    UnknownIncidentError,
)
from krubit.domain.watchdog import Incident, IncidentKind, RiskBand
from krubit.services.moderation import ModerationService
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
    def __init__(self, *, succeed: bool = True, fail_mode: str = "raise") -> None:
        """`fail_mode` controls how a non-`succeed` executor reports failure:
        `"raise"` (default) raises an exception; `"return"` returns a well-behaved
        `ActionReceipt(succeeded=False, ...)` instead, matching a real executor that
        never raises on ordinary Discord failures.
        """
        self.succeed = succeed
        self.fail_mode = fail_mode
        self.calls: list[tuple[str, str]] = []

    async def execute(self, case: object, execution_key: str) -> ActionReceipt:
        self.calls.append((case.case_id, execution_key))  # type: ignore[attr-defined]
        if not self.succeed:
            if self.fail_mode == "raise":
                raise RuntimeError("simulated executor failure")
            return ActionReceipt(
                receipt_id="receipt:fake", succeeded=False, detail="simulated executor failure"
            )
        return ActionReceipt(receipt_id="receipt:fake", succeeded=True, detail="ok")


async def _seed_incident(
    store: SQLiteStore, *, guild_id: int = 100, incident_id: str = "incident:1"
) -> None:
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
            {
                "case_id": record_response.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": "idem:2",
            }
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
            {
                "case_id": record_response.case_id,
                "recommended_action": "24h timeout",
                "idempotency_key": "idem:2",
            }
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
    await service.record_human_decision(
        100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED
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
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    assert response.status is ModerationStatus.EXECUTED
    # receipt_id is always service-generated (Finding 5), not trusted from the executor.
    assert response.receipt_state is not None
    assert response.receipt_state.startswith("receipt:")
    assert response.receipt_state != "receipt:fake"
    assert executor.calls == [(case_id, "idem:4")]


@pytest.mark.asyncio
async def test_execute_approved_action_failure(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=False)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    assert response.status is ModerationStatus.EXECUTION_FAILED
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_execute_approved_action_wrong_guild_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    with pytest.raises(ValueError, match="guild_id"):
        await service.execute_approved_action(
            999,
            ExecuteApprovedActionRequest.from_dict(
                {"case_id": case_id, "idempotency_key": "idem:4"}
            ),
        )


@pytest.mark.asyncio
async def test_close_incident_from_executed(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
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
async def test_close_incident_wrong_guild_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    with pytest.raises(ValueError, match="guild_id"):
        await service.close_incident(
            999,
            CloseIncidentRequest.from_dict(
                {"case_id": case_id, "decision": "resolved", "idempotency_key": "idem:5"}
            ),
        )


@pytest.mark.asyncio
async def test_submit_appeal_sets_appeal_status(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    response = await service.submit_appeal(
        100,
        SubmitAppealRequest.from_dict(
            {
                "case_id": case_id,
                "reason": "member disputes the timeout",
                "idempotency_key": "idem:6",
            }
        ),
    )
    assert response.appeal_status.value == "submitted"
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.EXECUTED


@pytest.mark.asyncio
async def test_submit_appeal_wrong_guild_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    with pytest.raises(ValueError, match="guild_id"):
        await service.submit_appeal(
            999,
            SubmitAppealRequest.from_dict(
                {
                    "case_id": case_id,
                    "reason": "member disputes the timeout",
                    "idempotency_key": "idem:6",
                }
            ),
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
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
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
    result = await service.get_pending_reviews(
        100, GetPendingReviewsQuery.from_dict({"guild_id": "100"})
    )
    assert result.cases == (record_response.case_id,)


@pytest.mark.asyncio
async def test_get_action_receipt_cross_tenant_raises(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=True)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    execute_response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    with pytest.raises(ValueError):
        await service.get_action_receipt(
            999,
            GetActionReceiptQuery.from_dict(
                {"guild_id": "999", "receipt_id": execute_response.receipt_state}
            ),
        )


@pytest.mark.asyncio
async def test_get_member_moderation_history_guild_id_mismatch_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    await _approved_case(store, service)
    with pytest.raises(ValueError, match="guild_id"):
        await service.get_member_moderation_history(
            999, GetMemberModerationHistoryQuery.from_dict({"guild_id": "100", "member_id": "200"})
        )


@pytest.mark.asyncio
async def test_get_incident_guild_id_mismatch_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    await _approved_case(store, service)
    with pytest.raises(ValueError, match="guild_id"):
        await service.get_incident(
            999, GetIncidentQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
        )


@pytest.mark.asyncio
async def test_get_incident_status_guild_id_mismatch_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    await _approved_case(store, service)
    with pytest.raises(ValueError, match="guild_id"):
        await service.get_incident_status(
            999, GetIncidentStatusQuery.from_dict({"guild_id": "100", "incident_id": "incident:1"})
        )


@pytest.mark.asyncio
async def test_get_action_receipt_guild_id_mismatch_raises(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=True)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    execute_response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    with pytest.raises(ValueError, match="guild_id"):
        await service.get_action_receipt(
            999,
            GetActionReceiptQuery.from_dict(
                {"guild_id": "100", "receipt_id": execute_response.receipt_state}
            ),
        )


@pytest.mark.asyncio
async def test_get_pending_reviews_guild_id_mismatch_raises(store: SQLiteStore) -> None:
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    with pytest.raises(ValueError, match="guild_id"):
        await service.get_pending_reviews(
            999, GetPendingReviewsQuery.from_dict({"guild_id": "100"})
        )


@pytest.mark.asyncio
async def test_same_idempotency_key_across_two_operations_does_not_collide(
    store: SQLiteStore,
) -> None:
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
async def test_execute_approved_action_executor_returns_failed_receipt(store: SQLiteStore) -> None:
    """A well-behaved executor that RETURNS `ActionReceipt(succeeded=False, ...)` rather
    than raising must still land the case in EXECUTION_FAILED (Finding 1)."""
    executor = FakeExecutor(succeed=False, fail_mode="return")
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    response = await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    assert response.status is ModerationStatus.EXECUTION_FAILED
    case = await store.get_moderation_case(case_id)
    assert case is not None
    assert case.status is ModerationStatus.EXECUTION_FAILED
    assert case.executed_action is None


@pytest.mark.asyncio
async def test_record_human_decision_wrong_guild_raises(store: SQLiteStore) -> None:
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
    with pytest.raises(ValueError, match="guild_id"):
        await service.record_human_decision(
            999, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.APPROVED
        )


@pytest.mark.asyncio
async def test_record_human_decision_rejected_transitions_case(store: SQLiteStore) -> None:
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
    case = await service.record_human_decision(
        100, record_response.case_id, reviewer_id=999, decision=ApprovalDecision.REJECTED
    )
    assert case.status is ModerationStatus.REJECTED


@pytest.mark.asyncio
async def test_execute_approved_action_idempotent_does_not_reexecute(store: SQLiteStore) -> None:
    executor = FakeExecutor(succeed=True)
    service = ModerationService(store, executor)
    case_id = await _approved_case(store, service)
    request = ExecuteApprovedActionRequest.from_dict(
        {"case_id": case_id, "idempotency_key": "idem:4"}
    )
    first = await service.execute_approved_action(100, request)
    second = await service.execute_approved_action(100, request)
    assert second.duplicate is True
    assert second.receipt_state == first.receipt_state
    assert executor.calls == [(case_id, "idem:4")]


@pytest.mark.asyncio
async def test_get_action_receipt_missing_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    with pytest.raises(ValueError):
        await service.get_action_receipt(
            100,
            GetActionReceiptQuery.from_dict({"guild_id": "100", "receipt_id": "receipt:missing"}),
        )


@pytest.mark.asyncio
async def test_submit_action_recommendation_illegal_status_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor())
    case_id = await _approved_case(store, service)
    with pytest.raises(ValueError):
        await service.submit_action_recommendation(
            100,
            SubmitActionRecommendationRequest.from_dict(
                {
                    "case_id": case_id,
                    "recommended_action": "24h timeout",
                    "idempotency_key": "idem:new",
                }
            ),
        )


@pytest.mark.asyncio
async def test_execute_approved_action_illegal_status_raises(store: SQLiteStore) -> None:
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
    with pytest.raises(ValueError):
        await service.execute_approved_action(
            100,
            ExecuteApprovedActionRequest.from_dict(
                {"case_id": record_response.case_id, "idempotency_key": "idem:4"}
            ),
        )


@pytest.mark.asyncio
async def test_submit_appeal_twice_raises(store: SQLiteStore) -> None:
    service = ModerationService(store, FakeExecutor(succeed=True))
    case_id = await _approved_case(store, service)
    await service.execute_approved_action(
        100,
        ExecuteApprovedActionRequest.from_dict({"case_id": case_id, "idempotency_key": "idem:4"}),
    )
    await service.submit_appeal(
        100,
        SubmitAppealRequest.from_dict(
            {
                "case_id": case_id,
                "reason": "member disputes the timeout",
                "idempotency_key": "idem:6",
            }
        ),
    )
    with pytest.raises(ValueError):
        await service.submit_appeal(
            100,
            SubmitAppealRequest.from_dict(
                {"case_id": case_id, "reason": "second appeal", "idempotency_key": "idem:7"}
            ),
        )


@pytest.mark.asyncio
async def test_record_incident_second_time_for_cased_incident_returns_duplicate(
    store: SQLiteStore,
) -> None:
    """A second `record_incident` for an already-cased `incident_id`, using a *different*
    idempotency key (not a literal duplicate request), returns the existing case with
    `duplicate=True` rather than erroring — see Finding 10."""
    await _seed_incident(store)
    service = ModerationService(store, FakeExecutor())
    request_kwargs = {
        "incident_id": "incident:1",
        "guild_id": "100",
        "member_id": "200",
        "report_timestamp": "2026-01-01T00:00:00+00:00",
    }
    first = await service.record_incident(
        100, RecordIncidentRequest.from_dict({**request_kwargs, "idempotency_key": "idem:1"})
    )
    second = await service.record_incident(
        100,
        RecordIncidentRequest.from_dict({**request_kwargs, "idempotency_key": "idem:different"}),
    )
    assert second.case_id == first.case_id
    assert second.duplicate is True


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
