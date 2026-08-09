# Deploy Health Alerting Design

**Date:** 2026-08-08
**Status:** Approved for implementation planning
**Scope:** An external, scheduled check that detects a broken Krubit deployment on Railway and alerts a Discord channel — closing the gap exposed by today's outage, where Railway's own crash-loop detection fired correctly but only emailed, and nobody saw the email in time.

## Context

Krubit went down because Nixpacks changed its own build recipe, breaking the `startCommand`'s hardcoded venv path. Railway detected the crash-loop correctly and had an active "Deployment Crashed" email/in-app notification rule — but the outage still went unnoticed for hours, because email isn't where anyone is actually looking, and Railway's notification system has no native Discord/Slack webhook option (confirmed by inspecting its Notification Rules UI — only Email, In-App, or both).

Krubit's own code cannot solve this: the failure was the container never starting Python at all, so nothing running *inside* Krubit's process could have observed or reported it. The check has to live outside Krubit's deployment entirely.

## Confirmed decisions (from conversation)

1. **Mechanism: a scheduled GitHub Actions workflow**, not a change to Krubit's runtime code, not a new HTTP endpoint on the bot, not a third-party uptime service. Polls Railway's public GraphQL API for the Krubit service's deployment status.
2. **Poll interval: 15 minutes.** GitHub Actions' free tier easily supports this; user explicitly traded faster detection for lower overhead.
3. **Alerts on both failure and recovery** — a second Discord message when the deployment returns to healthy, closing the loop without requiring a manual check.
4. **Status classification:**
   - Healthy: `SUCCESS`
   - Transient (ignored, no alert, no state change): `BUILDING`, `DEPLOYING`, `QUEUED`, `WAITING`, `SKIPPED` — these occur on every routine redeploy and must never trigger a false alarm
   - Unhealthy (alerts): `FAILED`, `CRASHED`, `REMOVED`, `SLEEPING` — `SLEEPING` is included because a Discord bot has no legitimate idle state; if Railway's idle-sleep ever applies here, the bot has silently disconnected from Discord's gateway exactly as if it had crashed
5. **Alert only on state transition, not every poll.** A dedicated, committed JSON state file tracks the last-known classification; a poll that doesn't change the classification does nothing (no Discord post, no commit). This bounds the alert volume to one message per incident start and one per recovery, never a message every 15 minutes while down.
6. **The checker's own failure is not silent.** If the Railway API call itself fails (bad/expired token, Railway API outage, network error), the workflow posts a distinct "health check itself failed" Discord message rather than doing nothing — an unreadable checker must not look identical to "everything is fine."

## Railway API details (verified against Railway's current docs, not assumed)

- Endpoint: `https://backboard.railway.com/graphql/v2`
- Auth: `Authorization: Bearer <RAILWAY_API_TOKEN>` header
- Query:
  ```graphql
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
  ```
  `input` takes `projectId`, `serviceId`, `environmentId`; `first: 1` returns just the latest deployment.
- The three IDs (project `6ee1417e-f70d-486c-bebf-621bc5c8fd62`, environment `f530bd42-cd24-4e03-b5f1-4d1c33fbb28d`, service `90238c9f-2a9d-41e7-aee5-7f9c0e2595bf`) are plain identifiers, not secrets — they were already visible in plaintext in the deploy logs pasted earlier in this project's history, and are safe to commit directly in the workflow file.

## Secrets required (user-provisioned, never handled by the assistant)

- `RAILWAY_API_TOKEN` — Railway Account → Tokens, a personal access token
- `DISCORD_WEBHOOK_URL` — the target Discord channel's Integrations → Webhooks → New Webhook
- Both added as GitHub repository secrets: Settings → Secrets and variables → Actions → New repository secret

## Implementation approach

- **New file: `.github/workflows/krubit-health-check.yml`** — a `schedule: cron` trigger (every 15 minutes) plus `workflow_dispatch` (manual trigger, useful for testing the workflow without waiting for the next scheduled tick).
- **New file: `.github/scripts/check_deploy_health.py`** — the actual GraphQL call, status classification, state-file comparison, and Discord webhook POST. A plain Python script (not embedded YAML shell), so its classification and transition logic can be unit-tested with `pytest` like the rest of this codebase, even though it's not part of the `krubit` package itself (it never imports from `src/krubit`, has no dependency on the bot's own runtime, and uses only the standard library plus `requests` or `urllib` for the two HTTP calls — no new project dependency needed for something this small).
- **New file: `.github/krubit-health-state.json`** — committed, tracks `{"status": "healthy" | "unhealthy", "last_deployment_id": "...", "last_checked_at": "..."}`. The workflow commits an update to this file only when the classification changes, using the workflow's own `GITHUB_TOKEN` (no new secret needed for this — `contents: write` permission on the default token is sufficient for a same-repo commit).
- **Discord message format:** a plain, factual message (this project's established convention — no personality/dramatized language, see `activity_commands.py`'s "No personality, loyalty, mental-health, or guilt language" precedent applied here to ops messaging too): on failure, state the deployment status and a link to the Railway project's deployments page; on recovery, state that the deployment returned to `SUCCESS`.

## Explicit Exclusions

- No changes to Krubit's own runtime code, `railway.toml`, or the bot's Discord command surface.
- No new HTTP endpoint exposed by Krubit.
- No third-party uptime-monitoring service signup.
- No attempt to detect Discord-gateway-level failures (e.g. Krubit's process is alive per Railway but has lost its gateway connection) — this is scoped to Railway's own deployment-status signal only, which covers today's actual failure class. A gateway-level check would need Krubit-side instrumentation and is a larger, separate scope if ever needed.

## Testing

- Unit tests for the status-classification function (each of the 9 possible Railway statuses maps to the correct bucket) and the state-transition logic (healthy→healthy no-op, healthy→unhealthy alerts, unhealthy→unhealthy no-op, unhealthy→healthy alerts/recovery) — pure functions, no network calls, run via the existing `pytest` setup even though this script lives outside `src/krubit`.
- A manually-triggered `workflow_dispatch` run against the real Railway API (using the real secrets once configured) is the practical end-to-end verification step, since mocking Railway's GraphQL API for a true integration test is out of proportion for this scope.

## Completion Gate

Complete when: the workflow runs on schedule and on manual dispatch; a real Railway API call against the live Krubit service returns and classifies correctly; the state file correctly suppresses repeat alerts and correctly fires exactly once on a genuine transition; the Discord webhook message is legible and links back to Railway; the checker's own failure path (bad token) has been exercised at least once and confirmed to post its own distinct alert rather than silently doing nothing.
