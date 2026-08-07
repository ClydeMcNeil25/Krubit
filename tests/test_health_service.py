from datetime import UTC, datetime, timedelta

from krubit.domain.companion import CoverageIssue, SnapshotRecord
from krubit.domain.models import JSONValue
from krubit.services.health import ActivityLedgerHealthFacts, HealthService, WatchdogHealthFacts


def snapshot_with(
    *,
    captured_at: datetime,
    missing: list[str] | None = None,
    coverage: tuple[CoverageIssue, ...] = (),
    configured_present: bool | None = None,
    unexpected_mutation: list[str] | None = None,
    live_signal: dict[str, JSONValue] | None = None,
) -> SnapshotRecord:
    configured: dict[str, JSONValue] | None = (
        None
        if configured_present is None
        else {"id": "900", "present": configured_present}
    )
    missing_values: list[JSONValue] = []
    missing_values.extend(missing or [])
    mutation_values: list[JSONValue] = []
    mutation_values.extend(unexpected_mutation or [])
    content: dict[str, JSONValue] = {
        "bot_permissions": {
            "missing_required": missing_values,
            "unexpected_mutation": mutation_values,
        },
        "configured_channel": configured,
        "live_signal": live_signal or {},
    }
    return SnapshotRecord(
        snapshot_id="snapshot:one",
        guild_id=111,
        version=1,
        content_hash="hash",
        content=content,
        coverage=coverage,
        captured_at=captured_at,
    )


def test_server_health_reports_missing_permission_and_limited_coverage() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(
            captured_at=now,
            missing=["view_audit_log"],
            coverage=(CoverageIssue("webhooks", "limited", "discord_http_403"),),
        ),
        now=now,
        database_healthy=True,
        gateway_ready=True,
    )

    assert report.status == "warning"
    assert [finding.code for finding in report.findings] == [
        "missing_required_permission",
        "limited_webhook_coverage",
    ]
    assert all(not hasattr(finding, "recommendation") for finding in report.findings)


def test_server_health_marks_missing_snapshot_and_database_as_critical() -> None:
    report = HealthService().server_health(
        None,
        now=datetime(2026, 8, 4, tzinfo=UTC),
        database_healthy=False,
        gateway_ready=True,
    )

    assert report.status == "critical"
    assert {finding.code for finding in report.findings} == {
        "database_unavailable",
        "snapshot_missing",
    }


def test_server_health_marks_old_snapshot_and_missing_channel_as_warning() -> None:
    now = datetime(2026, 8, 4, 12, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now - timedelta(hours=27), configured_present=False),
        now=now,
        database_healthy=True,
        gateway_ready=True,
    )

    assert report.status == "warning"
    assert [finding.code for finding in report.findings] == [
        "configured_channel_missing",
        "snapshot_stale",
    ]


def test_specialized_health_reports_limit_their_factual_sections() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    snapshot = snapshot_with(
        captured_at=now,
        missing=["view_audit_log"],
        coverage=(CoverageIssue("automod_rules", "limited", "discord_http_403"),),
    )

    permissions = HealthService().permission_health(snapshot)
    integrations = HealthService().integration_health(snapshot)

    assert [finding.code for finding in permissions.findings] == [
        "missing_required_permission"
    ]
    assert [finding.code for finding in integrations.findings] == [
        "limited_automod_coverage"
    ]


def test_permission_health_warns_when_discord_role_grants_mutation_authority() -> None:
    snapshot = snapshot_with(
        captured_at=datetime(2026, 8, 4, tzinfo=UTC),
        unexpected_mutation=["manage_roles", "ban_members"],
    )

    report = HealthService().permission_health(snapshot)

    assert report.status == "warning"
    assert [finding.code for finding in report.findings] == [
        "unexpected_mutation_permission",
        "unexpected_mutation_permission",
    ]
    assert "ban_members" in report.findings[0].detail
    assert "manage_roles" in report.findings[1].detail


def test_server_health_reports_each_missing_live_signal_fact_distinctly() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(
            captured_at=now,
            live_signal={
                "channel": {"id": "900", "present": False},
                "role": {"id": "333", "present": False},
                "presence_intent": False,
                "twitch_credentials": False,
                "twitch_available": False,
                "manage_roles": False,
                "role_hierarchy": False,
                "mention_everyone": False,
            },
        ),
        now=now,
        database_healthy=True,
        gateway_ready=True,
    )

    assert [finding.code for finding in report.findings] == [
        "live_channel_missing",
        "live_manage_roles_missing",
        "live_mention_everyone_missing",
        "live_presence_intent_missing",
        "live_role_hierarchy_missing",
        "live_role_missing",
        "live_twitch_credentials_missing",
        "live_twitch_unavailable",
    ]


