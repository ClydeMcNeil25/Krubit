"""Tests for the real Discord ActionExecutor (slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.discord.moderation_executor import DiscordActionExecutor, LazyDiscordActionExecutor
from krubit.domain.moderation import (
    AppealStatus,
    ApprovalDecision,
    ModerationCase,
    ModerationStatus,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EXPIRY = datetime(2026, 1, 2, tzinfo=UTC)


def _case(*, action_expiration=EXPIRY) -> ModerationCase:
    return ModerationCase(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=NOW,
        offense_number=1,
        recommended_action="24h timeout",
        executed_action=None,
        action_expiration=action_expiration,
        status=ModerationStatus.APPROVED,
        review_deadline=None,
        reviewer_id=999,
        reviewer_decision=ApprovalDecision.APPROVED,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )


@pytest.mark.asyncio
async def test_execute_succeeds_and_calls_member_timeout() -> None:
    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")

    assert receipt.succeeded is True
    member.timeout.assert_awaited_once_with(EXPIRY, reason="Krubit moderation case case:1")


@pytest.mark.asyncio
async def test_execute_missing_action_expiration_raises() -> None:
    bot = MagicMock()
    executor = DiscordActionExecutor(bot)
    with pytest.raises(ValueError, match="action_expiration"):
        await executor.execute(_case(action_expiration=None), "exec-key-1")


@pytest.mark.asyncio
async def test_execute_guild_not_found_returns_failed_receipt() -> None:
    bot = MagicMock()
    bot.get_guild.return_value = None
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False
    assert "guild" in receipt.detail.lower()


@pytest.mark.asyncio
async def test_execute_member_not_found_returns_failed_receipt() -> None:
    guild = MagicMock()
    guild.get_member.return_value = None
    bot = MagicMock()
    bot.get_guild.return_value = guild
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False
    assert "member" in receipt.detail.lower()


@pytest.mark.asyncio
async def test_execute_forbidden_returns_failed_receipt() -> None:
    member = MagicMock()
    member.timeout = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing permissions")
    )
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild
    executor = DiscordActionExecutor(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is False


@pytest.mark.asyncio
async def test_lazy_executor_raises_before_bind() -> None:
    executor = LazyDiscordActionExecutor()
    with pytest.raises(RuntimeError, match="bind"):
        await executor.execute(_case(), "exec-key-1")


@pytest.mark.asyncio
async def test_lazy_executor_delegates_after_bind() -> None:
    member = MagicMock()
    member.timeout = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    executor = LazyDiscordActionExecutor()
    executor.bind(bot)
    receipt = await executor.execute(_case(), "exec-key-1")
    assert receipt.succeeded is True
