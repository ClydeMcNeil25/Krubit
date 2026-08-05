"""Unit tests for `krubit.discord.watchdog_events.extract_message_signals`.

Pure-function tests: no Discord objects, no I/O, no storage — matching the
`test_entry_sniff_extraction.py` convention for `discord/*` extraction functions. Uses
a small local `FakeMessage`-shaped factory rather than mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from krubit.discord.watchdog_events import extract_message_signals
from krubit.domain.watchdog import RiskBand, evaluate_risk_band

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeMessage:
    def __init__(
        self,
        *,
        content: str = "hello there",
        mention_count: int = 0,
        role_mention_count: int = 0,
        mention_everyone: bool = False,
    ) -> None:
        self.content = content
        self.mentions = [object() for _ in range(mention_count)]
        self.role_mentions = [object() for _ in range(role_mention_count)]
        self.mention_everyone = mention_everyone


def message(
    *,
    content: str = "hello there",
    mention_count: int = 0,
    role_mention_count: int = 0,
    mention_everyone: bool = False,
) -> FakeMessage:
    return FakeMessage(
        content=content,
        mention_count=mention_count,
        role_mention_count=role_mention_count,
        mention_everyone=mention_everyone,
    )


def test_extract_message_signals_flags_mass_mentions_and_repeated_content() -> None:
    signals = extract_message_signals(
        message(mention_count=25, content="buy now buy now buy now"), now=NOW
    )
    assert any(s.name == "mass_mentions" for s in signals)
    assert any(s.name == "repeated_content" for s in signals)


def test_unremarkable_message_produces_no_signals() -> None:
    signals = extract_message_signals(message(content="hey, how's everyone doing today?"), now=NOW)
    assert signals == ()


def test_few_mentions_do_not_trigger_mass_mentions() -> None:
    signals = extract_message_signals(message(mention_count=3, content="hi @a @b @c"), now=NOW)
    assert not any(s.name == "mass_mentions" for s in signals)


def test_elevated_mention_count_is_a_weaker_signal_than_high_count() -> None:
    elevated = extract_message_signals(message(mention_count=8), now=NOW)
    high = extract_message_signals(message(mention_count=15), now=NOW)
    elevated_signal = next(s for s in elevated if s.name == "mass_mentions")
    high_signal = next(s for s in high if s.name == "mass_mentions")
    elevated_effective = elevated_signal.weight * elevated_signal.confidence
    high_effective = high_signal.weight * high_signal.confidence
    assert elevated_effective < high_effective


def test_mention_everyone_flags_mass_mentions() -> None:
    signals = extract_message_signals(
        message(mention_everyone=True, content="@everyone hi"), now=NOW
    )
    assert any(s.name == "mass_mentions" for s in signals)


def test_everyone_mention_alone_reaches_suspicious_band_by_design() -> None:
    """The one deliberate exception to "no single message-signal escalates alone".

    A bare `@everyone`/`@here` ping is already maximally disruptive to the guild on
    its own, so `mass_mentions`'s HIGH tier (weight 6 @ confidence 0.7 = effective
    4.2) is intentionally allowed to clear `_SUSPICIOUS_THRESHOLD` (3.0) by itself —
    see the "Message-signal thresholds" section of `watchdog_events.py`'s module
    docstring for the full rationale and its precedent in `extract_join_signals`'s
    `account_age`/`join_velocity` HIGH tiers.
    """
    signals = extract_message_signals(message(mention_everyone=True), now=NOW)
    band, _ = evaluate_risk_band(signals)
    assert band in (RiskBand.SUSPICIOUS, RiskBand.INCIDENT)


def test_high_tier_explicit_mention_count_alone_reaches_suspicious_band_by_design() -> None:
    """Same exception, reached via >= 15 explicit mentions instead of @everyone."""
    signals = extract_message_signals(message(mention_count=15), now=NOW)
    band, _ = evaluate_risk_band(signals)
    assert band in (RiskBand.SUSPICIOUS, RiskBand.INCIDENT)


def test_elevated_mention_count_alone_stays_at_watch_band() -> None:
    """The ELEVATED tier (below HIGH) is NOT part of the exception — it must never,
    by itself, clear `_SUSPICIOUS_THRESHOLD` alone, matching the general rule.
    """
    signals = extract_message_signals(message(mention_count=8), now=NOW)
    band, _ = evaluate_risk_band(signals)
    assert band is RiskBand.WATCH


def test_known_shortener_domain_flags_malicious_link_shape() -> None:
    signals = extract_message_signals(
        message(content="check this out https://bit.ly/abc123"), now=NOW
    )
    assert any(s.name == "malicious_link_shape" for s in signals)


def test_bare_ip_host_flags_malicious_link_shape() -> None:
    signals = extract_message_signals(
        message(content="free nitro at http://192.168.1.55/claim"), now=NOW
    )
    assert any(s.name == "malicious_link_shape" for s in signals)


def test_userinfo_trick_flags_malicious_link_shape() -> None:
    signals = extract_message_signals(
        message(content="login here https://discord.com@evil-phish.example/steal"), now=NOW
    )
    assert any(s.name == "malicious_link_shape" for s in signals)


def test_ordinary_https_link_does_not_flag_malicious_link_shape() -> None:
    signals = extract_message_signals(
        message(content="check out https://discord.com/blog/announcement"), now=NOW
    )
    assert not any(s.name == "malicious_link_shape" for s in signals)


def test_short_casual_repeat_does_not_flag_repeated_content() -> None:
    signals = extract_message_signals(message(content="lol lol"), now=NOW)
    assert not any(s.name == "repeated_content" for s in signals)


def test_varied_message_does_not_flag_repeated_content() -> None:
    signals = extract_message_signals(
        message(content="thanks for the invite, excited to be here!"), now=NOW
    )
    assert not any(s.name == "repeated_content" for s in signals)
