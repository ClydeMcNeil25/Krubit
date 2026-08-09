"""Unit tests for check_deploy_health.py's pure logic: status
classification and the alert-on-transition state machine. No network
calls are made in these tests -- query_railway_deployment and
post_to_discord are exercised separately via monkeypatched urllib calls,
never real HTTP.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from check_deploy_health import (  # noqa: E402
    build_discord_message,
    classify_status,
    determine_action,
    read_state,
    write_state,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("SUCCESS", "healthy"),
        ("BUILDING", "transient"),
        ("DEPLOYING", "transient"),
        ("QUEUED", "transient"),
        ("WAITING", "transient"),
        ("SKIPPED", "transient"),
        ("FAILED", "unhealthy"),
        ("CRASHED", "unhealthy"),
        ("REMOVED", "unhealthy"),
        ("SLEEPING", "unhealthy"),
        ("SOME_FUTURE_STATUS_RAILWAY_ADDS_LATER", "unhealthy"),
    ],
)
def test_classify_status(status: str, expected: str) -> None:
    assert classify_status(status) == expected


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (None, "healthy", "none"),
        (None, "unhealthy", "alert"),
        (None, "check_failed", "alert"),
        ("healthy", "healthy", "none"),
        ("healthy", "unhealthy", "alert"),
        ("healthy", "check_failed", "alert"),
        ("unhealthy", "unhealthy", "none"),
        ("unhealthy", "healthy", "recovery"),
        ("unhealthy", "check_failed", "alert"),
        ("check_failed", "check_failed", "none"),
        ("check_failed", "healthy", "recovery"),
        ("check_failed", "unhealthy", "alert"),
    ],
)
def test_determine_action(previous: str | None, current: str, expected: str) -> None:
    assert determine_action(previous, current) == expected


def test_read_state_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert read_state(tmp_path / "does-not-exist.json") is None


def test_read_state_returns_none_when_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not valid json", encoding="utf-8")
    assert read_state(path) is None


def test_write_state_then_read_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_state(
        path,
        classification="unhealthy",
        deployment_id="dep-123",
        checked_at="2026-08-08T19:45:00Z",
    )
    state = read_state(path)
    assert state is not None
    assert state["classification"] == "unhealthy"
    assert state["deployment_id"] == "dep-123"
    assert state["checked_at"] == "2026-08-08T19:45:00Z"


def test_build_discord_message_alert_mentions_status() -> None:
    message = build_discord_message(
        "alert", status="CRASHED", deployment_id="dep-123"
    )
    assert "CRASHED" in message
    assert "dep-123" in message


def test_build_discord_message_recovery_does_not_sound_like_a_failure() -> None:
    message = build_discord_message(
        "recovery", status="SUCCESS", deployment_id="dep-456"
    )
    assert "SUCCESS" in message
    assert "recover" in message.lower() or "healthy" in message.lower() or "back" in message.lower()


def test_build_discord_message_check_failed_is_distinguishable_from_unhealthy() -> None:
    alert_message = build_discord_message(
        "alert", status="check_failed", deployment_id=None, detail="401 Unauthorized"
    )
    assert "check" in alert_message.lower()
    assert "401" in alert_message
