from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krubit.contracts.moderation import ModerationContractError, _required_text, _timestamp


def test_required_text_raises_on_blank():
    with pytest.raises(ModerationContractError, match="idempotency_key"):
        _required_text({"idempotency_key": "  "}, "idempotency_key")


def test_required_text_returns_stripped_value():
    assert _required_text({"case_id": " case:1 "}, "case_id") == "case:1"


def test_timestamp_requires_timezone():
    with pytest.raises(ModerationContractError, match="timezone"):
        _timestamp("2026-01-01T00:00:00")


def test_timestamp_parses_and_normalizes_to_utc():
    parsed = _timestamp("2026-01-01T00:00:00+00:00")
    assert parsed == datetime(2026, 1, 1, tzinfo=UTC)
