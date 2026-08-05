"""Behavioral tests for `NotificationPolicy.evaluate` and `validate_template`.

`NotificationPolicy.evaluate` is a pure, deterministic function: quiet-hours and
mention-budget *state* are supplied by the caller (read from storage beforehand), so
these tests build fresh policy objects per call rather than mutating shared state. Real
`zoneinfo.ZoneInfo` timezones are used throughout — including across the 2026 US DST
spring-forward (March 8) and fall-back (November 1) boundaries — because naive UTC-offset
arithmetic is exactly the kind of bug this policy must not have. Atomic mention-budget
*consumption* under concurrency is a storage-layer guarantee, exercised here against a
real `SQLiteStore` via `claim_mention_budget`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from krubit.domain.creator_signals import ContentEvent, ContentKind, ContentState, Platform
from krubit.services.notification_policy import (
    DeliveryDecision,
    DeliveryDisposition,
    MentionBudgetState,
    MentionKind,
    NotificationPolicy,
    NotificationTemplate,
    QuietHours,
    validate_template,
)
from krubit.storage.sqlite import SQLiteStore

GUILD_ID = 111
NOW = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


def aware(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _event(
    *,
    kind: ContentKind,
    external_id: str,
    state: ContentState = ContentState.PUBLISHED,
) -> ContentEvent:
    return ContentEvent(
        guild_id=GUILD_ID,
        account_id="acct-1",
        platform=Platform.YOUTUBE,
        external_id=external_id,
        content_kind=kind,
        state=state,
        canonical_url=f"https://example.com/{external_id}",
        title="A title",
        published_at=NOW,
        first_observed_at=NOW,
        last_observed_at=NOW,
    )


def social_event(external_id: str = "post-1") -> ContentEvent:
    return _event(kind=ContentKind.POST, external_id=external_id)


def live_event(external_id: str) -> ContentEvent:
    return _event(kind=ContentKind.LIVE, external_id=external_id, state=ContentState.LIVE)


def policy(
    *,
    quiet: str | None = None,
    timezone: str = "UTC",
    live_bypass: bool = True,
    live_everyone_budget: int | None = None,
    consumed: int = 0,
    social_role_id: int | None = None,
    social_budget: int | None = None,
    social_consumed: int = 0,
) -> NotificationPolicy:
    quiet_hours = None
    if quiet is not None:
        start_text, end_text = quiet.split("-")
        quiet_hours = QuietHours(
            start=time.fromisoformat(start_text),
            end=time.fromisoformat(end_text),
            zone=ZoneInfo(timezone),
        )
    return NotificationPolicy(
        quiet_hours=quiet_hours,
        live_bypass_quiet_hours=live_bypass,
        live_everyone_budget=MentionBudgetState(limit=live_everyone_budget, consumed=consumed),
        social_role_budget=MentionBudgetState(limit=social_budget, consumed=social_consumed),
        social_mention_role_id=social_role_id,
    )


# --- Step 1 brief tests (verbatim behavior) -------------------------------------------


def test_social_event_queues_during_quiet_hours_without_consuming_mention() -> None:
    decision = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), aware("2026-08-04T23:00:00-05:00")
    )
    assert decision.disposition is DeliveryDisposition.QUEUE
    assert decision.mention is MentionKind.NONE


def test_live_budget_suppresses_second_everyone_but_not_delivery() -> None:
    first = policy(live_everyone_budget=1).evaluate(live_event("one"), NOW)
    second = policy(live_everyone_budget=1, consumed=1).evaluate(live_event("two"), LATER)
    assert first.mention is MentionKind.EVERYONE
    assert second.disposition is DeliveryDisposition.DELIVER
    assert second.mention is MentionKind.NONE


def test_template_allows_bounded_fields_but_cannot_create_mentions() -> None:
    template = validate_template(
        NotificationTemplate(
            headline="{creator} posted on {platform}",
            footer="Fetched by Krubit",
            accent_color=0x8A2BE2,
        )
    )
    assert template.headline == "{creator} posted on {platform}"
    with pytest.raises(ValueError, match="mentions are controlled by notification policy"):
        validate_template(replace(template, headline="@everyone {title}"))


# --- Quiet hours: half-open boundaries and non-live queueing --------------------------


def test_quiet_hours_start_boundary_is_inclusive() -> None:
    decision = policy(quiet="22:00-07:00", timezone="UTC").evaluate(
        social_event(), aware("2026-08-04T22:00:00+00:00")
    )
    assert decision.disposition is DeliveryDisposition.QUEUE


def test_quiet_hours_end_boundary_is_exclusive() -> None:
    decision = policy(quiet="22:00-07:00", timezone="UTC").evaluate(
        social_event(), aware("2026-08-05T07:00:00+00:00")
    )
    assert decision.disposition is DeliveryDisposition.DELIVER


def test_queued_social_decision_carries_release_at_end_of_quiet_window() -> None:
    decision = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), aware("2026-08-04T23:00:00-05:00")
    )
    assert decision.release_at is not None
    local_release = decision.release_at.astimezone(ZoneInfo("America/Chicago"))
    assert (local_release.hour, local_release.minute) == (7, 0)


def test_delivered_decision_never_carries_release_at() -> None:
    decision = policy(quiet="22:00-07:00", timezone="UTC").evaluate(
        social_event(), aware("2026-08-04T12:00:00+00:00")
    )
    assert decision.disposition is DeliveryDisposition.DELIVER
    assert decision.release_at is None


# --- Live priority bypass ---------------------------------------------------------------


def test_live_bypasses_quiet_hours_by_default() -> None:
    decision = policy(quiet="22:00-07:00", timezone="UTC").evaluate(
        live_event("stream-1"), aware("2026-08-04T23:00:00+00:00")
    )
    assert decision.disposition is DeliveryDisposition.DELIVER
    assert decision.mention is MentionKind.EVERYONE


def test_live_is_queued_during_quiet_hours_when_bypass_explicitly_disabled() -> None:
    decision = policy(quiet="22:00-07:00", timezone="UTC", live_bypass=False).evaluate(
        live_event("stream-1"), aware("2026-08-04T23:00:00+00:00")
    )
    assert decision.disposition is DeliveryDisposition.QUEUE
    assert decision.mention is MentionKind.NONE


# --- Social mention role and budget -----------------------------------------------------


def test_social_has_no_mention_by_default() -> None:
    decision = policy().evaluate(social_event(), NOW)
    assert decision.mention is MentionKind.NONE


def test_social_uses_configured_role_mention_when_budget_available() -> None:
    decision = policy(social_role_id=999, social_budget=1).evaluate(social_event(), NOW)
    assert decision.mention is MentionKind.ROLE
    assert decision.mention_role_id == 999


def test_social_role_mention_suppressed_once_budget_is_exhausted() -> None:
    decision = policy(social_role_id=999, social_budget=1, social_consumed=1).evaluate(
        social_event(), NOW
    )
    assert decision.disposition is DeliveryDisposition.DELIVER
    assert decision.mention is MentionKind.NONE


def test_decide_mention_marks_everyone_and_role_mentions_as_consumed() -> None:
    live_decision = policy(live_everyone_budget=5).decide_mention(live_event("s1"))
    assert live_decision.consumed is True

    social_decision = policy(social_role_id=999, social_budget=5).decide_mention(social_event())
    assert social_decision.consumed is True


def test_decide_mention_marks_suppressed_outcomes_as_not_consumed() -> None:
    live_decision = policy(live_everyone_budget=1, consumed=1).decide_mention(live_event("s1"))
    assert live_decision.kind is MentionKind.NONE
    assert live_decision.consumed is False

    social_decision = policy(
        social_role_id=999, social_budget=1, social_consumed=1
    ).decide_mention(social_event())
    assert social_decision.kind is MentionKind.NONE
    assert social_decision.consumed is False


# --- DST boundaries: spring-forward and fall-back --------------------------------------


def test_quiet_hours_spring_forward_boundary_uses_correct_wall_clock() -> None:
    """2026-03-08 is the US spring-forward transition (02:00 -> 03:00) in Chicago."""
    chicago = ZoneInfo("America/Chicago")
    decision = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), datetime(2026, 3, 7, 23, 0, tzinfo=chicago)
    )
    assert decision.disposition is DeliveryDisposition.QUEUE
    assert decision.release_at is not None
    local_release = decision.release_at.astimezone(chicago)
    assert local_release.date() == datetime(2026, 3, 8).date()
    assert (local_release.hour, local_release.minute) == (7, 0)

    just_after = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), datetime(2026, 3, 8, 7, 0, tzinfo=chicago)
    )
    assert just_after.disposition is DeliveryDisposition.DELIVER


def test_quiet_hours_fall_back_boundary_uses_correct_wall_clock() -> None:
    """2026-11-01 is the US fall-back transition (02:00 -> 01:00) in Chicago."""
    chicago = ZoneInfo("America/Chicago")
    decision = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), datetime(2026, 10, 31, 23, 0, tzinfo=chicago)
    )
    assert decision.disposition is DeliveryDisposition.QUEUE
    assert decision.release_at is not None
    local_release = decision.release_at.astimezone(chicago)
    assert local_release.date() == datetime(2026, 11, 1).date()
    assert (local_release.hour, local_release.minute) == (7, 0)

    just_after = policy(quiet="22:00-07:00", timezone="America/Chicago").evaluate(
        social_event(), datetime(2026, 11, 1, 7, 0, tzinfo=chicago)
    )
    assert just_after.disposition is DeliveryDisposition.DELIVER


# --- Template validation: mention syntax beyond the literal string "@everyone" --------


@pytest.mark.parametrize(
    "headline",
    [
        "@everyone new post",
        "@here new post",
        "<@123456789012345678> check this out",
        "<@!123456789012345678> check this out",
        "<@&123456789012345678> check this out",
        "@EVERYONE shouting case",
    ],
)
def test_template_rejects_every_mention_syntax_form(headline: str) -> None:
    with pytest.raises(ValueError, match="mentions are controlled by notification policy"):
        validate_template(
            NotificationTemplate(headline=headline, footer="footer", accent_color=0x123456)
        )


def test_template_rejects_unsupported_placeholder() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        validate_template(
            NotificationTemplate(
                headline="{creator} said {something_else}",
                footer="footer",
                accent_color=0x123456,
            )
        )


def test_template_rejects_accent_color_out_of_24_bit_range() -> None:
    with pytest.raises(ValueError):
        NotificationTemplate(headline="{creator}", footer="footer", accent_color=0x1000000)


def test_template_rejects_headline_exceeding_bound() -> None:
    with pytest.raises(ValueError):
        NotificationTemplate(headline="x" * 1000, footer="footer", accent_color=0x123456)


# --- Storage-backed atomic mention budget consumption ----------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    value = await SQLiteStore.open(tmp_path / "krubit.db")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


async def test_claim_mention_budget_is_atomic_under_concurrency(store: SQLiteStore) -> None:
    """Ten concurrent claims against a budget of one must yield exactly one winner."""

    async def attempt() -> bool:
        return await store.claim_mention_budget(
            guild_id=GUILD_ID,
            budget_kind="live_everyone",
            period_key="2026-08-04",
            limit=1,
            now=NOW,
        )

    results = await asyncio.gather(*(attempt() for _ in range(10)))
    assert sum(results) == 1
    assert await store.mention_budget_consumed(GUILD_ID, "live_everyone", "2026-08-04") == 1


async def test_claim_mention_budget_rejects_once_limit_reached(store: SQLiteStore) -> None:
    first = await store.claim_mention_budget(
        guild_id=GUILD_ID, budget_kind="social_role", period_key="p1", limit=2, now=NOW
    )
    second = await store.claim_mention_budget(
        guild_id=GUILD_ID, budget_kind="social_role", period_key="p1", limit=2, now=NOW
    )
    third = await store.claim_mention_budget(
        guild_id=GUILD_ID, budget_kind="social_role", period_key="p1", limit=2, now=NOW
    )
    assert (first, second, third) == (True, True, False)


async def test_mention_budgets_are_tracked_separately_per_kind(store: SQLiteStore) -> None:
    live_claim = await store.claim_mention_budget(
        guild_id=GUILD_ID, budget_kind="live_everyone", period_key="p1", limit=1, now=NOW
    )
    social_claim = await store.claim_mention_budget(
        guild_id=GUILD_ID, budget_kind="social_role", period_key="p1", limit=1, now=NOW
    )
    assert live_claim is True
    assert social_claim is True
    assert await store.mention_budget_consumed(GUILD_ID, "live_everyone", "p1") == 1
    assert await store.mention_budget_consumed(GUILD_ID, "social_role", "p1") == 1


def _live_everyone_claim(store: SQLiteStore, *, period_key: str = "p1"):
    async def claim() -> bool:
        return await store.claim_mention_budget(
            guild_id=GUILD_ID,
            budget_kind="live_everyone",
            period_key=period_key,
            limit=1,
            now=NOW,
        )

    return claim


async def test_evaluate_and_claim_downgrades_to_none_when_budget_claim_loses_race(
    store: SQLiteStore,
) -> None:
    """The decide+claim composition, not just `claim_mention_budget` in isolation.

    Both calls independently *decide* EVERYONE is available (each policy object is
    built with its own `MentionBudgetState(limit=1, consumed=0)` snapshot, exactly as
    a real caller would after separately reading storage) — but only one of them can
    actually win the underlying atomic claim against the same real budget row. The
    loser's overall decision must come back NONE, proving the claim result — not the
    stale snapshot — determines what mention actually ships.
    """
    claim = _live_everyone_claim(store)
    winner_policy = policy(live_everyone_budget=1)
    loser_policy = policy(live_everyone_budget=1)  # same limit=1, consumed=0 snapshot

    winner = await winner_policy.evaluate_and_claim(live_event("s1"), NOW, claim_mention=claim)
    loser = await loser_policy.evaluate_and_claim(live_event("s2"), LATER, claim_mention=claim)

    assert winner.mention is MentionKind.EVERYONE
    assert loser.mention is MentionKind.NONE
    assert loser.mention_role_id is None
    assert loser.disposition is DeliveryDisposition.DELIVER  # suppression never blocks delivery


async def test_evaluate_and_claim_never_double_awards_everyone_under_concurrency(
    store: SQLiteStore,
) -> None:
    """Ten concurrent `evaluate_and_claim` calls against a budget of one: exactly one EVERYONE."""
    claim = _live_everyone_claim(store, period_key="p2")

    async def attempt(index: int) -> DeliveryDecision:
        return await policy(live_everyone_budget=1).evaluate_and_claim(
            live_event(f"s{index}"), NOW, claim_mention=claim
        )

    decisions = await asyncio.gather(*(attempt(i) for i in range(10)))
    everyone_count = sum(1 for decision in decisions if decision.mention is MentionKind.EVERYONE)
    none_count = sum(1 for decision in decisions if decision.mention is MentionKind.NONE)
    assert everyone_count == 1
    assert none_count == 9
    assert all(decision.disposition is DeliveryDisposition.DELIVER for decision in decisions)


async def test_evaluate_and_claim_does_not_call_claim_when_mention_is_already_none(
    store: SQLiteStore,
) -> None:
    """No route mention configured -> mention is NONE without ever touching the budget."""

    async def unexpected_claim() -> bool:
        raise AssertionError("claim_mention must not be called when mention is already NONE")

    decision = await policy().evaluate_and_claim(
        social_event(), NOW, claim_mention=unexpected_claim
    )
    assert decision.mention is MentionKind.NONE


async def test_mention_receipts_record_every_outcome(store: SQLiteStore) -> None:
    await store.record_mention_receipt(
        guild_id=GUILD_ID,
        receipt_id="mention:1",
        budget_kind="live_everyone",
        period_key="p1",
        outcome="consumed",
        platform=Platform.YOUTUBE,
        external_id="s1",
        created_at=NOW,
    )
    await store.record_mention_receipt(
        guild_id=GUILD_ID,
        receipt_id="mention:2",
        budget_kind="live_everyone",
        period_key="p1",
        outcome="suppressed",
        platform=Platform.YOUTUBE,
        external_id="s2",
        created_at=LATER,
    )
    receipts = await store.list_mention_budget_receipts(GUILD_ID)
    assert [receipt.outcome for receipt in receipts] == ["suppressed", "consumed"]
