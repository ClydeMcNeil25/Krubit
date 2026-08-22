from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krubit.contracts.moderation import (
    ModerationContractError,
    RecordIncidentRequest,
    RecordIncidentResponse,
    RequestHumanApprovalRequest,
    RequestHumanApprovalResponse,
    SubmitActionRecommendationRequest,
    SubmitActionRecommendationResponse,
    _required_text,
    _timestamp,
)
from krubit.domain.moderation import ModerationStatus


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
    assert request.to_dict() == {
        "case_id": "case:1",
        "review_deadline": "2026-01-02T00:00:00Z",
        "idempotency_key": "idem:3",
    }

    response_payload = {
        "case_id": "case:1",
        "status": "approval_required",
        "duplicate": True,
        "receipt_state": None,
    }
    response = RequestHumanApprovalResponse.from_dict(response_payload)
    assert response.duplicate is True
    assert response.to_dict() == response_payload


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
