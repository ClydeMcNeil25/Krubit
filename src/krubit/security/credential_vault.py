"""AES-GCM vault for creator OAuth grants and other secrets stored at rest.

`CredentialVault.seal` encrypts plaintext bytes into a versioned, self-contained text
token (`v1:<base64 nonce+ciphertext+tag>`); `CredentialVault.open` verifies and
decrypts it back. The version prefix lets a future key-rotation or algorithm change
introduce `v2:` without breaking previously sealed values. Every seal uses a fresh
random 96-bit nonce, so sealing the same plaintext twice never produces the same
ciphertext. The key is derived from an operator-supplied secret string (for example
`KRUBIT_CREDENTIAL_ENCRYPTION_KEY`) via SHA-256, so any sufficiently long random
string works as the key material without requiring a specific encoding.

`seal_json`/`open_json` are a thin convenience layer over `seal`/`open` for the
structured secrets an OAuth grant actually is (an access token, an optional refresh
token, and an expiry) — a Meta connector never needs its own JSON encoding step, and
never handles the unsealed grant as anything but a short-lived in-memory mapping.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from hashlib import sha256
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from krubit.domain.models import JSONValue

_VERSION_PREFIX = "v1:"
_NONCE_LENGTH = 12  # 96-bit nonce, the size AES-GCM is designed for.
_KEY_LENGTH = 32  # AES-256.


class CredentialVaultError(ValueError):
    """Raised for invalid vault key material or unreadable sealed values."""


def _derive_key(secret: str) -> bytes:
    if not secret.strip():
        raise CredentialVaultError("credential encryption key must not be blank")
    return sha256(secret.encode("utf-8")).digest()


class CredentialVault:
    """Seals and opens secrets with AES-256-GCM under one derived key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LENGTH:
            raise CredentialVaultError(f"credential encryption key must be {_KEY_LENGTH} bytes")
        self._aesgcm = AESGCM(key)

    @classmethod
    def from_env_key(cls, secret: str) -> CredentialVault:
        """Build a vault from an operator-supplied secret string, e.g. an env var value."""
        return cls(_derive_key(secret))

    def seal(self, plaintext: bytes) -> str:
        """Encrypt `plaintext` into a versioned, base64url-encoded text token."""
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, None)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{_VERSION_PREFIX}{payload}"

    def open(self, sealed: str) -> bytes:
        """Verify and decrypt a token produced by `seal`, raising on any tampering."""
        if not sealed.startswith(_VERSION_PREFIX):
            raise CredentialVaultError("sealed value has an unrecognized or missing version")
        encoded = sealed[len(_VERSION_PREFIX) :]
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (binascii.Error, ValueError) as exc:
            raise CredentialVaultError("sealed value is not valid base64") from exc
        if len(decoded) <= _NONCE_LENGTH:
            raise CredentialVaultError("sealed value is truncated")
        nonce, ciphertext = decoded[:_NONCE_LENGTH], decoded[_NONCE_LENGTH:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise CredentialVaultError("sealed value failed authentication") from exc

    def seal_json(self, value: Mapping[str, JSONValue]) -> str:
        """Encrypt a JSON-shaped mapping (for example an OAuth grant) into a sealed token."""
        return self.seal(json.dumps(value, sort_keys=True).encode("utf-8"))

    def open_json(self, sealed: str) -> dict[str, JSONValue]:
        """Verify, decrypt, and JSON-decode a token produced by `seal_json`."""
        plaintext = self.open(sealed)
        try:
            decoded: object = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialVaultError("sealed value is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise CredentialVaultError("sealed value is not a JSON object")
        return cast("dict[str, JSONValue]", decoded)
