"""Polls Railway's API for Krubit's deployment status and posts to a
Discord webhook on failure and recovery.

Standalone script, no dependency on the `krubit` package -- runs as a
GitHub Actions step, not as part of the bot's own runtime. Uses only the
Python standard library (urllib, json) since this is too small a job to
justify a new project dependency.

Alerts only on a classification transition (see `determine_action`), so a
still-broken deployment doesn't spam a fresh message every 15-minute poll
-- exactly one alert per incident start, one per recovery. A Railway API
call that itself fails (bad token, network error, malformed response) is
its own `check_failed` classification, distinct from `unhealthy`, so a
broken checker never looks identical to "everything is fine."
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
_PROJECT_ID = "6ee1417e-f70d-486c-bebf-621bc5c8fd62"
_ENVIRONMENT_ID = "f530bd42-cd24-4e03-b5f1-4d1c33fbb28d"
_SERVICE_ID = "90238c9f-2a9d-41e7-aee5-7f9c0e2595bf"
_RAILWAY_PROJECT_URL = f"https://railway.com/project/{_PROJECT_ID}"
# Python's default urllib User-Agent ("Python-urllib/3.x") is a well-known
# bot signature that Cloudflare-fronted APIs (including Railway's) commonly
# reject outright with a 403 before the request ever reaches the actual
# GraphQL resolver -- sending a descriptive one avoids that class of
# false-negative "the token/query is fine but we got blocked anyway" failure.
_USER_AGENT = "krubit-deploy-health-check/1.0 (+https://github.com/ClydeMcNeil25/Krubit)"

_STATE_PATH = Path(__file__).parent.parent / "krubit-health-state.json"

_HEALTHY_STATUSES = frozenset({"SUCCESS"})
_TRANSIENT_STATUSES = frozenset({"BUILDING", "DEPLOYING", "QUEUED", "WAITING", "SKIPPED"})
# Every other status (FAILED, CRASHED, REMOVED, SLEEPING, and anything
# Railway might add later that isn't in the two sets above) is treated as
# unhealthy -- see classify_status's docstring for why an unrecognized
# status is never silently ignored.

_DEPLOYMENTS_QUERY = """
query deployments($input: DeploymentListInput!, $first: Int) {
  deployments(input: $input, first: $first) {
    edges {
      node {
        id
        status
        createdAt
      }
    }
  }
}
"""


def classify_status(status: str) -> str:
    """Map a raw Railway deployment status string to `"healthy"`,
    `"unhealthy"`, or `"transient"`.

    An unrecognized status (not in either the healthy or transient sets)
    is conservatively classified `"unhealthy"` -- if Railway ever adds a
    new status this script doesn't know about, silently treating it as
    fine would be exactly the failure mode this whole check exists to
    prevent.
    """
    if status in _HEALTHY_STATUSES:
        return "healthy"
    if status in _TRANSIENT_STATUSES:
        return "transient"
    return "unhealthy"


def determine_action(previous: str | None, current: str) -> str:
    """Decide whether this poll should alert, given the last *stored*
    classification (`previous`, `None` on the very first run) and the
    current one (`current` is always `"healthy"`, `"unhealthy"`, or
    `"check_failed"` -- the caller must never pass `"transient"` here,
    since transient states are filtered out before reaching this
    function and must never overwrite stored state).

    Returns `"none"` (no state change, no alert), `"alert"` (state is now
    bad, wasn't before, or changed to a different kind of bad), or
    `"recovery"` (state is now healthy after having been bad).
    """
    if current == previous:
        return "none"
    if current == "healthy":
        return "recovery" if previous in ("unhealthy", "check_failed") else "none"
    return "alert"


def read_state(path: Path) -> dict[str, object] | None:
    """Read the committed state file. Returns `None` if it doesn't exist
    yet (first-ever run) or is malformed (never crash the workflow over a
    corrupted state file -- treat it the same as "no prior state")."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def write_state(
    path: Path, *, classification: str, deployment_id: str | None, checked_at: str
) -> None:
    path.write_text(
        json.dumps(
            {
                "classification": classification,
                "deployment_id": deployment_id,
                "checked_at": checked_at,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_discord_message(
    action: str, *, status: str, deployment_id: str | None, detail: str | None = None
) -> str:
    """Plain, factual message text -- no personality/dramatized language,
    matching this codebase's established tone for member-facing cards,
    applied here to ops messaging."""
    if action == "recovery":
        return (
            f"Krubit's deployment has recovered (status: {status}, "
            f"deployment {deployment_id}). Back to healthy."
        )
    if status == "check_failed":
        return (
            "Krubit's deploy health check itself failed and could not "
            f"determine the deployment's real status. Detail: {detail}. "
            "This does not necessarily mean Krubit is down -- it means "
            "this checker could not verify either way."
        )
    return (
        f"Krubit's deployment is unhealthy (status: {status}, "
        f"deployment {deployment_id}). See {_RAILWAY_PROJECT_URL}"
    )


def query_railway_deployment(token: str) -> tuple[str, str, str]:
    """Query Railway's GraphQL API for the latest deployment's status, id,
    and createdAt timestamp. Raises `RuntimeError` (never a raw
    urllib/json exception) on any failure, with a short, non-sensitive
    detail message -- the token itself must never appear in a raised
    message, since that could end up logged."""
    body = json.dumps(
        {
            "query": _DEPLOYMENTS_QUERY,
            "variables": {
                "input": {
                    "projectId": _PROJECT_ID,
                    "serviceId": _SERVICE_ID,
                    "environmentId": _ENVIRONMENT_ID,
                },
                "first": 1,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _RAILWAY_GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Railway API returned HTTP {exc.code}") from exc
    except OSError as exc:
        # Covers URLError, TimeoutError, ConnectionError, and other
        # network-layer failures -- none of these can contain the token
        # (it lives only in the Authorization header, never in these
        # exception types' string forms).
        raise RuntimeError(f"Railway API request failed: {exc}") from exc
    except ValueError as exc:
        # Covers json.JSONDecodeError and UnicodeDecodeError (both are
        # ValueError subclasses) -- a non-JSON or non-UTF8 response body.
        raise RuntimeError("Railway API returned an unreadable response") from exc

    if "errors" in payload:
        raise RuntimeError(f"Railway API returned GraphQL errors: {payload['errors']}")
    try:
        edges = payload["data"]["deployments"]["edges"]
        if not edges:
            raise RuntimeError("Railway API returned no deployments for this service")
        node = edges[0]["node"]
        return str(node["status"]), str(node["id"]), str(node["createdAt"])
    except (KeyError, TypeError, IndexError) as exc:
        raise RuntimeError("Railway API response had an unexpected shape") from exc


def post_to_discord(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def main() -> int:
    token = os.environ.get("RAILWAY_API_TOKEN")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not token or not webhook_url:
        print("RAILWAY_API_TOKEN and DISCORD_WEBHOOK_URL must both be set", file=sys.stderr)
        return 1

    now = datetime.now(UTC).isoformat()
    previous_state = read_state(_STATE_PATH)
    previous_classification = (
        previous_state["classification"] if previous_state is not None else None
    )

    detail: str | None = None
    deployment_id: str | None = None
    created_at: str | None = None
    try:
        status, deployment_id, created_at = query_railway_deployment(token)
        classification = classify_status(status)
    except RuntimeError as exc:
        status = "check_failed"
        classification = "check_failed"
        detail = str(exc)

    if classification == "transient":
        print(f"status={status} classification=transient, no action")
        return 0

    action = determine_action(previous_classification, classification)
    print(
        f"status={status} classification={classification} action={action} "
        f"deployment_created_at={created_at} detail={detail}"
    )

    if action == "none":
        return 0

    message = build_discord_message(
        action, status=status, deployment_id=deployment_id, detail=detail
    )
    post_to_discord(webhook_url, message)
    write_state(
        _STATE_PATH,
        classification=classification,
        deployment_id=deployment_id,
        checked_at=now,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
