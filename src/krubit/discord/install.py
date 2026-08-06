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


def phase_three_intents() -> discord.Intents:
    """The Phase 3 Watchdog intent set: Message Content, additively on top of Phase 2.

    Per the design doc's "Privileged Intent Requirement" section, the post-join watch
    window's message inspection needs Discord's privileged `message_content` intent,
    which nothing before this phase requests. This also adds the non-privileged
    `messages` intent (`guild_messages`/`dm_messages`): `message_content` on its own
    only controls whether `content`/`embeds`/`attachments`/`components` populate on a
    dispatched `MESSAGE_CREATE` event — it does not itself cause that event to be
    dispatched at all (`discord.Intents.messages` gates dispatch). Without `messages`,
    requesting `message_content` alone would leave `on_message` never firing for guild
    text channels, silently dead-ending the entire watch-window message path this
    intent exists to unlock. `phase_two_intents()`'s own three flags (`guilds`,
    `members`, `presences`) are inherited unchanged.
    """
    intents = phase_two_intents()
    intents.message_content = True
    intents.messages = True
    # AutoMod correlation (Task 6, `krubit.services.incident_evidence.
    # correlate_automod_action`, wired in `KrubitBot.on_automod_action`) needs Discord
    # to actually dispatch `AUTO_MODERATION_ACTION_EXECUTION`/rule-CRUD gateway events
    # in the first place -- without these two intents Discord never sends them
    # regardless of what the bot's own handlers do, silently dead-ending the entire
    # correlation path. Both are non-privileged (no Developer Portal toggle required),
    # unlike `message_content` above.
    intents.auto_moderation_configuration = True
    intents.auto_moderation_execution = True
    return intents


def phase_four_intents() -> discord.Intents:
    """The Phase 4 Activity Ledger intent set: reactions, voice states, and
    Scheduled Event RSVPs, additively on top of Phase 3.

    All three are non-privileged (no Developer Portal toggle required), unlike
    `phase_three_intents()`'s `message_content`. `guild_reactions` lets
    `on_raw_reaction_add`/`on_raw_reaction_remove` dispatch at all; `voice_states`
    lets `on_voice_state_update` dispatch (the activity ledger's voice-session
    tracking has nothing to bridge without it); `guild_scheduled_events` lets
    `on_scheduled_event_user_add`/`on_scheduled_event_user_remove` dispatch (the
    attendance bridge's two source callbacks). `phase_three_intents()`'s own flags
    (including `message_content`) are inherited unchanged.
    """
    intents = phase_three_intents()
    intents.guild_reactions = True
    intents.voice_states = True
    intents.guild_scheduled_events = True
    return intents


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
