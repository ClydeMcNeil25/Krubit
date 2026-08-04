from datetime import UTC, datetime

from krubit.discord.cards import render_card, render_diff_card, render_health_card
from krubit.domain.companion import DiffItem, HealthFinding, HealthReport, SnapshotDiff
from krubit.domain.models import Card, CardField


def test_render_card_preserves_functional_content_and_brand_color() -> None:
    embed = render_card(
        Card(
            kind="fetched",
            title="Fetched: Phase 0 Status",
            description="Foundation healthy.",
            fields=(CardField("Database", "Healthy", inline=True),),
            color=0x8B5CF6,
        )
    )

    assert embed.title == "Fetched: Phase 0 Status"
    assert embed.description == "Foundation healthy."
    assert embed.color is not None and embed.color.value == 0x8B5CF6
    assert [(field.name, field.value, field.inline) for field in embed.fields] == [
        ("Database", "Healthy", True)
    ]
    assert "functional system card" in (embed.footer.text or "")


def test_phase_one_cards_bound_diff_fields_and_report_overflow() -> None:
    diff = SnapshotDiff(
        "older_to_newer",
        tuple(
            DiffItem("channels", str(index), "modified", {"name": {"before": "A", "after": "B"}})
            for index in range(30)
        ),
    )

    embed = render_diff_card(diff, title="Fetched: Server Changes")

    assert len(embed.fields) == 25
    assert "5 additional changes" in (embed.footer.text or "")


def test_health_card_contains_factual_status_and_findings() -> None:
    report = HealthReport(
        "warning",
        (HealthFinding("snapshot_stale", "warning", "Snapshot is 27 hours old."),),
        datetime(2026, 8, 4, tzinfo=UTC),
    )

    embed = render_health_card(report, title="Fetched: Server Health")

    assert embed.title == "Fetched: Server Health"
    assert "Warning" in (embed.description or "")
    assert embed.fields[0].name == "snapshot_stale"
