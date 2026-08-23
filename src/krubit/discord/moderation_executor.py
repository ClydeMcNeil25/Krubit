"""Real Discord ActionExecutor for the moderation adapter (Phase 5+6 slice 4).

Timeout only -- kick and permanent ban are never reachable through this
class, matching the roadmap's Phase 5 boundary ("Kick and permanent ban
remain unavailable to autonomous execution in this phase regardless of
configuration"). Every ordinary failure mode (guild/member not found,
missing permission, Discord API error) produces an honest
`ActionReceipt(succeeded=False, ...)` rather than raising -- matching
`krubit.web.moderation_adapter.NoOpActionExecutor`'s contract from slice 3.
"""

from __future__ import annotations

import uuid

import discord

from krubit.domain.moderation import ActionReceipt, ModerationCase

_MAX_DETAIL_LENGTH = 500


class DiscordActionExecutor:
    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        if case.action_expiration is None:
            raise ValueError(
                f"case {case.case_id} has no action_expiration set -- "
                "record_human_decision must run before execute_approved_action"
            )
        guild = self._bot.get_guild(case.guild_id)
        if guild is None:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"guild {case.guild_id} not found or bot not a member",
            )
        member = guild.get_member(case.member_id)
        if member is None:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"member {case.member_id} not found in guild (left?)",
            )
        try:
            await member.timeout(
                case.action_expiration, reason=f"Krubit moderation case {case.case_id}"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return ActionReceipt(
                receipt_id=f"receipt:{uuid.uuid4().hex}",
                succeeded=False,
                detail=f"{type(exc).__name__}: {exc}"[:_MAX_DETAIL_LENGTH],
            )
        return ActionReceipt(
            receipt_id=f"receipt:{uuid.uuid4().hex}",
            succeeded=True,
            detail=f"timed out until {case.action_expiration.isoformat()}",
        )


class LazyDiscordActionExecutor:
    """Bindable wrapper solving `__main__.py`'s boot-order problem: the HTTP
    callback server (which needs an `ActionExecutor` to construct its
    `ModerationService`) is built before `KrubitBot` exists. Construct one
    instance, pass it wherever an `ActionExecutor` is needed, then call
    `bind(bot)` once `KrubitBot` is constructed.
    """

    def __init__(self) -> None:
        self._bot: discord.Client | None = None

    def bind(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, case: ModerationCase, execution_key: str) -> ActionReceipt:
        if self._bot is None:
            raise RuntimeError(
                "LazyDiscordActionExecutor.execute called before bind() -- "
                "the bot must be constructed and bound first"
            )
        return await DiscordActionExecutor(self._bot).execute(case, execution_key)
