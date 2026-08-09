# Deploy Health Alerting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled GitHub Actions workflow that polls Railway's API for
Krubit's deployment status and posts to a Discord webhook on failure and
recovery, so an outage like today's (Railway's own email alert went
unnoticed for hours) gets caught in a channel someone actually watches.

**Architecture:** A standalone Python script
(`.github/scripts/check_deploy_health.py`, no dependency on the `krubit`
package) does one GraphQL call to Railway, classifies the result into
`healthy` / `unhealthy` / `check_failed` / `transient`, compares against a
committed JSON state file, and — only on an actual state transition —
posts to a Discord webhook and updates the state file. A GitHub Actions
workflow (`.github/workflows/krubit-health-check.yml`) runs it on a
15-minute cron plus manual dispatch.

**Tech Stack:** Python 3.13 standard library only (`urllib.request`,
`json`) for the script — no new project dependency. `pytest` for the
script's own tests (run via an explicit path, not swept into the main
`tests/` suite, since `pyproject.toml`'s `testpaths = ["tests"]` doesn't
cover `.github/`). GitHub Actions YAML for scheduling.

## Global Constraints

- Status classification: `SUCCESS` → healthy. `BUILDING`, `DEPLOYING`,
  `QUEUED`, `WAITING`, `SKIPPED` → transient (never stored, never alerts).
  `FAILED`, `CRASHED`, `REMOVED`, `SLEEPING` → unhealthy. Any other/unknown
  status string → unhealthy (conservative default — never silently ignore
  an unrecognized state).
- A Railway API call that itself fails (network error, non-200, malformed
  response) produces a distinct `check_failed` classification — this is
  not the same as `unhealthy` and must be visually distinguishable in the
  Discord message, but participates in the same alert-once-per-transition
  state machine.
- Alert only on a classification transition (compared against the last
  *stored* classification — `transient` is filtered out before ever
  reaching the state comparison, so it can never overwrite a stored
  `unhealthy`/`check_failed`/`healthy` value). No alert on the very first
  run if the first-ever observed classification is `healthy` (nothing to
  "recover" from). An alert fires on first run if the first-ever
  observation is `unhealthy` or `check_failed`.
- Railway GraphQL endpoint: `https://backboard.railway.com/graphql/v2`,
  `Authorization: Bearer <token>` header.
- Project ID `6ee1417e-f70d-486c-bebf-621bc5c8fd62`, environment ID
  `f530bd42-cd24-4e03-b5f1-4d1c33fbb28d`, service ID
  `90238c9f-2a9d-41e7-aee5-7f9c0e2595bf` — plain identifiers, not secrets,
  hardcoded as script constants.
- Secrets (`RAILWAY_API_TOKEN`, `DISCORD_WEBHOOK_URL`) come from GitHub
  Actions secrets via environment variables — never hardcoded, never
  logged.
- Discord messages are plain, factual text — no personality/dramatized
  language, matching this codebase's established tone for member-facing
  cards, applied here to ops messaging.

---

### Task 1: `check_deploy_health.py` — classification, state machine, and script logic

**Files:**
- Create: `.github/scripts/check_deploy_health.py`
- Test: `.github/scripts/test_check_deploy_health.py`

**Interfaces:**
- Produces: `classify_status(status: str) -> str` (returns `"healthy"`,
  `"unhealthy"`, or `"transient"`)
- Produces: `determine_action(previous: str | None, current: str) -> str`
  (returns `"none"`, `"alert"`, or `"recovery"`; `current` is always one
  of `"healthy"`, `"unhealthy"`, `"check_failed"` — never `"transient"`,
  the caller filters that out before calling this)
- Produces: `read_state(path: Path) -> dict | None`, `write_state(path:
  Path, *, classification: str, deployment_id: str | None, checked_at:
  str) -> None`
- Produces: `build_discord_message(action: str, *, status: str,
  deployment_id: str | None, detail: str | None = None) -> str`
- Produces: `query_railway_deployment(token: str) -> tuple[str, str]` (raises
  `RuntimeError` on any failure; returns `(status, deployment_id)` on
  success)
- Produces: `post_to_discord(webhook_url: str, content: str) -> None`
- Produces: `main() -> int` (the script's entry point, reads env vars,
  orchestrates the above, returns a process exit code)

- [ ] **Step 1: Write the failing tests**

Create `.github/scripts/test_check_deploy_health.py`:

```python
"""Unit tests for check_deploy_health.py's pure logic: status
classification and the alert-on-transition state machine. No network
calls are made in these tests -- query_railway_deployment and
post_to_discord are exercised separately via monkeypatched urllib calls,
never real HTTP.
"""

from __future__ import annotations

import json
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest .github/scripts/test_check_deploy_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'check_deploy_health'`

- [ ] **Step 3: Implement `check_deploy_health.py`**

Create `.github/scripts/check_deploy_health.py`:

