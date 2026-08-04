from urllib.parse import parse_qs, urlparse

import discord

from krubit.discord.install import install_url, phase_zero_intents, phase_zero_permissions


def test_phase_zero_enables_only_guilds_intent() -> None:
    expected = discord.Intents.none()
    expected.guilds = True

    assert phase_zero_intents().value == expected.value


def test_phase_zero_requests_only_four_message_delivery_permissions() -> None:
    expected = discord.Permissions.none()
    expected.view_channel = True
    expected.send_messages = True
    expected.embed_links = True
    expected.read_message_history = True

    assert phase_zero_permissions().value == expected.value


def test_install_url_contains_guild_install_scopes_and_permissions() -> None:
    parsed = urlparse(install_url(123456789012345678))
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"
    assert query["client_id"] == ["123456789012345678"]
    assert set(query["scope"][0].split(" ")) == {"bot", "applications.commands"}
    assert query["permissions"] == [str(phase_zero_permissions().value)]
    assert query["integration_type"] == ["0"]

