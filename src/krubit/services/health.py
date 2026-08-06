"""Factual operational health classification without community recommendations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from krubit.domain.companion import HealthFinding, HealthReport, SnapshotRecord
from krubit.domain.creator_signals import CapabilityState, ContentCursor
from krubit.domain.models import JSONValue
from krubit.integrations.base import ConnectorHealth
from krubit.services.creator_analytics import DeliveryCounts

_SEVERITY = {"healthy": 0, "limited": 1, "warning": 2, "critical": 3}

_CURSOR_STALE_AFTER = timedelta(hours=26)

# A capability is only ever reported operational at READY; every other declared state
# maps to a factual, non-editorializing severity for `/fetch creator`/`/fetch
# integrations` findings.
_CAPABILITY_SEVERITY: dict[CapabilityState, str] = {
    CapabilityState.READY: "healthy",
    CapabilityState.UNCONFIGURED: "limited",
    CapabilityState.AUTHORIZATION_REQUIRED: "limited",
    CapabilityState.APPROVAL_REQUIRED: "limited",
    CapabilityState.UNSUPPORTED: "limited",
    CapabilityState.DEGRADED: "warning",
    CapabilityState.QUOTA_LIMITED: "warning",
}


@dataclass(frozen=True, slots=True)
class WatchdogHealthFacts:
    """Process-level Phase 3 Watchdog capability facts, supplied by the caller.

    These are not derived from a `SnapshotRecord` (unlike `_live_signal_findings`'s
    `live_signal` dict) because they are properties of the running process's
    `Settings`/gateway connection, not of a captured guild inventory -- the same
    reason `server_health`'s `database_healthy`/`gateway_ready` are already passed as
    explicit keyword arguments rather than read from a snapshot. Passing `None`
    instead of a `WatchdogHealthFacts` (the default on every method below) skips
    watchdog findings entirely, preserving every caller that predates Phase 3.
    """

    enabled: bool
    notifications_enabled: bool
    message_content_available: bool


@dataclass(frozen=True, slots=True)
class ActivityLedgerHealthFacts:
    """Process-level Phase 4 Activity Ledger capability facts, supplied by the
    caller -- mirrors `WatchdogHealthFacts`'s own docstring rationale exactly: these
    are properties of the running process's `Settings`, not of a captured guild
    inventory, so they are passed as an explicit keyword argument rather than
    derived from a `SnapshotRecord`. Passing `None` (the default) skips
    activity-ledger findings entirely, preserving every caller that predates Phase 4.
    """

    enabled: bool
    retention_configured: bool


def _activity_ledger_findings(facts: ActivityLedgerHealthFacts | None) -> list[HealthFinding]:
    if facts is None:
        return []
    if not facts.enabled:
        return [
            HealthFinding(
                "activity_ledger_disabled",
                "limited",
                "Activity ledger is disabled (KRUBIT_ACTIVITY_LEDGER_ENABLED=false); no "
                "participation event (message, reaction, voice, attendance, join, role "
                "change) is being ingested, and every /fetch newcomers/inactive/"
                "milestones/retention/community-pulse view has no data to report.",
            )
        ]
    findings: list[HealthFinding] = []
    if not facts.retention_configured:
        findings.append(
            HealthFinding(
                "activity_ledger_retention_unconfigured",
                "limited",
                "No default retention window is configured "
                "(KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS unset); raw ledger rows "
                "accumulate indefinitely for any guild that has not separately been "
                "given its own staff-configured retention policy.",
            )
        )
    return findings


def _watchdog_findings(facts: WatchdogHealthFacts | None) -> list[HealthFinding]:
    if facts is None:
        return []
    if not facts.enabled:
        return [
            HealthFinding(
                "watchdog_disabled",
                "limited",
                "Watchdog detection is disabled (KRUBIT_WATCHDOG_ENABLED=false); no "
                "join assessment, watch window, or incident detection is running.",
            )
        ]
    findings: list[HealthFinding] = []
    if not facts.notifications_enabled:
        findings.append(
            HealthFinding(
                "watchdog_notifications_disabled",
                "limited",
                "Watchdog detection is running but staff notification delivery is "
                "disabled (KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED=false); assessments "
                "and incidents are recorded but never sent to the staff channel.",
            )
        )
    if not facts.message_content_available:
        findings.append(
            HealthFinding(
                "watchdog_message_content_unavailable",
                "warning",
                "Discord Message Content intent is not available; Watchdog is "
                "running in join-signal-only mode -- no message-based signal "
                "(mass mentions, malicious-link shape, repeated messages, spam-wave "
                "correlation, or watch-window message inspection) can be observed.",
            )
        )
    return findings


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
        watchdog: WatchdogHealthFacts | None = None,
        activity_ledger: ActivityLedgerHealthFacts | None = None,
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
            findings.extend(_watchdog_findings(watchdog))
            findings.extend(_activity_ledger_findings(activity_ledger))
            return _report(findings, now)
        findings.extend(_permission_findings(snapshot))
        findings.extend(_integration_findings(snapshot))
        findings.extend(_live_signal_findings(snapshot))
        findings.extend(_watchdog_findings(watchdog))
        findings.extend(_activity_ledger_findings(activity_ledger))
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

    def integration_health(
        self,
        snapshot: SnapshotRecord,
        *,
        watchdog: WatchdogHealthFacts | None = None,
        activity_ledger: ActivityLedgerHealthFacts | None = None,
    ) -> HealthReport:
        findings = _integration_findings(snapshot)
        findings.extend(_watchdog_findings(watchdog))
        findings.extend(_activity_ledger_findings(activity_ledger))
        return _report(findings, snapshot.captured_at)

    def creator_health(
        self,
        *,
        connector_health: Iterable[ConnectorHealth],
        cursor: ContentCursor | None,
        delivery_counts: DeliveryCounts,
        quota_exhausted: bool,
        now: datetime,
    ) -> HealthReport:
        """Factual operational health for one registered creator account.

        `connector_health` states never render anywhere beyond their `CapabilityState`
        name here — `ConnectorHealth.detail` is caller-supplied and, unlike
        `ConnectorFailure.safe_detail`, carries no built-in redaction guarantee, so this
        method never reads it.
        """
        findings: list[HealthFinding] = []
        for health in connector_health:
            severity = _CAPABILITY_SEVERITY[health.state]
            if severity == "healthy":
                continue
            findings.append(
                HealthFinding(
                    f"connector_{health.capability.value}_{health.state.value}",
                    severity,
                    f"{health.capability.value} capability is {health.state.value}.",
                )
            )
        if cursor is None:
            findings.append(
                HealthFinding(
                    "cursor_missing", "limited", "No content cursor recorded yet for this account."
                )
            )
        elif now - cursor.updated_at > _CURSOR_STALE_AFTER:
            findings.append(
                HealthFinding(
                    "cursor_stale",
                    "warning",
                    f"Content cursor last updated at {cursor.updated_at.isoformat()}.",
                )
            )
        if delivery_counts.failed:
            findings.append(
                HealthFinding(
                    "delivery_failed",
                    "warning",
                    f"{delivery_counts.failed} delivery attempt(s) currently failed.",
                )
            )
        if delivery_counts.pending:
            findings.append(
                HealthFinding(
                    "delivery_pending",
                    "limited",
                    f"{delivery_counts.pending} delivery attempt(s) still pending.",
                )
            )
        if quota_exhausted:
            findings.append(
                HealthFinding(
                    "quota_exhausted",
                    "warning",
                    "This guild's mention budget is exhausted for the current period.",
                )
            )
        return _report(findings, now)

    def bootstrap_health(
        self,
        *,
        role_present: bool,
        role_ambiguous: bool,
        channel_present: bool,
        channel_ambiguous: bool,
        now: datetime,
    ) -> HealthReport:
        """Factual health for the guild's once-resolved Creator role/notification
        channel. Ambiguous (more than one exact-name match) and missing are both
        reported as findings — creator command surfaces never implicitly create
        either resource."""
        findings: list[HealthFinding] = []
        if role_ambiguous:
            findings.append(
                HealthFinding(
                    "creator_role_ambiguous",
                    "warning",
                    "More than one role matches the configured Creator role name.",
                )
            )
        elif not role_present:
            findings.append(
                HealthFinding(
                    "creator_role_missing", "warning", "The configured Creator role was not found."
                )
            )
        if channel_ambiguous:
            findings.append(
                HealthFinding(
                    "creator_channel_ambiguous",
                    "warning",
                    "More than one channel matches the configured notification channel name.",
                )
            )
        elif not channel_present:
            findings.append(
                HealthFinding(
                    "creator_channel_missing",
                    "warning",
                    "The configured creator notification channel was not found.",
                )
            )
        return _report(findings, now)
