from urllib.parse import parse_qs, urlparse

import discord

from krubit.discord.install import (
    install_url,
    phase_one_intents,
    phase_one_permissions,
    phase_three_intents,
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


def test_phase_two_permissions_omit_scheduled_event_scopes_by_default() -> None:
    permissions = phase_two_permissions()

    assert permissions.create_events is False
    assert permissions.manage_events is False


def test_phase_two_permissions_add_scheduled_event_scopes_when_enabled() -> None:
    permissions = phase_two_permissions(scheduled_events_enabled=True)

    assert permissions.create_events is True
    assert permissions.manage_events is True
    # Enabling the feature must not silently grant anything else.
    disabled = phase_two_permissions()
    disabled.create_events = True
    disabled.manage_events = True
    assert permissions.value == disabled.value


def test_install_url_omits_scheduled_event_scopes_by_default() -> None:
    parsed = urlparse(install_url(123456789012345678))
    query = parse_qs(parsed.query)

    assert query["permissions"] == [str(phase_two_permissions().value)]


def test_phase_three_intents_adds_message_content_to_phase_two() -> None:
    intents = phase_three_intents()

    assert intents.message_content is True
    assert intents.members is True  # inherited from phase_two_intents()
    assert intents.guilds is True
    assert intents.presences is True


def test_phase_three_intents_adds_messages_so_on_message_actually_dispatches() -> None:
    """`message_content` alone only controls whether Message fields populate; it does
    not cause `MESSAGE_CREATE` to dispatch at all -- `Intents.messages` does. Without
    it, requesting `message_content` alone would never fire `on_message`."""
    intents = phase_three_intents()

    assert intents.messages is True


def test_install_url_requests_scheduled_event_scopes_when_enabled() -> None:
    parsed = urlparse(install_url(123456789012345678, scheduled_events_enabled=True))
    query = parse_qs(parsed.query)

    expected = phase_two_permissions(scheduled_events_enabled=True)
    assert query["permissions"] == [str(expected.value)]
    assert query["permissions"] != [str(phase_two_permissions().value)]
