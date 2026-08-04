from datetime import UTC, datetime

import pytest

from krubit.contracts.zariya import ZARIYA_SIGNAL_SCHEMA, SignalContractError, ZariyaSignal


def test_test_signal_has_literal_safe_contract() -> None:
    signal = ZariyaSignal.create_test(
        guild_id=356068206034550784,
        source_event_id="phase0-smoke-1",
        occurred_at=datetime(2026, 8, 3, 17, 30, tzinfo=UTC),
    )

    payload = signal.to_dict()

    assert payload == {
        "schema_version": "krubit.zariya-signal.v1",
        "signal_id": "signal:phase0-smoke-1",
        "guild_id": "356068206034550784",
        "kind": "foundation_test",
        "severity": "info",
        "occurred_at": "2026-08-03T17:30:00Z",
        "source_event_id": "phase0-smoke-1",
        "summary": "Krubit Phase 0 signal path is healthy.",
        "evidence": {"phase": 0, "member_data": False},
        "action_request": None,
    }


def test_signal_round_trip_redacts_evidence() -> None:
    payload = {
        "schema_version": ZARIYA_SIGNAL_SCHEMA,
        "signal_id": "signal:test",
        "guild_id": "111",
        "kind": "foundation_test",
        "severity": "info",
        "occurred_at": "2026-08-03T17:30:00Z",
        "source_event_id": "source-1",
        "summary": "test",
        "evidence": {"debug": "secret=do-not-emit"},
        "action_request": None,
    }

    signal = ZariyaSignal.from_dict(payload)

    assert signal.to_dict()["evidence"] == {"debug": "secret=[REDACTED]"}


def test_signal_rejects_unknown_schema() -> None:
    with pytest.raises(SignalContractError, match="schema_version"):
        ZariyaSignal.from_dict(
            {
                "schema_version": "krubit.zariya-signal.v2",
                "signal_id": "signal:test",
                "guild_id": "111",
                "kind": "foundation_test",
                "severity": "info",
                "occurred_at": "2026-08-03T17:30:00Z",
                "source_event_id": "source-1",
                "summary": "test",
                "evidence": {},
                "action_request": None,
            }
        )


def test_signal_rejects_action_request_in_phase_zero() -> None:
    with pytest.raises(SignalContractError, match="action_request"):
        ZariyaSignal.from_dict(
            {
                "schema_version": ZARIYA_SIGNAL_SCHEMA,
                "signal_id": "signal:test",
                "guild_id": "111",
                "kind": "foundation_test",
                "severity": "info",
                "occurred_at": "2026-08-03T17:30:00Z",
                "source_event_id": "source-1",
                "summary": "test",
                "evidence": {},
                "action_request": {"action": "ban"},
            }
        )
