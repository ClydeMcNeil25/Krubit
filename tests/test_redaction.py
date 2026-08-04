from datetime import UTC, datetime

import pytest

from krubit.domain.models import ActionReceipt, GuildEvent
from krubit.security.redaction import redact


def test_redact_removes_discord_token_and_assignment_secrets_recursively() -> None:
    discord_token = "A" * 24 + "." + "B" * 6 + "." + "C" * 30
    source = {
        "message": f"paste {discord_token} here",
        "nested": ["api_key=top-secret", {"password": "password: hunter2"}],
        "count": 7,
        "active": True,
        "missing": None,
    }

    result = redact(source)

    assert result == {
        "message": "paste [REDACTED_DISCORD_TOKEN] here",
        "nested": ["api_key=[REDACTED]", {"password": "password: [REDACTED]"}],
        "count": 7,
        "active": True,
        "missing": None,
    }


def test_redact_preserves_ordinary_text() -> None:
    assert redact("Krubit fetched the server status") == "Krubit fetched the server status"


def test_guild_event_rejects_blank_event_id() -> None:
    with pytest.raises(ValueError, match="event_id"):
        GuildEvent(
            event_id=" ",
            guild_id=123,
            event_type="guild_available",
            occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
            payload={},
        )


def test_domain_values_reject_nonpositive_guild_ids() -> None:
    with pytest.raises(ValueError, match="guild_id"):
        ActionReceipt(
            receipt_id="receipt-1",
            guild_id=0,
            action="status",
            status="succeeded",
            actor_id=None,
            detail={},
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        )

