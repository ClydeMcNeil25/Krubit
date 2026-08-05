"""Discord install settings for the non-privileged Phase 0 surface."""

from __future__ import annotations

from urllib.parse import urlencode

import discord


def phase_zero_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


def phase_zero_permissions() -> discord.Permissions:
    permissions = discord.Permissions.none()
    permissions.view_channel = True
    permissions.send_messages = True
    permissions.embed_links = True
    permissions.read_message_history = True
    return permissions


def phase_one_intents() -> discord.Intents:
    intents = phase_zero_intents()
    intents.members = True
    return intents


def phase_one_permissions() -> discord.Permissions:
    permissions = phase_zero_permissions()
    permissions.view_audit_log = True
    return permissions


def phase_two_intents() -> discord.Intents:
    intents = phase_one_intents()
    intents.presences = True
    return intents


def phase_two_permissions(*, scheduled_events_enabled: bool = False) -> discord.Permissions:
    """The Phase 2 permission set, with `create_events`/`manage_events` opt-in.

    Scheduled Event sync (Task 11) is an opt-in feature: an install that never turns
    it on has no reason to ask a server for `Create Events`/`Manage Events`, so those
    two scopes are only added when `scheduled_events_enabled` is explicitly True.
    """
    permissions = phase_one_permissions()
    permissions.manage_roles = True
    permissions.mention_everyone = True
    if scheduled_events_enabled:
        permissions.create_events = True
        permissions.manage_events = True
    return permissions


def install_url(application_id: int, *, scheduled_events_enabled: bool = False) -> str:
    if application_id <= 0:
        raise ValueError("application_id must be positive")
    query = urlencode(
        {
            "client_id": str(application_id),
            "scope": "bot applications.commands",
            "permissions": str(
                phase_two_permissions(scheduled_events_enabled=scheduled_events_enabled).value
            ),
            "integration_type": "0",
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"
