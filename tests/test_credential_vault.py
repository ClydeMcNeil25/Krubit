import pytest

from krubit.security.credential_vault import CredentialVault, CredentialVaultError

_TEST_KEY = "test-only-credential-encryption-key-material"


def vault() -> CredentialVault:
    return CredentialVault.from_env_key(_TEST_KEY)


def test_vault_ciphertext_is_versioned_and_does_not_contain_plaintext() -> None:
    sealed = vault().seal(b"creator-refresh-token")
    assert sealed.startswith("v1:")
    assert "creator-refresh-token" not in sealed
    assert vault().open(sealed) == b"creator-refresh-token"


def test_vault_produces_a_fresh_nonce_for_each_seal() -> None:
    first = vault().seal(b"same-plaintext")
    second = vault().seal(b"same-plaintext")
    assert first != second
    assert vault().open(first) == b"same-plaintext"
    assert vault().open(second) == b"same-plaintext"


def test_vault_rejects_ciphertext_from_a_different_key() -> None:
    sealed = vault().seal(b"creator-refresh-token")
    other = CredentialVault.from_env_key("a-completely-different-key-material")
    with pytest.raises(CredentialVaultError):
        other.open(sealed)


def test_vault_rejects_tampered_ciphertext() -> None:
    sealed = vault().seal(b"creator-refresh-token")
    tampered = sealed[:-1] + ("A" if sealed[-1] != "A" else "B")
    with pytest.raises(CredentialVaultError):
        vault().open(tampered)


def test_vault_rejects_unversioned_or_malformed_input() -> None:
    with pytest.raises(CredentialVaultError):
        vault().open("not-a-sealed-value")
    with pytest.raises(CredentialVaultError):
        vault().open("v2:whatever")


def test_vault_rejects_blank_key_material() -> None:
    with pytest.raises(CredentialVaultError):
        CredentialVault.from_env_key("   ")
