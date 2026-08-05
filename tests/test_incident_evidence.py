"""Tests for `krubit.services.incident_evidence.build_evidence_packet`.

Two redaction-boundary decisions are load-bearing here and documented in
`incident_evidence.py`'s module docstring:

1. `build_evidence_packet` never copies a raw message's `.content` into the packet
   at all — only its `.jump_url` (a link, not the payload) and `.id` (an opaque
   identifier). This is the strongest possible redaction: content that is never
   stored can never leak, regardless of what `redact()`'s regexes do or don't catch.
2. Every `RiskSignal.detail` a caller supplies via `raw_signals` — which, unlike raw
   message content, legitimately needs to reach the packet (that is the whole point
   of an evidence packet) — is redacted before the `EvidencePacket` is constructed.

The "bypass" test at the bottom exercises `EvidencePacket.to_storage_dict()`
directly, without going through `build_evidence_packet` at all, to prove the
redaction guarantee is structural (on the type itself), not merely a habit of this
one builder function.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krubit.domain.watchdog import EvidencePacket, Incident, IncidentKind, RiskBand, RiskSignal
from krubit.services.incident_evidence import build_evidence_packet

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
GUILD_ID = 111


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int = 999,
        content: str = "hello there",
        jump_url: str = "https://discord.com/channels/111/222/999",
    ) -> None:
        self.id = message_id
        self.content = content
        self.jump_url = jump_url


def message(
    *,
    message_id: int = 999,
    content: str = "hello there",
    jump_url: str = "https://discord.com/channels/111/222/999",
) -> FakeMessage:
    return FakeMessage(message_id=message_id, content=content, jump_url=jump_url)


def incident(*, incident_id: str = "raid:test") -> Incident:
    return Incident(
        guild_id=GUILD_ID,
        incident_id=incident_id,
        kind=IncidentKind.RAID,
        band=RiskBand.INCIDENT,
        opened_at=NOW,
        evidence_packet_id="pending",
        recommended_action="Review the flagged activity; no automatic action has been taken.",
        acknowledged_by=None,
    )


def test_evidence_packet_redacts_raw_message_content_before_storage() -> None:
    packet = build_evidence_packet(
        incident(),
        raw_signals=(
            RiskSignal(
                name="raid_join_cluster", weight=8, detail="8 elevated joins", confidence=0.85
            ),
        ),
        raw_messages=(message(content="token=secret-abc"),),
    )

    assert "secret-abc" not in str(packet.to_storage_dict())


def test_evidence_packet_never_carries_raw_message_content_at_all() -> None:
    """Belt-and-suspenders: the packet's fields never include message.content in any
    form, not even redacted — see the module docstring's redaction-boundary note.
    """
    packet = build_evidence_packet(
        incident(),
        raw_signals=(RiskSignal(name="s", weight=3, detail="d", confidence=0.5),),
        raw_messages=(message(content="totally unique marker XYZZY-CONTENT"),),
    )

    assert "XYZZY-CONTENT" not in str(packet.to_storage_dict())
    assert "XYZZY-CONTENT" not in str(packet)


def test_evidence_packet_redacts_signal_detail_before_storage() -> None:
    """`raw_signals` detail text legitimately needs to reach the packet (it's the
    named evidence for the incident) — but any secret-shaped substring inside it must
    still be redacted before the packet's storage representation is produced.
    """
    packet = build_evidence_packet(
        incident(),
        raw_signals=(
            RiskSignal(
                name="url_risk",
                weight=5,
                detail="message body quoted: password=hunter2-plaintext",
                confidence=0.6,
            ),
        ),
        raw_messages=(),
    )

    storage_repr = str(packet.to_storage_dict())
    assert "hunter2-plaintext" not in storage_repr
    assert "[REDACTED]" in storage_repr


def test_evidence_packet_message_links_and_event_ids_come_from_raw_messages() -> None:
    packet = build_evidence_packet(
        incident(),
        raw_signals=(RiskSignal(name="s", weight=3, detail="d", confidence=0.5),),
        raw_messages=(message(message_id=555, jump_url="https://discord.com/channels/1/2/555"),),
    )

    assert packet.message_links == ("https://discord.com/channels/1/2/555",)
    assert packet.event_ids == ("555",)
    assert packet.guild_id == GUILD_ID
    assert packet.incident_id == "raid:test"


def test_build_evidence_packet_requires_at_least_one_signal() -> None:
    with pytest.raises(ValueError):
        build_evidence_packet(incident(), raw_signals=(), raw_messages=())


def test_build_evidence_packet_rejects_non_tuple_signals() -> None:
    with pytest.raises((TypeError, ValueError)):
        build_evidence_packet(incident(), raw_signals=[], raw_messages=())  # type: ignore[arg-type]


def test_evidence_packet_to_storage_dict_redacts_even_when_constructed_directly() -> None:
    """The 'bypass' test: build an `EvidencePacket` by hand, skipping
    `build_evidence_packet` entirely, and confirm `to_storage_dict()` still redacts.
    This is what proves the guarantee is structural on `EvidencePacket` itself, not
    merely a habit of the one builder function above.
    """
    packet = EvidencePacket(
        guild_id=GUILD_ID,
        incident_id="raid:bypass",
        signals=(
            RiskSignal(
                name="leaked",
                weight=3,
                detail="bot_token: abcd1234plaintextvalue",
                confidence=0.5,
            ),
        ),
        message_links=("https://discord.com/channels/1/2/3",),
        event_ids=("evt-1",),
        created_at=NOW,
    )

    storage_repr = str(packet.to_storage_dict())
    assert "abcd1234plaintextvalue" not in storage_repr
    assert "[REDACTED]" in storage_repr
