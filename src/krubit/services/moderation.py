"""Moderation case lifecycle service: implements the 11 Zariya-facing
contract operations (plus one internal admin-reserved operation) against
real SQLite storage. No Discord or HTTP knowledge — matches
`services/incident_evidence.py`'s framework-independent convention.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from krubit.contracts.moderation import (
    CloseIncidentRequest,
    CloseIncidentResponse,
    ExecuteApprovedActionRequest,
    ExecuteApprovedActionResponse,
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
    _MAX_ACTION_LENGTH,
    ActionReceipt,
    AppealStatus,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
    UnknownIncidentError,
    transition,
)
from krubit.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)

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


def _new_receipt_id() -> str:
    return f"receipt:{uuid.uuid4().hex}"


class ModerationService:
    def __init__(
        self,
        store: SQLiteStore,
        executor: ActionExecutor,
        *,
        id_factory: Callable[[], str] = _new_case_id,
        receipt_id_factory: Callable[[], str] = _new_receipt_id,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._executor = executor
        self._id_factory = id_factory
        self._receipt_id_factory = receipt_id_factory
        self._clock = clock

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
            case_id=self._id_factory(),
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
        try:
            await self._store.save_moderation_case(case)
        except sqlite3.IntegrityError:
            existing_case = await self._store.get_moderation_case_by_incident(
                guild_id, request.incident_id
            )
            if existing_case is None:
                raise RuntimeError(
                    "case row missing immediately after a unique-constraint conflict "
                    "on the same key"
                ) from None
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
        updated = replace(case, recommended_action=request.recommended_action)
        await self._store.save_moderation_case(updated)
        response = SubmitActionRecommendationResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id,
            _SUBMIT_ACTION_RECOMMENDATION,
            request.idempotency_key,
            response.case_id,
            response,
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
        updated = replace(case, status=new_status, review_deadline=request.review_deadline)
        await self._store.save_moderation_case(updated)
        response = RequestHumanApprovalResponse(
            case_id=updated.case_id, status=updated.status, duplicate=False, receipt_state=None
        )
        await self._cache_response(
            case.guild_id,
            _REQUEST_HUMAN_APPROVAL,
            request.idempotency_key,
            response.case_id,
            response,
        )
        return response

    async def record_human_decision(
        self,
        guild_id: int,
        case_id: str,
        *,
        reviewer_id: int,
        decision: ApprovalDecision,
        timeout_duration: timedelta | None = None,
    ) -> ModerationCase:
        """Internal — not part of the 11-operation Zariya contract. Reserved
        for slice 4's `/fetch moderation` approval commands.
        """
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            raise ValueError(f"unknown case_id {case_id!r}")
        if case.guild_id != guild_id:
            raise ValueError("request does not belong to the authenticated guild_id")
        target = (
            ModerationStatus.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ModerationStatus.REJECTED
        )
        if target is ModerationStatus.APPROVED and timeout_duration is None:
            raise ValueError("timeout_duration is required when approving a case")
        new_status = transition(case.status, target)
        action_expiration = (
            self._clock() + timeout_duration if target is ModerationStatus.APPROVED else None
        )
        updated = replace(
            case,
            status=new_status,
            reviewer_id=reviewer_id,
            reviewer_decision=decision,
            action_expiration=action_expiration,
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
            raw_receipt = await self._executor.execute(case, request.idempotency_key)
            new_status = (
                ModerationStatus.EXECUTED
                if raw_receipt.succeeded
                else ModerationStatus.EXECUTION_FAILED
            )
            executed_action = case.recommended_action if raw_receipt.succeeded else None
            receipt = ActionReceipt(
                receipt_id=self._receipt_id_factory(),
                succeeded=raw_receipt.succeeded,
                detail=raw_receipt.detail,
            )
        except Exception as exc:
            logger.exception("ActionExecutor.execute raised for case %s", case.case_id)
            receipt = ActionReceipt(
                receipt_id=self._receipt_id_factory(),
                succeeded=False,
                detail=f"executor raised: {exc!r}"[:_MAX_ACTION_LENGTH],
            )
            new_status = ModerationStatus.EXECUTION_FAILED
            executed_action = None

        new_status = transition(case.status, new_status)
        updated = replace(case, executed_action=executed_action, status=new_status)
        response = ExecuteApprovedActionResponse(
            case_id=updated.case_id,
            status=updated.status,
            duplicate=False,
            receipt_state=receipt.receipt_id,
        )
        await self._store.save_execution_outcome(
            updated,
            receipt,
            _EXECUTE_APPROVED_ACTION,
            request.idempotency_key,
            json.dumps(response.to_dict()),
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
        updated = replace(case, status=new_status, close_timestamp=self._clock())
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
        updated = replace(case, appeal_status=AppealStatus.SUBMITTED)
        await self._store.save_moderation_case(updated)
        response = SubmitAppealResponse(
            case_id=updated.case_id, appeal_status=updated.appeal_status, duplicate=False
        )
        await self._cache_response(
            case.guild_id, _SUBMIT_APPEAL, request.idempotency_key, response.case_id, response
        )
        return response

    async def get_member_moderation_history(
        self, guild_id: int, query: GetMemberModerationHistoryQuery
    ) -> GetMemberModerationHistoryResult:
        if guild_id != int(query.guild_id):
            raise ValueError("guild_id does not match the authenticated guild_id")
        cases = await self._store.list_moderation_cases_for_member(
            int(query.guild_id), int(query.member_id)
        )
        return GetMemberModerationHistoryResult(
            guild_id=query.guild_id,
            member_id=query.member_id,
            cases=tuple(case.case_id for case in cases),
        )

    async def get_incident(self, guild_id: int, query: GetIncidentQuery) -> GetIncidentResult:
        if guild_id != int(query.guild_id):
            raise ValueError("guild_id does not match the authenticated guild_id")
        case = await self._store.get_moderation_case_by_incident(
            int(query.guild_id), query.incident_id
        )
        if case is None:
            raise ValueError(f"no case found for incident_id {query.incident_id!r}")
        return GetIncidentResult(
            guild_id=query.guild_id, incident_id=query.incident_id, case_id=case.case_id
        )

    async def get_incident_status(
        self, guild_id: int, query: GetIncidentStatusQuery
    ) -> GetIncidentStatusResult:
        if guild_id != int(query.guild_id):
            raise ValueError("guild_id does not match the authenticated guild_id")
        case = await self._store.get_moderation_case_by_incident(
            int(query.guild_id), query.incident_id
        )
        if case is None:
            raise ValueError(f"no case found for incident_id {query.incident_id!r}")
        return GetIncidentStatusResult(
            guild_id=query.guild_id, incident_id=query.incident_id, status=case.status
        )

    async def get_action_receipt(
        self, guild_id: int, query: GetActionReceiptQuery
    ) -> GetActionReceiptResult:
        if guild_id != int(query.guild_id):
            raise ValueError("guild_id does not match the authenticated guild_id")
        found = await self._store.get_action_receipt_by_id(query.receipt_id)
        if found is None:
            raise ValueError(f"no receipt found for receipt_id {query.receipt_id!r}")
        receipt, case_id = found
        case = await self._store.get_moderation_case(case_id)
        if case is None or case.guild_id != int(query.guild_id):
            raise ValueError(f"no receipt found for receipt_id {query.receipt_id!r}")
        return GetActionReceiptResult(
            receipt_id=receipt.receipt_id, case_id=case_id, succeeded=receipt.succeeded
        )

    async def get_pending_reviews(
        self, guild_id: int, query: GetPendingReviewsQuery
    ) -> GetPendingReviewsResult:
        if guild_id != int(query.guild_id):
            raise ValueError("guild_id does not match the authenticated guild_id")
        cases = await self._store.list_pending_review_cases(int(query.guild_id))
        return GetPendingReviewsResult(
            guild_id=query.guild_id, cases=tuple(case.case_id for case in cases)
        )

    async def get_case(self, guild_id: int, case_id: str) -> ModerationCase | None:
        case = await self._store.get_moderation_case(case_id)
        if case is None:
            return None
        if case.guild_id != guild_id:
            return None
        return case

    async def _cache_response(
        self, guild_id: int, operation: str, idempotency_key: str, case_id: str, response: object
    ) -> None:
        # A concurrent request for the same (guild, operation, idempotency_key) may
        # already have cached its response first; that cached entry is equivalent to
        # this one, so there is nothing further to do.
        with contextlib.suppress(sqlite3.IntegrityError):
            await self._store.save_moderation_idempotency_response(
                guild_id,
                operation,
                idempotency_key,
                case_id,
                json.dumps(response.to_dict()),  # type: ignore[attr-defined]
            )
