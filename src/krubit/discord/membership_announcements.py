"""Automatic, convention-based join/leave announcements.

No configuration of any kind -- a channel's mere presence or absence by
exact name (`WELCOME_CHANNEL_NAME`/`STAFF_NOTES_CHANNEL_NAME`) is the only
control, matching `live_runtime.py`'s existing `LIVE_CHANNEL_NAME`
convention exactly. Enable/disable control for this and every other
Krubit feature is explicitly deferred to a future web dashboard (see the
design doc) -- this module intentionally carries zero settings/storage
dependency until that exists.

Every message is plain, factual text, matching this codebase's
established tone: Krubit's own README describes it as a "non-conversational"
bot, so even this public-facing welcome message stays factual rather than
personality-laden.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord

WELCOME_CHANNEL_NAME = "welcome"
STAFF_NOTES_CHANNEL_NAME = "staff-notes"

_logger = logging.getLogger(__name__)


def _is_text_channel(channel: object | None) -> bool:
    """Duck-typed text-channel check, matching `live_runtime.py`'s
    identical helper -- a channel needs a callable `.send` and
    `.permissions_for`, never `isinstance(discord.TextChannel)`."""
    return (
        channel is not None
        and callable(getattr(channel, "send", None))
        and callable(getattr(channel, "permissions_for", None))
    )


def _find_channel_by_name(guild: discord.Guild, name: str) -> discord.abc.Messageable | None:
    return next(
        (
            candidate
            for candidate in guild.channels
            if getattr(candidate, "name", None) == name and _is_text_channel(candidate)
        ),
        None,
    )


async def _send(channel: discord.abc.Messageable | None, content: str) -> None:
    if channel is None:
        return
    try:
        await channel.send(content=content)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound, ValueError):
        _logger.debug("membership announcement send failed", exc_info=True)


class MembershipAnnouncementRuntime:
    """Posts join/leave announcements to fixed-name channels, if present."""

    async def on_member_join(self, member: discord.Member) -> None:
        welcome_channel = _find_channel_by_name(member.guild, WELCOME_CHANNEL_NAME)
        await _send(welcome_channel, f"<@{member.id}> has joined the server.")

        staff_notes_channel = _find_channel_by_name(member.guild, STAFF_NOTES_CHANNEL_NAME)
        created_at = member.created_at.isoformat()
        await _send(
            staff_notes_channel, f"<@{member.id}> joined. Account created: {created_at}."
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        staff_notes_channel = _find_channel_by_name(member.guild, STAFF_NOTES_CHANNEL_NAME)
        joined_at = member.joined_at.isoformat() if member.joined_at is not None else "unknown"
        left_at = datetime.now(UTC).isoformat()
        await _send(
            staff_notes_channel,
            f"<@{member.id}> left. Joined: {joined_at}. Left: {left_at}.",
        )
