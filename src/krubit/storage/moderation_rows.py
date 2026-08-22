"""Row-decoding helpers for the moderation storage service (Phase 5+6 slice 2).

Mirrors `storage/watchdog_rows.py`'s convention: isolate column-to-value-object
mapping so `SQLiteStore` stays focused on queries and transactions.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from krubit.domain.moderation import (
    ActionReceipt,
    AppealStatus,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def moderation_case_from_row(row: aiosqlite.Row | None) -> ModerationCase | None:
    if row is None:
        return None
    reviewer_id = row["reviewer_id"]
    reviewer_decision = row["reviewer_decision"]
    return ModerationCase(
        case_id=str(row["case_id"]),
        incident_id=str(row["incident_id"]),
        guild_id=int(row["guild_id"]),
        member_id=int(row["member_id"]),
        report_timestamp=datetime.fromisoformat(str(row["report_timestamp"])),
        offense_number=int(row["offense_number"]),
        recommended_action=(
            None if row["recommended_action"] is None else str(row["recommended_action"])
        ),
        executed_action=(
            None if row["executed_action"] is None else str(row["executed_action"])
        ),
        action_expiration=_optional_datetime(row["action_expiration"]),
        status=ModerationStatus(str(row["status"])),
        review_deadline=_optional_datetime(row["review_deadline"]),
        reviewer_id=None if reviewer_id is None else int(reviewer_id),
        reviewer_decision=(
            None if reviewer_decision is None else ApprovalDecision(str(reviewer_decision))
        ),
        appeal_status=AppealStatus(str(row["appeal_status"])),
        close_timestamp=_optional_datetime(row["close_timestamp"]),
    )


def action_receipt_from_row(row: aiosqlite.Row | None) -> ActionReceipt | None:
    if row is None:
        return None
    return ActionReceipt(
        receipt_id=str(row["receipt_id"]),
        succeeded=bool(row["succeeded"]),
        detail=str(row["detail"]),
    )
