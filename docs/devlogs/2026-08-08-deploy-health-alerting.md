# Krubit Development Log: Automated Deploy Health Alerting

**Date:** August 8-10, 2026
**Status:** Implementation, fix wave, and final review complete. Merged and live-verified against real Railway/Discord credentials.

## Scope

Krubit's Railway deployment went down for hours (a Nixpacks build-recipe
change broke `startCommand`) without anyone noticing — Railway's own
"Deployment Crashed" email fired correctly, but nobody saw it. This
project closes that gap: a standalone script polling Railway's API for
Krubit's deployment status, alerting a Discord webhook on failure and
recovery transitions, run on a schedule by GitHub Actions. Deliberately
external to Krubit's own deployment (a container that never starts
Python at all can't be detected by anything running inside it) — see
[the design spec](../superpowers/specs/2026-08-08-deploy-health-alerting-design.md).

## Delivered

- `.github/scripts/check_deploy_health.py` — classifies a Railway
  deployment status into `healthy`/`unhealthy`/`transient`/
  `check_failed`, alerts only on an actual state transition (never spams
  every 15-minute poll), and treats a failed Railway API call itself as
  its own distinct `check_failed` classification so a broken checker
  never looks identical to "everything is fine."
- `.github/workflows/krubit-health-check.yml` — cron every 15 minutes
  plus manual dispatch, committing the state file back only when it
  changes.

## Final review and fix wave

The final whole-branch review found one Important gap: several realistic
network failures (`TimeoutError`, `ConnectionResetError`, a non-UTF8
response body) escaped the script's exception handling entirely, meaning
a degraded-but-reachable Railway API produced an unhandled crash and
**zero Discord message** — precisely the "signal fires but nobody sees
it" failure this whole project exists to prevent. Fixed by broadening the
caught exception types (`OSError`/`ValueError` supersets covering the
specific ones originally handled) while confirming the token could still
never leak into any exception message. Also added `main()` orchestration
tests (the transient-filtering and write-only-on-transition guarantees
had no test coverage of their own) and a `read_state` guard against
valid-but-non-dict JSON.

## Live verification (post-merge)

Setting the real credentials up hit two real snags, resolved during
actual live testing:

1. **A 403 from Discord's webhook** — traced to the `DISCORD_WEBHOOK_URL`
   GitHub secret not actually holding the webhook's current value (fixed
   by re-copying it fresh).
2. **A 403 from Railway's API** — traced to Python's default
   `urllib` User-Agent (`Python-urllib/3.x`) being a well-known bot
   signature that Cloudflare-fronted APIs commonly reject outright,
   independent of whether the token itself was valid. Fixed by sending a
   descriptive `User-Agent` header on both the Railway and Discord
   requests (`fix: send a descriptive User-Agent on Railway/Discord
   requests`).

After both fixes, a live `workflow_dispatch` run correctly reported
`status=SUCCESS classification=healthy action=none` with a
`deployment_created_at` timestamp matching Railway's actual latest
deploy — confirming the check is looking at the real, current
deployment, not a stale one.

## Test evidence

Full suite: 42/42 script-specific tests passing (run via explicit path,
since `pyproject.toml`'s `testpaths` doesn't cover `.github/`). Ruff
clean throughout. Live end-to-end run confirmed working against real
Railway and Discord credentials.
