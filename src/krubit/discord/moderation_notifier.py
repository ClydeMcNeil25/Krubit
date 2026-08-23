"""#mod-room notifications for the moderation adapter (Phase 5+6 slice 4).

Mirrors `krubit.discord.watchdog_runtime.WatchdogRuntime.notify_staff`'s exact
gate order and failure handling: a message-send failure never raises and
never blocks or invalidates whatever ledger/state change it follows -- it is
fully isolated, best-effort, logged-and-swallowed.

`ModerationNotifier` is constructed with only `channel_id` -- `bind(guild_
lookup)` supplies the guild-resolution callable later, mirroring
`krubit.discord.moderation_executor.LazyDiscordActionExecutor`'s bindable
pattern, since `__main__.py` must construct this notifier before `KrubitBot`
(and therefore any real guild-lookup capability) exists. Unlike
`LazyDiscordActionExecutor.execute` (which raises when called unbound),
calling before `bind()` here is a clean no-op, matching the "channel not
configured" no-op path it already needs -- consistent with this module's
"never raise, never block" contract.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

import discord

from krubit.discord.cards import render_card
from krubit.domain.models import Card, CardField
from krubit.domain.moderation import ModerationCase

_logger = logging.getLogger(__name__)

GuildLookup = Callable[[int], discord.Guild | None]


def _is_sendable_channel(channel: object | None) -> bool:
    return (
        channel is not None
        and callable(getattr(channel, "send", None))
        and callable(getattr(channel, "permissions_for", None))
    )


class ModerationNotifier:
    """Post moderation lifecycle notifications to a configured `#mod-room`.

    Constructed with only `channel_id` -- `bind(guild_lookup)` supplies the
    guild-resolution callable later. See the module docstring for why.
    """

    def __init__(self, channel_id: int | None) -> None:
        self._channel_id = channel_id
        self._guild_lookup: GuildLookup | None = None

    def bind(self, guild_lookup: GuildLookup) -> None:
        self._guild_lookup = guild_lookup

    async def notify_pending_review(self, case: ModerationCase) -> None:
        await self._send(case.guild_id, case.case_id, _pending_review_card(case))

    async def notify_outcome(self, case: ModerationCase, succeeded: bool) -> None:
        await self._send(case.guild_id, case.case_id, _outcome_card(case, succeeded))

    async def _send(self, guild_id: int, case_id: str, card: Card) -> None:
        if self._channel_id is None or self._guild_lookup is None:
            return
        guild = self._guild_lookup(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(self._channel_id)
        if not _is_sendable_channel(channel):
            return
        sendable = cast(discord.TextChannel, channel)
        permissions = sendable.permissions_for(guild.me)
        if not all(
            (
                getattr(permissions, "view_channel", False),
                getattr(permissions, "send_messages", False),
                getattr(permissions, "embed_links", False),
            )
        ):
            return
        try:
            await sendable.send(embed=render_card(card))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            _logger.exception(
                "ModerationNotifier: failed to deliver notification for case %s in guild %s",
                case_id,
                guild_id,
            )


def _pending_review_card(case: ModerationCase) -> Card:
    return Card(
        kind="moderation_pending_review",
        title="Moderation: Pending Review",
        description=f"Case `{case.case_id}` needs staff review.",
        fields=(
            CardField("Case", case.case_id, inline=True),
            CardField("Member", str(case.member_id), inline=True),
            CardField("Recommended action", case.recommended_action or "—", inline=False),
        ),
    )


def _outcome_card(case: ModerationCase, succeeded: bool) -> Card:
    return Card(
        kind="moderation_outcome",
        title="Moderation: Action Outcome",
        description=f"Case `{case.case_id}` execution {'succeeded' if succeeded else 'failed'}.",
        fields=(
            CardField("Case", case.case_id, inline=True),
            CardField("Member", str(case.member_id), inline=True),
            CardField("Status", case.status.value, inline=True),
        ),
    )
