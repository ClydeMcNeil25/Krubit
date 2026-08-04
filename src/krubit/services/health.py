"""Factual operational health classification without community recommendations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from krubit.domain.companion import HealthFinding, HealthReport, SnapshotRecord
from krubit.domain.models import JSONValue

_SEVERITY = {"healthy": 0, "limited": 1, "warning": 2, "critical": 3}


def _dict(value: JSONValue | None) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], value) if isinstance(value, dict) else {}


def _strings(value: JSONValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value) if isinstance(item, str)]


def _report(findings: list[HealthFinding], checked_at: datetime) -> HealthReport:
    findings.sort(key=lambda item: (-_SEVERITY[item.severity], item.code))
    status = findings[0].severity if findings else "healthy"
    return HealthReport(status=status, findings=tuple(findings), checked_at=checked_at)


def _permission_findings(snapshot: SnapshotRecord) -> list[HealthFinding]:
    permissions = _dict(snapshot.content.get("bot_permissions"))
    missing = _strings(permissions.get("missing_required"))
    findings = [
        HealthFinding(
            "missing_required_permission",
            "warning",
            f"Missing required Discord permission: {name}.",
        )
        for name in missing
    ]
    unexpected = sorted(_strings(permissions.get("unexpected_mutation")))
    findings.extend(
        HealthFinding(
            "unexpected_mutation_permission",
            "warning",
            f"Discord role unexpectedly grants mutation permission: {name}.",
        )
        for name in unexpected
    )
    return findings


def _integration_findings(snapshot: SnapshotRecord) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    for issue in snapshot.coverage:
        code_section = {
            "automod_rules": "automod",
            "webhooks": "webhook",
        }.get(issue.section, issue.section)
        severity = "limited" if issue.status == "limited" else "warning"
        findings.append(
            HealthFinding(
                f"limited_{code_section}_coverage",
                severity,
                f"{issue.section} coverage is {issue.status}: {issue.detail}.",
            )
        )
    configured = _dict(snapshot.content.get("configured_channel"))
    if configured and configured.get("present") is False:
        findings.append(
            HealthFinding(
                "configured_channel_missing",
                "warning",
                f"Configured channel {configured.get('id')} is not present.",
            )
        )
    return findings


def _live_signal_findings(snapshot: SnapshotRecord) -> list[HealthFinding]:
    live = _dict(snapshot.content.get("live_signal"))
    if not live:
        return []
    findings: list[HealthFinding] = []
    channel = _dict(live.get("channel"))
    role = _dict(live.get("role"))
    if live.get("channel") is None:
        findings.append(
            HealthFinding(
                "live_channel_unconfigured",
                "warning",
                "No live notification channel is configured.",
            )
        )
    elif channel.get("present") is False:
        channel_id = str(channel.get("id", "unknown"))
        findings.append(
            HealthFinding(
                "live_channel_missing",
                "warning",
                f"Configured live notification channel {channel_id} is not present.",
            )
        )
    if live.get("role") is None:
        findings.append(
            HealthFinding("live_role_unconfigured", "warning", "No streaming role is configured.")
        )
    elif role.get("present") is False:
        role_id = str(role.get("id", "unknown"))
        findings.append(
            HealthFinding(
                "live_role_missing",
                "warning",
                f"Configured streaming role {role_id} is not present.",
            )
        )
    facts = (
        (
            "presence_intent",
            "live_presence_intent_missing",
            "Discord presence intent is not enabled.",
        ),
        (
            "twitch_credentials",
            "live_twitch_credentials_missing",
            "Twitch credentials are not configured.",
        ),
        ("twitch_available", "live_twitch_unavailable", "Twitch integration is not available."),
        (
            "manage_roles",
            "live_manage_roles_missing",
            "Discord Manage Roles permission is not granted.",
        ),
        (
            "role_hierarchy",
            "live_role_hierarchy_missing",
            "Streaming role is not below Krubit's top role.",
        ),
        (
            "mention_everyone",
            "live_mention_everyone_missing",
            "Discord Mention Everyone permission is not granted.",
        ),
    )
    findings.extend(
        HealthFinding(code, "warning", detail)
        for fact, code, detail in facts
        if live.get(fact) is False
    )
    return findings


class HealthService:
    def server_health(
        self,
        snapshot: SnapshotRecord | None,
        *,
        now: datetime,
        database_healthy: bool,
        gateway_ready: bool,
    ) -> HealthReport:
        findings: list[HealthFinding] = []
        if not database_healthy:
            findings.append(
                HealthFinding("database_unavailable", "critical", "Snapshot database unavailable.")
            )
        if not gateway_ready:
            findings.append(
                HealthFinding("gateway_unavailable", "critical", "Discord gateway is not ready.")
            )
        if snapshot is None:
            findings.append(
                HealthFinding("snapshot_missing", "critical", "No configuration snapshot exists.")
            )
            return _report(findings, now)
        findings.extend(_permission_findings(snapshot))
        findings.extend(_integration_findings(snapshot))
        findings.extend(_live_signal_findings(snapshot))
        if now - snapshot.captured_at > timedelta(hours=26):
            findings.append(
                HealthFinding(
                    "snapshot_stale",
                    "warning",
                    f"Latest snapshot was captured at {snapshot.captured_at.isoformat()}.",
                )
            )
        return _report(findings, now)

    def permission_health(self, snapshot: SnapshotRecord) -> HealthReport:
        return _report(_permission_findings(snapshot), snapshot.captured_at)

    def integration_health(self, snapshot: SnapshotRecord) -> HealthReport:
        return _report(_integration_findings(snapshot), snapshot.captured_at)
