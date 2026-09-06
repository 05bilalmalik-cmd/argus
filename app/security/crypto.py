from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

_PREFIX = "enc:v1:"


class CryptoBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_path(cls, path: Path) -> "CryptoBox":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            key = path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, key + b"\n")
            finally:
                os.close(descriptor)
        if os.name != "nt":
            os.chmod(path, 0o600)
        return cls(key)

    def encrypt(self, value: str) -> str:
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return f"{_PREFIX}{token}"

    def decrypt(self, token: str) -> str:
        if not token.startswith(_PREFIX):
            raise ValueError("Expected an encrypted value with enc:v1 prefix")
        value = self._fernet.decrypt(token.removeprefix(_PREFIX).encode("ascii"))
        return value.decode("utf-8")
