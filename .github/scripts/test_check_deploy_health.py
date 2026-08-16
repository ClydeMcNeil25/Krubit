"""Unit tests for check_deploy_health.py's pure logic: status
classification and the alert-on-transition state machine. No network
calls are made in these tests -- query_railway_deployment and
post_to_discord are exercised separately via monkeypatched urllib calls,
never real HTTP.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import check_deploy_health  # noqa: E402
from check_deploy_health import (  # noqa: E402
    build_discord_message,
    classify_status,
    determine_action,
    main,
    post_to_discord,
    query_railway_deployment,
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


def test_read_state_returns_none_when_json_is_not_a_dict(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps("healthy"), encoding="utf-8")
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


# Tests for query_railway_deployment with mocked urllib
def test_query_railway_deployment_success() -> None:
    """Test successful deployment query returns (status, id) tuple."""
    token = "secret-token-xyz123"
    mock_response_data = {
        "data": {
            "deployments": {
                "edges": [
                    {
                        "node": {
                            "id": "dep-123",
                            "status": "SUCCESS",
                            "createdAt": "2026-08-08T19:45:00Z",
                        }
                    }
                ]
            }
        }
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_response_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        status, deployment_id, created_at = query_railway_deployment(token)

        assert status == "SUCCESS"
        assert deployment_id == "dep-123"
        assert created_at == "2026-08-08T19:45:00Z"
        mock_urlopen.assert_called_once()


def test_query_railway_deployment_http_error_raises_runtime_error() -> None:
    """Test that HTTPError raises RuntimeError and does not leak the token."""
    token = "secret-token-xyz123"

    mock_error = urllib.error.HTTPError(
        url="https://railway.com",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=mock_error):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        error_message = str(exc_info.value)
        assert "HTTP" in error_message
        assert token not in error_message, (
            f"Token leaked into error message: {error_message}"
        )


def test_query_railway_deployment_url_error_raises_runtime_error() -> None:
    """Test that URLError raises RuntimeError and does not leak the token."""
    token = "secret-token-xyz123"

    mock_error = urllib.error.URLError("Connection refused")

    with patch("urllib.request.urlopen", side_effect=mock_error):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        error_message = str(exc_info.value)
        assert "request failed" in error_message.lower()
        assert token not in error_message, (
            f"Token leaked into error message: {error_message}"
        )


def test_query_railway_deployment_timeout_error_raises_runtime_error() -> None:
    """Test that a raw TimeoutError (not wrapped in URLError, as urlopen
    can raise directly on socket timeouts) is caught and converted to
    RuntimeError instead of propagating and crashing the checker."""
    token = "secret-token-xyz123"

    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        error_message = str(exc_info.value)
        assert "request failed" in error_message.lower()
        assert token not in error_message, (
            f"Token leaked into error message: {error_message}"
        )


def test_query_railway_deployment_non_utf8_response_raises_runtime_error() -> None:
    """Test that a response body that fails UTF-8 decoding (UnicodeDecodeError,
    a ValueError subclass) is caught and converted to RuntimeError."""
    token = "secret-token-xyz123"

    mock_response = MagicMock()
    mock_response.read.return_value = b"\xff\xfe not valid utf-8"
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        error_message = str(exc_info.value)
        assert "unreadable" in error_message.lower()
        assert token not in error_message, (
            f"Token leaked into error message: {error_message}"
        )


def test_query_railway_deployment_graphql_errors() -> None:
    """Test that GraphQL errors in response raise RuntimeError and do not leak the token."""
    token = "secret-token-xyz123"
    mock_response_data = {
        "errors": [
            {"message": "Invalid query", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}
        ]
    }
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_response_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        error_message = str(exc_info.value)
        assert "GraphQL errors" in error_message
        assert "Invalid query" in error_message
        assert token not in error_message, (
            f"Token leaked into error message: {error_message}"
        )


def test_query_railway_deployment_empty_edges_list() -> None:
    """Test that empty edges list raises RuntimeError."""
    token = "secret-token-xyz123"
    mock_response_data = {"data": {"deployments": {"edges": []}}}
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_response_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        assert "no deployments" in str(exc_info.value).lower()


def test_query_railway_deployment_unexpected_response_shape() -> None:
    """Test that unexpected response shape raises RuntimeError, not KeyError."""
    token = "secret-token-xyz123"
    mock_response_data = {"unexpected": "structure"}
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(mock_response_data).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            query_railway_deployment(token)

        assert "unexpected shape" in str(exc_info.value).lower()


# Tests for post_to_discord with mocked urllib
def test_post_to_discord_constructs_correct_request() -> None:
    """Test that post_to_discord constructs POST request with correct body and headers."""
    webhook_url = "https://discord.com/api/webhooks/123/abc"
    content = "Test alert message"

    mock_response = MagicMock()
    mock_response.read.return_value = b""
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen, patch(
        "urllib.request.Request"
    ) as mock_request_class:
        mock_request_instance = MagicMock()
        mock_request_class.return_value = mock_request_instance

        post_to_discord(webhook_url, content)

        # Verify Request was instantiated with correct arguments
        mock_request_class.assert_called_once()
        call_args = mock_request_class.call_args

        assert call_args[0][0] == webhook_url
        assert call_args[1]["method"] == "POST"
        assert call_args[1]["headers"]["Content-Type"] == "application/json"

        # Verify the body contains the correct JSON
        body = call_args[1]["data"]
        body_dict = json.loads(body.decode("utf-8"))
        assert body_dict["content"] == content

        # Verify urlopen was called with the request and timeout
        mock_urlopen.assert_called_once()
        assert mock_urlopen.call_args[0][0] == mock_request_instance
        assert mock_urlopen.call_args[1]["timeout"] == 15


# Integration tests for main()'s orchestration -- query_railway_deployment
# and post_to_discord are always mocked here, so these never make a real
# network call. _STATE_PATH is patched to a tmp_path file so tests never
# touch the real committed state file.
def _seed_state(path: Path, *, classification: str) -> None:
    write_state(
        path,
        classification=classification,
        deployment_id="dep-previous",
        checked_at="2026-08-08T00:00:00Z",
    )


def test_main_transient_does_not_clobber_stored_state(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    _seed_state(state_path, classification="unhealthy")

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake")
    monkeypatch.setattr(check_deploy_health, "_STATE_PATH", state_path)

    with patch.object(
        check_deploy_health,
        "query_railway_deployment",
        return_value=("BUILDING", "dep-new", "2026-08-09T00:00:00Z"),
    ), patch.object(check_deploy_health, "post_to_discord") as mock_post:
        result = main()

    assert result == 0
    mock_post.assert_not_called()
    state = read_state(state_path)
    assert state is not None
    assert state["classification"] == "unhealthy"
    assert state["deployment_id"] == "dep-previous"


def test_main_no_op_poll_does_not_write_or_alert(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    _seed_state(state_path, classification="healthy")
    before_mtime = state_path.stat().st_mtime_ns
    before_content = state_path.read_text(encoding="utf-8")

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake")
    monkeypatch.setattr(check_deploy_health, "_STATE_PATH", state_path)

    with patch.object(
        check_deploy_health,
        "query_railway_deployment",
        return_value=("SUCCESS", "dep-new", "2026-08-09T00:00:00Z"),
    ), patch.object(check_deploy_health, "post_to_discord") as mock_post:
        result = main()

    assert result == 0
    mock_post.assert_not_called()
    assert state_path.stat().st_mtime_ns == before_mtime
    assert state_path.read_text(encoding="utf-8") == before_content


def test_main_discord_post_failure_does_not_crash_and_leaves_state_for_retry(
    tmp_path, monkeypatch
) -> None:
    """A Discord webhook delivery failure (e.g. Cloudflare 403) must not
    crash the workflow step -- that would fire GitHub's own failed-workflow
    email instead of the Discord alert, and silently discard the alert.
    State must be left unwritten so the next poll sees the same transition
    and retries the alert, rather than recording the incident as "announced"
    when it never actually reached Discord."""
    state_path = tmp_path / "state.json"
    _seed_state(state_path, classification="healthy")

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake")
    monkeypatch.setattr(check_deploy_health, "_STATE_PATH", state_path)

    mock_error = urllib.error.HTTPError(
        url="https://discord.com", code=403, msg="Forbidden", hdrs={}, fp=None
    )

    with patch.object(
        check_deploy_health,
        "query_railway_deployment",
        return_value=("CRASHED", "dep-new", "2026-08-09T00:00:00Z"),
    ), patch.object(check_deploy_health, "post_to_discord", side_effect=mock_error):
        result = main()

    assert result == 0
    state = read_state(state_path)
    assert state is not None
    # State must still reflect the last *successfully announced* classification,
    # not the new one -- since the alert for it never actually got delivered.
    assert state["classification"] == "healthy"


def test_main_real_transition_posts_and_writes_state(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    _seed_state(state_path, classification="healthy")

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake")
    monkeypatch.setattr(check_deploy_health, "_STATE_PATH", state_path)

    with patch.object(
        check_deploy_health,
        "query_railway_deployment",
        return_value=("CRASHED", "dep-new", "2026-08-09T00:00:00Z"),
    ), patch.object(check_deploy_health, "post_to_discord") as mock_post:
        result = main()

    assert result == 0
    mock_post.assert_called_once()
    posted_message = mock_post.call_args[0][1]
    assert "CRASHED" in posted_message

    state = read_state(state_path)
    assert state is not None
    assert state["classification"] == "unhealthy"
    assert state["deployment_id"] == "dep-new"
