from urllib.parse import parse_qs, urlparse

import discord

from krubit.discord.install import (
    install_url,
    phase_one_intents,
    phase_one_permissions,
    phase_two_intents,
    phase_two_permissions,
    phase_zero_intents,
    phase_zero_permissions,
)


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
    assert query["permissions"] == [str(phase_two_permissions().value)]
    assert query["integration_type"] == ["0"]


def test_phase_one_adds_members_intent_and_read_only_audit_access() -> None:
    intents = phase_one_intents()
    permissions = phase_one_permissions()

    assert intents.guilds is True
    assert intents.members is True
    assert permissions.view_audit_log is True
    assert permissions.manage_guild is False
    assert permissions.manage_channels is False
    assert permissions.manage_roles is False
    assert permissions.manage_webhooks is False
    assert permissions.kick_members is False
    assert permissions.ban_members is False


def test_phase_two_enables_presence_and_required_mutations() -> None:
    intents = phase_two_intents()
    permissions = phase_two_permissions()

    assert intents.guilds and intents.members and intents.presences
    assert permissions.manage_roles
    assert permissions.mention_everyone
    assert permissions.administrator is False
