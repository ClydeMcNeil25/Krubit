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
import itertools
import json
import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

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
from krubit.domain.moderation import (
    ActionReceipt,
    IllegalTransitionError,
    ModerationCase,
    UnknownIncidentError,
)
from krubit.services.moderation import ModerationService
from krubit.storage.sqlite import SQLiteStore
from krubit.web.callbacks import CallbackRoute

_logger = logging.getLogger(__name__)


class PendingReviewNotifier(Protocol):
    """The one notifier capability this HTTP layer needs.

    Declared structurally rather than importing
    `krubit.discord.moderation_notifier.ModerationNotifier` directly, so the
    `krubit.web` package keeps its existing property of never importing
    `discord` -- exactly the reason `ActionExecutor` is a Protocol in
    `krubit.services.moderation` too. The real `ModerationNotifier` satisfies
    this structurally with no registration or subclassing.
    """

    async def notify_pending_review(self, case: ModerationCase) -> None: ...


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


async def _notify_pending_review(
    service: ModerationService,
    notifier: PendingReviewNotifier,
    guild_id: int,
    response_payload: dict[str, object],
) -> None:
    """Best-effort `#mod-room` announcement for a freshly requested review.

    Fully isolated from the HTTP response that is already about to be
    returned: nothing raised in here -- including a store read failing, or a
    notifier implementation that raises rather than swallowing as
    `ModerationNotifier._send` does -- may turn a successful 200 into an
    error. Matches `WatchdogRuntime.notify_staff`'s "never block the state
    change it follows" contract.
    """
    try:
        case_id = response_payload.get("case_id")
        if not isinstance(case_id, str):
            return
        case = await service.get_case(guild_id, case_id)
        if case is None:
            return
        await notifier.notify_pending_review(case)
    except Exception:
        _logger.exception(
            "moderation adapter: pending-review notification failed for guild %s", guild_id
        )


async def _handle(
    request: web.Request,
    *,
    header_name: str,
    secret: str,
    service: ModerationService,
    notifier: PendingReviewNotifier | None = None,
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
        else:
            guild_id = int(getattr(parsed_request, "guild_id", "0"))
    except (TypeError, ValueError):
        return _error_response(400, "guild_id is missing or invalid")

    try:
        result = await method(guild_id, parsed_request)
    except UnknownIncidentError as exc:
        return _error_response(404, str(exc))
    except IllegalTransitionError as exc:
        return _error_response(409, str(exc))
    except ModerationContractError as exc:
        return _error_response(400, str(exc))
    except ValueError as exc:
        message = str(exc)
        is_not_found = (
            "unknown" in message
            or "no case found" in message
            or "no receipt found" in message
            or "does not belong" in message
            or "does not match the authenticated" in message
        )
        status = 404 if is_not_found else 400
        return _error_response(status, message)

    response_payload = dict(result.to_dict())
    # A replayed request (same idempotency key) returns the cached response
    # rather than re-transitioning the case, so re-announcing it would spam
    # #mod-room with a duplicate of a review staff were already told about.
    if (
        operation == "request_human_approval"
        and notifier is not None
        and response_payload.get("duplicate") is not True
    ):
        await _notify_pending_review(service, notifier, guild_id, response_payload)
    response_payload["correlation_id"] = correlation_id
    return web.json_response(response_payload, status=200)


def build_moderation_adapter_route(
    *,
    path: str,
    header_name: str,
    secret: str,
    service: ModerationService,
    notifier: PendingReviewNotifier | None = None,
    test_header_name: str = "X-Krubit-Test-Mode",
    test_mode_enabled: bool = False,
    test_secret: str | None = None,
) -> CallbackRoute:
    async def handler(request: web.Request) -> web.StreamResponse:
        test_mode_requested = request.headers.get(test_header_name) == "1"
        if test_mode_enabled and test_mode_requested and test_secret is not None:
            active_secret = test_secret
            session_id = request.headers.get("X-Krubit-Test-Session-Id", "default")
            active_service = await _get_or_build_test_mode_service(session_id)
            # Test-mode traffic runs against a throwaway store and must never
            # reach a real #mod-room, so the notifier is deliberately dropped
            # on this branch -- matching the isolated-store decision above.
            active_notifier: PendingReviewNotifier | None = None
        else:
            active_secret = secret
            active_service = service
            active_notifier = notifier
        return await _handle(
            request,
            header_name=header_name,
            secret=active_secret,
            service=active_service,
            notifier=active_notifier,
        )

    return CallbackRoute(path=path, method="POST", handler=handler)


# Test-mode services are session-scoped (keyed by the caller-supplied
# X-Krubit-Test-Session-Id header, or "default" when absent) rather than
# built fresh per request, so a caller can run a full multi-step lifecycle
# (record_incident -> submit_action_recommendation -> ...) against the same
# isolated store within one test session. Bounded by _MAX_TEST_MODE_SESSIONS
# with oldest-entry eviction (and store closure) to prevent unbounded growth
# of temp-file DB connections/threads under sustained test-mode traffic.
_TEST_MODE_SESSIONS: dict[str, ModerationService] = {}
_MAX_TEST_MODE_SESSIONS = 32


async def _get_or_build_test_mode_service(session_id: str) -> ModerationService:
    if session_id in _TEST_MODE_SESSIONS:
        return _TEST_MODE_SESSIONS[session_id]
    if len(_TEST_MODE_SESSIONS) >= _MAX_TEST_MODE_SESSIONS:
        oldest_key = next(iter(_TEST_MODE_SESSIONS))
        oldest_service = _TEST_MODE_SESSIONS.pop(oldest_key)
        await oldest_service._store.close()
    service = await _build_test_mode_service()
    _TEST_MODE_SESSIONS[session_id] = service
    return service


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