```python
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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


def query_railway_deployment(token: str) -> tuple[str, str]:
    """Query Railway's GraphQL API for the latest deployment's status and
    id. Raises `RuntimeError` (never a raw urllib/json exception) on any
    failure, with a short, non-sensitive detail message -- the token
    itself must never appear in a raised message, since that could end up
    logged."""
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
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Railway API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Railway API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Railway API returned a non-JSON response") from exc

    if "errors" in payload:
        raise RuntimeError(f"Railway API returned GraphQL errors: {payload['errors']}")
    try:
        edges = payload["data"]["deployments"]["edges"]
        if not edges:
            raise RuntimeError("Railway API returned no deployments for this service")
        node = edges[0]["node"]
        return str(node["status"]), str(node["id"])
    except (KeyError, TypeError, IndexError) as exc:
        raise RuntimeError("Railway API response had an unexpected shape") from exc


def post_to_discord(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
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
    try:
        status, deployment_id = query_railway_deployment(token)
        classification = classify_status(status)
    except RuntimeError as exc:
        status = "check_failed"
        classification = "check_failed"
        detail = str(exc)

    if classification == "transient":
        print(f"status={status} classification=transient, no action")
        return 0

    action = determine_action(previous_classification, classification)
    print(f"status={status} classification={classification} action={action}")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest .github/scripts/test_check_deploy_health.py -v`
Expected: all PASS

- [ ] **Step 5: Lint**

Run: `./.venv/Scripts/python.exe -m ruff check .github/scripts/`
Expected: clean. `pyproject.toml`'s `[tool.ruff]` section has no
`include`/`exclude` restriction, so it applies repo-wide by default and
will pick up this new directory without any config change. Fix any
findings; do not modify `pyproject.toml`.

- [ ] **Step 6: Commit**

```bash
git add .github/scripts/check_deploy_health.py .github/scripts/test_check_deploy_health.py
git commit -m "feat: add deploy health check script with alert-on-transition logic"
```

---

### Task 2: GitHub Actions workflow and initial state file

**Files:**
- Create: `.github/workflows/krubit-health-check.yml`
- Create: `.github/krubit-health-state.json` (initial placeholder state)

**Interfaces:**
- Consumes: `.github/scripts/check_deploy_health.py`'s `main()` (Task 1) as
  a script invoked by the workflow; `RAILWAY_API_TOKEN` and
  `DISCORD_WEBHOOK_URL` GitHub Actions secrets (user-provisioned, not
  created by this task).

This task has no automated test — it's CI configuration, verified by
actually running it (Step 4 below) rather than by `pytest`.

- [ ] **Step 1: Create the initial state file**

Create `.github/krubit-health-state.json`:

```json
{
  "classification": "healthy",
  "deployment_id": null,
  "checked_at": null
}
```

Seeding this as `"healthy"` (rather than leaving the file absent) means
the very first scheduled run treats today's already-resolved incident as
the known-good baseline, and won't fire a spurious "recovery" message for
an incident that's already over and already been manually confirmed
fixed earlier in this conversation.

- [ ] **Step 2: Write the workflow**

Create `.github/workflows/krubit-health-check.yml`:

```yaml
name: Krubit Deploy Health Check

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Run health check
        id: check
        env:
          RAILWAY_API_TOKEN: ${{ secrets.RAILWAY_API_TOKEN }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: python .github/scripts/check_deploy_health.py

      - name: Commit updated state file if changed
        run: |
          if ! git diff --quiet -- .github/krubit-health-state.json; then
            git config user.name "github-actions[bot]"
            git config user.email "github-actions[bot]@users.noreply.github.com"
            git add .github/krubit-health-state.json
            git commit -m "chore: update deploy health state"
            git push
          else
            echo "No state change, nothing to commit"
          fi
```

Note: `check_deploy_health.py`'s `write_state` only runs when `action !=
"none"`, so most scheduled runs (deployment still healthy, no change) exit
without touching the state file at all -- the "Commit updated state file
if changed" step's `git diff --quiet` check is what keeps those routine
runs from creating empty commits.

- [ ] **Step 3: Verify the workflow file is valid YAML and the script runs standalone**

Run (no live secrets needed for this local sanity check — it should fail
cleanly with the "must both be set" message from Step 4 of Task 1, not
crash with a traceback):

```bash
./.venv/Scripts/python.exe .github/scripts/check_deploy_health.py
```

Expected: prints `RAILWAY_API_TOKEN and DISCORD_WEBHOOK_URL must both be
set` to stderr, exits with code 1. This confirms the script itself is
syntactically valid and its env-var guard works, without needing real
credentials yet.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/krubit-health-check.yml .github/krubit-health-state.json
git commit -m "feat: add scheduled deploy health check workflow"
```

---

## Final Verification (requires the user's own Railway/Discord credentials — cannot be done by an agent)

- [ ] User creates a Railway personal access token (Account → Tokens) and
      adds it as the `RAILWAY_API_TOKEN` GitHub repository secret.
- [ ] User creates a Discord webhook in the target channel and adds its
      URL as the `DISCORD_WEBHOOK_URL` GitHub repository secret.
- [ ] Manually trigger the workflow via `workflow_dispatch` (GitHub's
      Actions tab → this workflow → "Run workflow") and confirm it
      completes successfully with the real Krubit deployment's actual
      status (should classify as `healthy`/`"none"` action if Krubit is
      currently up, per this conversation's earlier confirmation).
- [ ] Temporarily set `RAILWAY_API_TOKEN` to an invalid value, re-run
      manually, and confirm a distinct "check itself failed" message posts
      to Discord (proving the `check_failed` path works) — then restore
      the correct token value afterward.
- [ ] Confirm the scheduled `cron` trigger fires on its own within the
      next 15-30 minutes (check the Actions tab's run history) without
      requiring another manual dispatch.
