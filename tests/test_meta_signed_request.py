from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from krubit.integrations.meta import verify_meta_signed_request

_SECRET = "app-secret-value"


def _sign(payload: dict[str, Any], secret: str = _SECRET) -> str:
    payload_json = json.dumps(payload)
    encoded_payload = (
        base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    )
    signature = hmac.new(
        secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def test_verify_meta_signed_request_accepts_valid_fresh_request():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signed_request = _sign(payload)
    result = verify_meta_signed_request(signed_request, _SECRET, now=lambda: now)
    assert result is not None
    assert result["user_id"] == "12345"


def test_verify_meta_signed_request_rejects_tampered_payload():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signature, _, _ = _sign(payload).partition(".")
    tampered_payload = base64.urlsafe_b64encode(
        json.dumps({**payload, "user_id": "99999"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    forged = f"{signature}.{tampered_payload}"
    assert verify_meta_signed_request(forged, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_wrong_secret():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    payload = {"algorithm": "HMAC-SHA256", "issued_at": int(now.timestamp()), "user_id": "12345"}
    signed_request = _sign(payload, secret="wrong-secret")
    assert verify_meta_signed_request(signed_request, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_stale_issued_at():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    stale_time = now - timedelta(minutes=10)
    payload = {
        "algorithm": "HMAC-SHA256",
        "issued_at": int(stale_time.timestamp()),
        "user_id": "12345",
    }
    signed_request = _sign(payload)
    assert verify_meta_signed_request(signed_request, _SECRET, now=lambda: now) is None


def test_verify_meta_signed_request_rejects_malformed_input():
    now = datetime(2026, 8, 7, tzinfo=UTC)
    result1 = verify_meta_signed_request(
        "not-a-valid-signed-request", _SECRET, now=lambda: now
    )
    assert result1 is None
    result2 = verify_meta_signed_request("", _SECRET, now=lambda: now)
    assert result2 is None
