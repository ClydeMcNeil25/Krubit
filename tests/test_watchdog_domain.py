"""Tests for the Watchdog domain value objects (frozen dataclasses and enums).

Covers construction, immutability, and `__post_init__` validation for every type in
`krubit.domain.watchdog` other than `evaluate_risk_band` itself (see
`test_risk_band_evaluation.py` for that). No I/O, no Discord objects — pure value
object tests only.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from krubit.domain.watchdog import (
    AllowBlockEntry,
    EntrySniffAssessment,
    EvidencePacket,
    Incident,
    IncidentKind,
    RiskBand,
    RiskSignal,
    WatchWindow,
    WatchWindowCloseReason,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _signal(**overrides: object) -> RiskSignal:
    fields: dict[str, object] = {
        "name": "account_age",
        "weight": 3,
        "detail": "account 2h old",
        "confidence": 0.9,
    }
    merged = {**fields, **overrides}
    return RiskSignal(**merged)  # type: ignore[arg-type]


def test_risk_band_enumerates_bands_in_ascending_severity() -> None:
    assert [band.value for band in RiskBand] == ["clear", "watch", "suspicious", "incident"]


def test_watch_window_close_reason_enumerates_close_reasons() -> None:
    assert [reason.value for reason in WatchWindowCloseReason] == [
        "expired",
        "escalated",
        "staff_override",
    ]


def test_incident_kind_enumerates_detection_categories() -> None:
    assert [kind.value for kind in IncidentKind] == [
        "member",
        "raid",
        "spam_wave",
        "webhook_abuse",
        "permission_risk",
    ]


def test_risk_signal_is_frozen() -> None:
    signal = _signal()
    with pytest.raises(FrozenInstanceError):
        signal.weight = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "   "},
        {"detail": ""},
        {"weight": 0},
        {"weight": -1},
        {"weight": 11},
        {"confidence": -0.01},
        {"confidence": 1.01},
    ],
)
def test_risk_signal_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _signal(**overrides)


def test_risk_signal_accepts_boundary_weight_and_confidence() -> None:
    assert _signal(weight=1, confidence=0.0).weight == 1
    assert _signal(weight=10, confidence=1.0).confidence == 1.0


def test_entry_sniff_assessment_round_trips_fields() -> None:
    assessment = EntrySniffAssessment(
        guild_id=1,
        member_id=2,
        joined_at=_NOW,
        band=RiskBand.WATCH,
        signals=(_signal(),),
        explanation="watch band: ...",
        created_at=_NOW,
    )
    assert assessment.band is RiskBand.WATCH
    assert assessment.signals == (_signal(),)


@pytest.mark.parametrize(
    "overrides",
    [
        {"guild_id": 0},
        {"member_id": 0},
        {"explanation": ""},
    ],
)
def test_entry_sniff_assessment_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "guild_id": 1,
        "member_id": 2,
        "joined_at": _NOW,
        "band": RiskBand.CLEAR,
        "signals": (),
        "explanation": "no signals observed",
        "created_at": _NOW,
    }
    merged = {**fields, **overrides}
    with pytest.raises(ValueError):
        EntrySniffAssessment(**merged)  # type: ignore[arg-type]


def test_entry_sniff_assessment_requires_aware_timestamps() -> None:
    with pytest.raises(ValueError):
        EntrySniffAssessment(
            guild_id=1,
            member_id=2,
            joined_at=datetime(2026, 8, 5, 12, 0),
            band=RiskBand.CLEAR,
            signals=(),
            explanation="no signals observed",
            created_at=_NOW,
        )


def test_watch_window_opens_and_closes_cleanly() -> None:
    window = WatchWindow(
        guild_id=1,
        member_id=2,
        opened_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        band=RiskBand.WATCH,
        closed_at=None,
        close_reason=None,
    )
    assert window.closed_at is None
    assert window.close_reason is None

    closed = WatchWindow(
        guild_id=1,
        member_id=2,
        opened_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        band=RiskBand.WATCH,
        closed_at=_NOW + timedelta(minutes=30),
        close_reason=WatchWindowCloseReason.EXPIRED,
    )
    assert closed.close_reason is WatchWindowCloseReason.EXPIRED


def test_watch_window_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError):
        WatchWindow(
            guild_id=1,
            member_id=2,
            opened_at=_NOW,
            expires_at=_NOW,
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=None,
        )


def test_watch_window_rejects_mismatched_close_fields() -> None:
    with pytest.raises(ValueError):
        WatchWindow(
            guild_id=1,
            member_id=2,
            opened_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            band=RiskBand.WATCH,
            closed_at=_NOW + timedelta(minutes=5),
            close_reason=None,
        )
    with pytest.raises(ValueError):
        WatchWindow(
            guild_id=1,
            member_id=2,
            opened_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            band=RiskBand.WATCH,
            closed_at=None,
            close_reason=WatchWindowCloseReason.STAFF_OVERRIDE,
        )


def test_watch_window_never_opens_for_clear_band() -> None:
    with pytest.raises(ValueError):
        WatchWindow(
            guild_id=1,
            member_id=2,
            opened_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
            band=RiskBand.CLEAR,
            closed_at=None,
            close_reason=None,
        )


def test_incident_requires_incident_band() -> None:
    with pytest.raises(ValueError):
        Incident(
            guild_id=1,
            incident_id="inc-1",
            kind=IncidentKind.MEMBER,
            band=RiskBand.SUSPICIOUS,
            opened_at=_NOW,
            evidence_packet_id="ev-1",
            recommended_action="notify staff and recommend a manual review",
            acknowledged_by=None,
        )


def test_incident_accepts_incident_band() -> None:
    incident = Incident(
        guild_id=1,
        incident_id="inc-1",
        kind=IncidentKind.RAID,
        band=RiskBand.INCIDENT,
        opened_at=_NOW,
        evidence_packet_id="ev-1",
        recommended_action="notify staff and recommend a manual review",
        acknowledged_by=None,
    )
    assert incident.kind is IncidentKind.RAID
    assert incident.acknowledged_by is None


def test_incident_rejects_blank_recommended_action() -> None:
    with pytest.raises(ValueError):
        Incident(
            guild_id=1,
            incident_id="inc-1",
            kind=IncidentKind.RAID,
            band=RiskBand.INCIDENT,
            opened_at=_NOW,
            evidence_packet_id="ev-1",
            recommended_action="   ",
            acknowledged_by=None,
        )


def test_evidence_packet_requires_at_least_one_signal() -> None:
    with pytest.raises(ValueError):
        EvidencePacket(
            guild_id=1,
            incident_id="inc-1",
            signals=(),
            message_links=("https://discord.com/channels/1/2/3",),
            event_ids=("evt-1",),
            created_at=_NOW,
        )


def test_evidence_packet_rejects_non_https_message_links() -> None:
    with pytest.raises(ValueError):
        EvidencePacket(
            guild_id=1,
            incident_id="inc-1",
            signals=(_signal(),),
            message_links=("http://discord.com/channels/1/2/3",),
            event_ids=("evt-1",),
            created_at=_NOW,
        )


def test_evidence_packet_round_trips_fields() -> None:
    packet = EvidencePacket(
        guild_id=1,
        incident_id="inc-1",
        signals=(_signal(),),
        message_links=("https://discord.com/channels/1/2/3",),
        event_ids=("evt-1",),
        created_at=_NOW,
    )
    assert packet.message_links == ("https://discord.com/channels/1/2/3",)
    assert packet.event_ids == ("evt-1",)


def test_allow_block_entry_accepts_allow_and_block() -> None:
    allow = AllowBlockEntry(
        guild_id=1,
        discord_user_id=42,
        list_kind="allow",
        reason="verified partner",
        set_by=99,
        set_at=_NOW,
    )
    block = AllowBlockEntry(
        guild_id=1,
        discord_user_id=43,
        list_kind="block",
        reason="known raid account",
        set_by=99,
        set_at=_NOW,
    )
    assert allow.list_kind == "allow"
    assert block.list_kind == "block"


def test_allow_block_entry_rejects_unknown_list_kind() -> None:
    with pytest.raises(ValueError):
        AllowBlockEntry(
            guild_id=1,
            discord_user_id=42,
            list_kind="deny",
            reason="typo",
            set_by=99,
            set_at=_NOW,
        )
