from krubit.discord.cards import render_card
from krubit.domain.models import Card, CardField


def test_render_card_preserves_functional_content_and_brand_color() -> None:
    embed = render_card(
        Card(
            kind="fetched",
            title="🦴 Fetched: Phase 0 Status",
            description="Foundation healthy.",
            fields=(CardField("Database", "Healthy", inline=True),),
            color=0x8B5CF6,
        )
    )

    assert embed.title == "🦴 Fetched: Phase 0 Status"
    assert embed.description == "Foundation healthy."
    assert embed.color is not None and embed.color.value == 0x8B5CF6
    assert [(field.name, field.value, field.inline) for field in embed.fields] == [
        ("Database", "Healthy", True)
    ]
    assert embed.footer.text == "Krubit · functional system card"
