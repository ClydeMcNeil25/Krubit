from datetime import UTC, datetime, timedelta

from krubit.domain.companion import CoverageIssue, SnapshotRecord
from krubit.domain.models import JSONValue
from krubit.services.health import HealthService


def snapshot_with(
    *,
    captured_at: datetime,
    missing: list[str] | None = None,
    coverage: tuple[CoverageIssue, ...] = (),
    configured_present: bool | None = None,
) -> SnapshotRecord:
    configured: dict[str, JSONValue] | None = (
        None
        if configured_present is None
        else {"id": "900", "present": configured_present}
    )
    missing_values: list[JSONValue] = []
    missing_values.extend(missing or [])
    content: dict[str, JSONValue] = {
        "bot_permissions": {"missing_required": missing_values},
        "configured_channel": configured,
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
