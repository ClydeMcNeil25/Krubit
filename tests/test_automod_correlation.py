"""Tests for `krubit.services.incident_evidence.correlate_automod_action`.

`correlate_automod_action` reads the *existing* `discord.AutoModAction` payload that
`KrubitBot.on_automod_action` (`src/krubit/discord/bot.py:840`) already receives from
Discord's own AutoMod system, and maps it to a `RiskSignal`. It must never call any
Discord API and must never create/edit an AutoMod rule or take any enforcement action
(timeout/kick/ban/delete) — it only *reads* an event that already happened and
*names* it as a signal for Krubit's own, independent risk-evaluation path.

`matched_keyword`/`matched_content` are message content, so a family of tests below
covers the `member_has_open_watch_window` gate: the plan's global constraint is that
Krubit reads message content only for a member with an actively open watch window,
so the literal matched text must never appear in the returned signal's `detail` when
that flag is `False`, even though the fact that AutoMod fired is still useful,
content-free evidence either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from krubit.domain.watchdog import RiskSignal
from krubit.services.incident_evidence import correlate_automod_action

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeAutoModAction:
    def __init__(
        self,
        *,
        guild_id: int = 111,
        rule_id: int = 222,
        user_id: int = 333,
        channel_id: int = 444,
        rule_trigger_type: discord.AutoModRuleTriggerType | None = None,
        matched_keyword: str | None = None,
        matched_content: str | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.rule_id = rule_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.rule_trigger_type = rule_trigger_type
        self.matched_keyword = matched_keyword
        self.matched_content = matched_content


def automod_action(
    *,
    rule_trigger: str | None,
    guild_id: int = 111,
    rule_id: int = 222,
    user_id: int = 333,
    channel_id: int = 444,
    matched_keyword: str | None = None,
    matched_content: str | None = None,
) -> discord.AutoModAction:
    trigger = discord.AutoModRuleTriggerType[rule_trigger] if rule_trigger is not None else None
    action = FakeAutoModAction(
        guild_id=guild_id,
        rule_id=rule_id,
        user_id=user_id,
        channel_id=channel_id,
        rule_trigger_type=trigger,
        matched_keyword=matched_keyword,
        matched_content=matched_content,
    )
    return cast(discord.AutoModAction, action)


def test_automod_action_becomes_a_correlated_risk_signal_not_a_new_enforcement() -> None:
    signal = correlate_automod_action(
        automod_action(rule_trigger="spam"), now=NOW, member_has_open_watch_window=False
    )

    assert signal is not None
    assert isinstance(signal, RiskSignal)
    assert signal.name == "automod_correlated_spam"


def test_correlated_signal_names_the_triggered_rule_type() -> None:
    signal = correlate_automod_action(
        automod_action(rule_trigger="keyword_preset"),
        now=NOW,
        member_has_open_watch_window=False,
    )

    assert signal is not None
    assert signal.name == "automod_correlated_keyword_preset"
    assert "222" in signal.detail  # names the rule_id, per bot.py's existing payload


def test_correlated_signal_carries_the_matched_keyword_when_member_has_open_watch_window() -> None:
    signal = correlate_automod_action(
        automod_action(rule_trigger="keyword", matched_keyword="badword"),
        now=NOW,
        member_has_open_watch_window=True,
    )

    assert signal is not None
    assert "badword" in signal.detail


def test_correlated_signal_withholds_matched_content_without_an_open_watch_window() -> None:
    """The plan's global constraint: Krubit reads message content only for a member
    with an actively open watch window. `matched_keyword`/`matched_content` are
    literal substrings of the member's own message, so they must never reach the
    signal's `detail` for a member outside any watch window — only the fact that
    AutoMod fired, and which rule/category, may.
    """
    signal = correlate_automod_action(
        automod_action(rule_trigger="keyword", matched_keyword="badword"),
        now=NOW,
        member_has_open_watch_window=False,
    )

    assert signal is not None
    assert "badword" not in signal.detail
    assert signal.name == "automod_correlated_keyword"


def test_correlated_signal_also_withholds_matched_content_field_without_open_watch_window() -> None:
    signal = correlate_automod_action(
        automod_action(
            rule_trigger="harmful_link", matched_content="evil-secret-payload.example"
        ),
        now=NOW,
        member_has_open_watch_window=False,
    )

    assert signal is not None
    assert "evil-secret-payload.example" not in signal.detail


def test_correlation_returns_none_without_a_recognizable_trigger_type() -> None:
    """Matches the minimal payload shape already exercised by
    `tests/test_discord_events.py`'s `test_bot_collects_guild_and_automod_rule_changes`
    (a bare `SimpleNamespace(guild_id=..., rule_id=..., user_id=..., channel_id=...)`
    with no `rule_trigger_type`) — correlation must degrade to `None`, not raise, so
    wiring this into `on_automod_action` cannot break that existing regression test.
    """
    minimal = cast(
        discord.AutoModAction,
        SimpleNamespace(guild_id=111, rule_id=222, user_id=333, channel_id=444),
    )

    assert correlate_automod_action(minimal, now=NOW, member_has_open_watch_window=False) is None


def test_correlate_automod_action_requires_an_aware_datetime() -> None:
    with pytest.raises(ValueError):
        correlate_automod_action(
            automod_action(rule_trigger="spam"),
            now=datetime(2026, 8, 5, 12, 0),
            member_has_open_watch_window=False,
        )
