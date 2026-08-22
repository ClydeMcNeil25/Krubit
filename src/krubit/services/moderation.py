"""Moderation case lifecycle service: implements the 11 Zariya-facing
contract operations (plus one internal admin-reserved operation) against
real SQLite storage. No Discord or HTTP knowledge — matches
`services/incident_evidence.py`'s framework-independent convention.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Protocol

from krubit.contracts.moderation import (
    CloseIncidentRequest,
    CloseIncidentResponse,
    ExecuteApprovedActionRequest,
    ExecuteApprovedActionResponse,
    RecordIncidentRequest,
    RecordIncidentResponse,
    RequestHumanApprovalRequest,
    RequestHumanApprovalResponse,
    SubmitActionRecommendationRequest,
    SubmitActionRecommendationResponse,
    SubmitAppealRequest,
    SubmitAppealResponse,
)
from krubit.domain.moderation import (
    ActionReceipt,
    AppealStatus,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
    UnknownIncidentError,
    transition,
)
from krubit.storage.sqlite import SQLiteStore

_RECORD_INCIDENT = "record_incident"
_SUBMIT_ACTION_RECOMMENDATION = "submit_action_recommendation"
_REQUEST_HUMAN_APPROVAL = "request_human_approval"
_EXECUTE_APPROVED_ACTION = "execute_approved_action"
_CLOSE_INCIDENT = "close_incident"
_SUBMIT_APPEAL = "submit_appeal"


class ActionExecutor(Protocol):
    """Caller-supplied Discord-mutation performer, injected into
    `ModerationService`. A real implementation lands in slice 4; this slice
    tests against a fake. Mirrors `SignedWebhook`'s injected-protocol shape
    in `krubit.web.callbacks`.
    """

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt: ...


def _new_case_id() -> str:
    return f"case:{uuid.uuid4().hex}"


class ModerationService:
    def __init__(self, store: SQLiteStore, executor: ActionExecutor) -> None:
        self._store = store
        self._executor = executor

    async def record_incident(
        self, guild_id: int, request: RecordIncidentRequest
    ) -> RecordIncidentResponse:
        if request.guild_id != guild_id:
            raise ValueError("request.guild_id does not match the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            guild_id, _RECORD_INCIDENT, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return RecordIncidentResponse.from_dict(payload | {"duplicate": True})

        existing_incident = await self._store.get_incident(guild_id, request.incident_id)
        if existing_incident is None:
            raise UnknownIncidentError(f"unknown incident_id {request.incident_id!r}")

        existing_case = await self._store.get_moderation_case_by_incident(
            guild_id, request.incident_id
        )
        if existing_case is not None:
            response = RecordIncidentResponse(
                case_id=existing_case.case_id,
                status=existing_case.status,
                duplicate=True,
                receipt_state=None,
            )
            await self._cache_response(
                guild_id, _RECORD_INCIDENT, request.idempotency_key, response.case_id, response
            )
            return response

        offense_number = 1 + await self._store.count_moderation_cases_for_member(
            guild_id, request.member_id
        )
        case = ModerationCase(
            case_id=_new_case_id(),
            incident_id=request.incident_id,
            guild_id=guild_id,
            member_id=request.member_id,
            report_timestamp=request.report_timestamp,
            offense_number=offense_number,
            recommended_action=None,
            executed_action=None,
            action_expiration=None,
            status=ModerationStatus.RECORDED,
            review_deadline=None,
            reviewer_id=None,
            reviewer_decision=None,
            appeal_status=AppealStatus.NONE,
            close_timestamp=None,
        )
        await self._store.save_moderation_case(case)
        response = RecordIncidentResponse(
            case_id=case.case_id, status=case.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            guild_id, _RECORD_INCIDENT, request.idempotency_key, response.case_id, response
        )
        return response

    async def submit_action_recommendation(
        self, guild_id: int, request: SubmitActionRecommendationRequest
    ) -> SubmitActionRecommendationResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _SUBMIT_ACTION_RECOMMENDATION, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return SubmitActionRecommendationResponse.from_dict(payload | {"duplicate": True})

        if case.status is not ModerationStatus.RECORDED:
            raise ValueError(
                f"submit_action_recommendation requires status RECORDED, case is {case.status}"
            )
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=request.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=case.status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = SubmitActionRecommendationResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _SUBMIT_ACTION_RECOMMENDATION, request.idempotency_key, response.case_id, response
        )
        return response

    async def request_human_approval(
        self, guild_id: int, request: RequestHumanApprovalRequest
    ) -> RequestHumanApprovalResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _REQUEST_HUMAN_APPROVAL, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return RequestHumanApprovalResponse.from_dict(payload | {"duplicate": True})

        if case.recommended_action is None:
            raise ValueError("recommended_action must be set before requesting approval")
        new_status = transition(case.status, ModerationStatus.APPROVAL_REQUIRED)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=request.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = RequestHumanApprovalResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _REQUEST_HUMAN_APPROVAL, request.idempotency_key, response.case_id, response
        )
        return response

    async def record_human_decision(
        self, case_id: str, *, reviewer_id: int, decision: ApprovalDecision
    ) -> ModerationCase:
        """Internal — not part of the 11-operation Zariya contract. Reserved
        for slice 4's `/fetch admin` approval commands; added now only so
        this slice's own tests can drive a case through the full lifecycle.
        """
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            raise ValueError(f"unknown case_id {case_id!r}")
        target = (
            ModerationStatus.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ModerationStatus.REJECTED
        )
        new_status = transition(case.status, target)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=reviewer_id,
            reviewer_decision=decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        return await self._store.save_moderation_case(updated)

    async def execute_approved_action(
        self, guild_id: int, request: ExecuteApprovedActionRequest
    ) -> ExecuteApprovedActionResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _EXECUTE_APPROVED_ACTION, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return ExecuteApprovedActionResponse.from_dict(payload | {"duplicate": True})

        if case.status is not ModerationStatus.APPROVED:
            raise ValueError(
                f"execute_approved_action requires status APPROVED, case is {case.status}"
            )

        try:
            receipt = await self._executor.execute(case, request.idempotency_key)
            new_status = ModerationStatus.EXECUTED
            executed_action = case.recommended_action
        except Exception:
            receipt = ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}", succeeded=False, detail="executor raised"
            )
            new_status = ModerationStatus.EXECUTION_FAILED
            executed_action = None

        transition(case.status, new_status)
        await self._store.save_action_receipt(case.guild_id, receipt, case.case_id)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = ExecuteApprovedActionResponse(
            case_id=updated.case_id,
            status=updated.status,
            duplicate=False,
            receipt_state=receipt.receipt_id,
        )
        await self._cache_response(
            case.guild_id, _EXECUTE_APPROVED_ACTION, request.idempotency_key, response.case_id, response
        )
        return response

    async def close_incident(
        self, guild_id: int, request: CloseIncidentRequest
    ) -> CloseIncidentResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _CLOSE_INCIDENT, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return CloseIncidentResponse.from_dict(payload | {"duplicate": True})

        new_status = transition(case.status, ModerationStatus.CLOSED)
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=new_status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=case.appeal_status,
            close_timestamp=datetime.now(UTC),
        )
        await self._store.save_moderation_case(updated)
        response = CloseIncidentResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id, _CLOSE_INCIDENT, request.idempotency_key, response.case_id, response
        )
        return response

    async def submit_appeal(
        self, guild_id: int, request: SubmitAppealRequest
    ) -> SubmitAppealResponse:
        case = await self._store.get_moderation_case(request.case_id)
        if case is None:
            raise ValueError(f"unknown case_id {request.case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        cached = await self._store.get_moderation_idempotency_response(
            case.guild_id, _SUBMIT_APPEAL, request.idempotency_key
        )
        if cached is not None:
            payload = json.loads(cached)
            return SubmitAppealResponse.from_dict(payload | {"duplicate": True})

        if case.appeal_status is not AppealStatus.NONE:
            raise ValueError("an appeal has already been submitted for this case")
        if case.status not in (ModerationStatus.EXECUTED, ModerationStatus.CLOSED):
            raise ValueError(
                f"submit_appeal requires status EXECUTED or CLOSED, case is {case.status}"
            )
        updated = ModerationCase(
            case_id=case.case_id,
            incident_id=case.incident_id,
            guild_id=case.guild_id,
            member_id=case.member_id,
            report_timestamp=case.report_timestamp,
            offense_number=case.offense_number,
            recommended_action=case.recommended_action,
            executed_action=case.executed_action,
            action_expiration=case.action_expiration,
            status=case.status,
            review_deadline=case.review_deadline,
            reviewer_id=case.reviewer_id,
            reviewer_decision=case.reviewer_decision,
            appeal_status=AppealStatus.SUBMITTED,
            close_timestamp=case.close_timestamp,
        )
        await self._store.save_moderation_case(updated)
        response = SubmitAppealResponse(
            case_id=updated.case_id, appeal_status=updated.appeal_status, duplicate=False
        )
        await self._cache_response(
            case.guild_id, _SUBMIT_APPEAL, request.idempotency_key, response.case_id, response
        )
        return response

    async def _cache_response(
        self, guild_id: int, operation: str, idempotency_key: str, case_id: str, response: object
    ) -> None:
        await self._store.save_moderation_idempotency_response(
            guild_id, operation, idempotency_key, case_id, json.dumps(response.to_dict())  # type: ignore[attr-defined]
        )