def test_server_health_reports_nothing_about_watchdog_when_facts_are_not_supplied() -> None:
    """A caller that never passes `watchdog=` (every pre-Phase-3 test and call site)
    must see zero behavior change -- `None` means "don't report," not "disabled."
    """
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now), now=now, database_healthy=True, gateway_ready=True
    )

    assert report.status == "healthy"
    assert report.findings == ()


def test_server_health_reports_watchdog_disabled_and_nothing_else_when_off() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        watchdog=WatchdogHealthFacts(
            enabled=False, notifications_enabled=False, message_content_available=False
        ),
    )

    assert [finding.code for finding in report.findings] == ["watchdog_disabled"]


def test_server_health_reports_notifications_and_message_content_facts_when_enabled() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        watchdog=WatchdogHealthFacts(
            enabled=True, notifications_enabled=False, message_content_available=False
        ),
    )

    assert [finding.code for finding in report.findings] == [
        "watchdog_message_content_unavailable",
        "watchdog_notifications_disabled",
    ]
    assert report.status == "warning"


def test_server_health_reports_nothing_about_watchdog_when_fully_enabled() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        watchdog=WatchdogHealthFacts(
            enabled=True, notifications_enabled=True, message_content_available=True
        ),
    )

    assert report.findings == ()


def test_integration_health_includes_watchdog_facts_when_supplied() -> None:
    snapshot = snapshot_with(captured_at=datetime(2026, 8, 4, tzinfo=UTC))

    report = HealthService().integration_health(
        snapshot,
        watchdog=WatchdogHealthFacts(
            enabled=True, notifications_enabled=True, message_content_available=False
        ),
    )

    assert [finding.code for finding in report.findings] == [
        "watchdog_message_content_unavailable"
    ]


def test_server_health_reports_nothing_about_activity_ledger_when_facts_are_not_supplied() -> None:
    """A caller that never passes `activity_ledger=` (every pre-Phase-4 test and call
    site) must see zero behavior change -- `None` means "don't report," not "disabled."
    """
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now), now=now, database_healthy=True, gateway_ready=True
    )

    assert report.status == "healthy"
    assert report.findings == ()


def test_server_health_reports_activity_ledger_disabled_and_nothing_else_when_off() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(enabled=False, retention_configured=False),
    )

    assert [finding.code for finding in report.findings] == ["activity_ledger_disabled"]


def test_server_health_reports_retention_unconfigured_when_ledger_enabled() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(
            enabled=True, retention_configured=False, exclusions_configured=True
        ),
    )

    assert [finding.code for finding in report.findings] == [
        "activity_ledger_retention_unconfigured"
    ]


def test_server_health_reports_exclusions_unconfigured_when_ledger_enabled() -> None:
    """See Important #7 of the 2026-08-06 Phase 4 final-review fix report: an
    operator running `/fetch server-health` on an enabled ledger with zero channel
    exclusions configured must see a finding, not a clean bill of health."""
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(
            enabled=True, retention_configured=True, exclusions_configured=False
        ),
    )

    assert [finding.code for finding in report.findings] == [
        "activity_ledger_exclusions_unconfigured"
    ]


def test_server_health_reports_both_retention_and_exclusions_unconfigured() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(
            enabled=True, retention_configured=False, exclusions_configured=False
        ),
    )

    assert {finding.code for finding in report.findings} == {
        "activity_ledger_retention_unconfigured",
        "activity_ledger_exclusions_unconfigured",
    }


def test_server_health_reports_nothing_about_activity_ledger_when_fully_configured() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        snapshot_with(captured_at=now),
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(
            enabled=True, retention_configured=True, exclusions_configured=True
        ),
    )

    assert report.findings == ()


def test_integration_health_includes_activity_ledger_facts_when_supplied() -> None:
    snapshot = snapshot_with(captured_at=datetime(2026, 8, 6, tzinfo=UTC))

    report = HealthService().integration_health(
        snapshot,
        activity_ledger=ActivityLedgerHealthFacts(
            enabled=True, retention_configured=False, exclusions_configured=True
        ),
    )

    assert [finding.code for finding in report.findings] == [
        "activity_ledger_retention_unconfigured"
    ]


def test_server_health_reports_missing_snapshot_still_reports_activity_ledger_facts() -> None:
    """Mirrors `test_server_health_marks_missing_snapshot_and_database_as_critical`'s
    missing-snapshot early-return path -- activity-ledger facts must still surface
    even when there is no snapshot to derive permission/integration findings from.
    """
    now = datetime(2026, 8, 6, tzinfo=UTC)
    report = HealthService().server_health(
        None,
        now=now,
        database_healthy=True,
        gateway_ready=True,
        activity_ledger=ActivityLedgerHealthFacts(enabled=False, retention_configured=False),
    )

    assert "activity_ledger_disabled" in [finding.code for finding in report.findings]
    assert "snapshot_missing" in [finding.code for finding in report.findings]
