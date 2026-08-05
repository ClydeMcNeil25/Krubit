"""Tests for the pure, deterministic risk-band evaluation function.

`evaluate_risk_band` is safety-sensitive: it is the sole place that maps observed
signals to a `RiskBand`, and every result must be reproducible and fully explained
from the signals given. These tests pin down both the documented threshold
boundaries (see `watchdog.py` module docstring for the rationale) and the
determinism/explainability guarantees the design mandates.
"""

from __future__ import annotations

from krubit.domain.watchdog import RiskBand, RiskSignal, evaluate_risk_band


def test_evaluate_risk_band_is_deterministic_for_identical_signals() -> None:
    signals = (RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),)
    first = evaluate_risk_band(signals)
    second = evaluate_risk_band(signals)
    assert first == second


def test_evaluate_risk_band_explanation_names_every_contributing_signal() -> None:
    signals = (
        RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),
        RiskSignal(name="join_velocity", weight=4, detail="12 joins in 60s", confidence=0.8),
    )
    band, explanation = evaluate_risk_band(signals)
    assert band is RiskBand.SUSPICIOUS
    assert "account_age" in explanation
    assert "join_velocity" in explanation


def test_evaluate_risk_band_with_no_signals_is_clear() -> None:
    assert evaluate_risk_band(()) == (RiskBand.CLEAR, "no signals observed")


def test_evaluate_risk_band_single_low_confidence_signal_stays_watch() -> None:
    # effective weight = 3 * 0.5 = 1.5, well under the SUSPICIOUS threshold.
    signals = (
        RiskSignal(name="default_avatar", weight=3, detail="default avatar", confidence=0.5),
    )
    band, explanation = evaluate_risk_band(signals)
    assert band is RiskBand.WATCH
    assert "default_avatar" in explanation


def test_evaluate_risk_band_zero_confidence_signal_still_elevates_from_clear() -> None:
    # A signal fired at all, even with zero confidence, means Entry Sniff observed
    # something atypical about the join; CLEAR is reserved exclusively for "no
    # signals observed" per the design doc's Risk Bands section.
    signals = (RiskSignal(name="unverified_flag", weight=5, detail="unverified", confidence=0.0),)
    band, _ = evaluate_risk_band(signals)
    assert band is RiskBand.WATCH


def test_evaluate_risk_band_high_weight_high_confidence_signal_reaches_incident() -> None:
    # effective weight = 8 * 0.9 = 7.2, over the INCIDENT threshold on its own.
    signals = (
        RiskSignal(
            name="known_raid_pattern",
            weight=8,
            detail="matches known raid signature",
            confidence=0.9,
        ),
    )
    band, explanation = evaluate_risk_band(signals)
    assert band is RiskBand.INCIDENT
    assert "known_raid_pattern" in explanation


def test_evaluate_risk_band_accumulates_multiple_moderate_signals_into_incident() -> None:
    signals = (
        RiskSignal(name="account_age", weight=5, detail="account 5m old", confidence=0.9),
        RiskSignal(name="join_velocity", weight=5, detail="30 joins in 30s", confidence=0.9),
    )
    band, _ = evaluate_risk_band(signals)
    # effective weight = 4.5 + 4.5 = 9.0, over the INCIDENT threshold.
    assert band is RiskBand.INCIDENT


def test_evaluate_risk_band_explanation_reports_band_value() -> None:
    signals = (RiskSignal(name="account_age", weight=3, detail="account 2h old", confidence=0.9),)
    band, explanation = evaluate_risk_band(signals)
    assert band.value in explanation
