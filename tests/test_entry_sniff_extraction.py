"""Unit tests for `krubit.discord.watchdog_events.extract_join_signals`.

Pure-function tests: no Discord objects, no I/O, no storage — matching the
`extract_twitch_observation`-style test convention for `discord/*` extraction
functions. Uses a small local `FakeMember`-shaped factory rather than mocks, since the
function only touches plain attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from krubit.discord.watchdog_events import extract_join_signals

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeMember:
    def __init__(
        self,
        member_id: int = 1,
        *,
        created_hours_ago: float = 24 * 365,
        has_avatar: bool = True,
        bot: bool = False,
        system: bool = False,
        pending: bool = False,
        name: str = "krucialmember",
    ) -> None:
        self.id = member_id
        self.created_at = NOW - timedelta(hours=created_hours_ago)
        self.avatar = object() if has_avatar else None
        self.bot = bot
        self.system = system
        self.pending = pending
        self.name = name


def member(
    member_id: int = 1,
    *,
    created_hours_ago: float = 24 * 365,
    has_avatar: bool = True,
    bot: bool = False,
    system: bool = False,
    pending: bool = False,
    name: str = "krucialmember",
) -> FakeMember:
    return FakeMember(
        member_id,
        created_hours_ago=created_hours_ago,
        has_avatar=has_avatar,
        bot=bot,
        system=system,
        pending=pending,
        name=name,
    )


def test_extract_join_signals_flags_new_account_and_default_avatar() -> None:
    signals = extract_join_signals(
        member(created_hours_ago=1, has_avatar=False), recent_joins=(), now=NOW
    )
    assert any(s.name == "account_age" for s in signals)
    assert any(s.name == "default_avatar" for s in signals)


def test_extract_join_signals_flags_join_cluster_similarity() -> None:
    cluster = tuple(member(created_hours_ago=1) for _ in range(8))
    signals = extract_join_signals(member(created_hours_ago=1), recent_joins=cluster, now=NOW)
    assert any(s.name == "join_cluster_similarity" for s in signals)


def test_extract_join_signals_returns_empty_tuple_for_an_unremarkable_join() -> None:
    signals = extract_join_signals(member(), recent_joins=(), now=NOW)
    assert signals == ()


def test_extract_join_signals_flags_join_velocity_without_similarity() -> None:
    # Recent joins are brand-new, default-avatar accounts, which neither match this
    # well-established member's account age nor its avatar-presence pattern, so only
    # the raw join count should fire, not the similarity signal.
    burst = tuple(
        member(member_id=100 + i, created_hours_ago=0.1, has_avatar=False) for i in range(10)
    )
    signals = extract_join_signals(
        member(created_hours_ago=24 * 365, has_avatar=True), recent_joins=burst, now=NOW
    )
    assert any(s.name == "join_velocity" for s in signals)
    assert not any(s.name == "join_cluster_similarity" for s in signals)


def test_extract_join_signals_flags_garbage_username() -> None:
    signals = extract_join_signals(member(name="284719203"), recent_joins=(), now=NOW)
    assert any(s.name == "garbage_username" for s in signals)


def test_extract_join_signals_flags_bot_and_system_accounts() -> None:
    bot_signals = extract_join_signals(member(bot=True), recent_joins=(), now=NOW)
    system_signals = extract_join_signals(member(system=True), recent_joins=(), now=NOW)
    assert any(s.name == "bot_or_system_account" for s in bot_signals)
    assert any(s.name == "bot_or_system_account" for s in system_signals)


def test_extract_join_signals_flags_rules_screening_pending() -> None:
    signals = extract_join_signals(member(pending=True), recent_joins=(), now=NOW)
    assert any(s.name == "rules_screening_pending" for s in signals)


def test_extract_join_signals_is_deterministic() -> None:
    subject = member(created_hours_ago=2, has_avatar=False, name="42")
    recent = (member(created_hours_ago=2),)
    first = extract_join_signals(subject, recent_joins=recent, now=NOW)
    second = extract_join_signals(subject, recent_joins=recent, now=NOW)
    assert first == second


def test_extract_join_signals_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        extract_join_signals(member(), recent_joins=(), now=datetime(2026, 8, 5, 12, 0))
