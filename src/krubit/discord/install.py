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


def install_url(application_id: int) -> str:
    if application_id <= 0:
        raise ValueError("application_id must be positive")
    query = urlencode(
        {
            "client_id": str(application_id),
            "scope": "bot applications.commands",
            "permissions": str(phase_zero_permissions().value),
            "integration_type": "0",
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"
