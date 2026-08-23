"""Tests for #mod-room moderation notifications (slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from krubit.discord.moderation_notifier import ModerationNotifier
from krubit.domain.moderation import AppealStatus, ModerationCase, ModerationStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _case(status: ModerationStatus = ModerationStatus.APPROVAL_REQUIRED) -> ModerationCase:
    return ModerationCase(
        case_id="case:1",
        incident_id="incident:1",
        guild_id=100,
        member_id=200,
        report_timestamp=NOW,
        offense_number=1,
        recommended_action="24h timeout",
        executed_action=None,
        action_expiration=None,
        status=status,
        review_deadline=None,
        reviewer_id=None,
        reviewer_decision=None,
        appeal_status=AppealStatus.NONE,
        close_timestamp=None,
    )


def _sendable_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    permissions = MagicMock(view_channel=True, send_messages=True, embed_links=True)
    channel.permissions_for.return_value = permissions
    return channel


@pytest.mark.asyncio
async def test_notify_pending_review_sends_when_configured() -> None:
    channel = _sendable_channel()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())

    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_pending_review_noop_when_channel_id_unset() -> None:
    guild = MagicMock()
    notifier = ModerationNotifier(channel_id=None)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())

    guild.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pending_review_noop_when_guild_not_found() -> None:
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: None)
    await notifier.notify_pending_review(_case())  # must not raise


@pytest.mark.asyncio
async def test_notify_pending_review_swallows_send_failure() -> None:
    channel = _sendable_channel()
    channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_pending_review(_case())  # must not raise


@pytest.mark.asyncio
async def test_notify_outcome_sends_for_executed_case() -> None:
    channel = _sendable_channel()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    guild.me = MagicMock()
    notifier = ModerationNotifier(channel_id=555)
    notifier.bind(lambda gid: guild)

    await notifier.notify_outcome(_case(status=ModerationStatus.EXECUTED), succeeded=True)

    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_pending_review_noop_before_bind() -> None:
    notifier = ModerationNotifier(channel_id=555)
    await notifier.notify_pending_review(_case())  # must not raise, no bind() called
