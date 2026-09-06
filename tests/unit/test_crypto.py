from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from app.security.crypto import CryptoBox


def test_crypto_box_round_trips_unicode_and_reuses_key(tmp_path: Path) -> None:
    key_path = tmp_path / "secret.key"
    first = CryptoBox.from_path(key_path)
    token = first.encrypt("Exact approved answer — Alex")
    second = CryptoBox.from_path(key_path)

    assert token.startswith("enc:v1:")
    assert second.decrypt(token) == "Exact approved answer — Alex"
    assert key_path.read_bytes().strip()


def test_wrong_key_cannot_decrypt_ciphertext(tmp_path: Path) -> None:
    one = CryptoBox.from_path(tmp_path / "one.key")
    two = CryptoBox.from_path(tmp_path / "two.key")

    with pytest.raises(InvalidToken):
        two.decrypt(one.encrypt("private"))


def test_plaintext_is_never_accepted_as_ciphertext(tmp_path: Path) -> None:
    crypto = CryptoBox.from_path(tmp_path / "secret.key")

    with pytest.raises(ValueError, match="encrypted value"):
        crypto.decrypt("private")
