"""Versioned, scope-separated authenticated encryption and private lookup tags."""

import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecurityError(Exception):
    """Deliberately payload-free security failure."""


class Scope(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class Purpose(StrEnum):
    CONTENT = "content"
    ROUTING = "routing"
    INDEX = "index"


def frame(*values: str) -> bytes:
    return json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def validate_storage_paths(data_dir: Path, secrets_dir: Path) -> None:
    data, secrets = data_dir.resolve(), secrets_dir.resolve()
    if data == secrets or data in secrets.parents or secrets in data.parents:
        raise SecurityError("Data and secrets directories must be separate.")


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SecurityError("Symbolic storage paths are not supported.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SecurityError("Private directory permissions are required.")


def create_keyring(path: Path) -> None:
    """Exclusive creation: never replace or regenerate an existing key file."""
    private_directory(path.parent)
    keys = {
        f"{scope}.{purpose}": base64.b64encode(os.urandom(32)).decode("ascii")
        for scope in Scope
        for purpose in Purpose
    }
    payload = json.dumps({"format": 1, "active": "v1", "versions": {"v1": keys}})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class Keyring:
    def __init__(self, active: str, versions: dict[str, dict[str, bytes]]) -> None:
        self.active = active
        self._versions = versions

    def __repr__(self) -> str:
        return "Keyring(<redacted>)"

    @classmethod
    def load(cls, path: Path) -> "Keyring":
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError
            if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError
            if path.stat().st_size > 65536:
                raise ValueError
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["format"] != 1:
                raise ValueError
            versions = {}
            all_keys = set()
            expected = {f"{scope}.{purpose}" for scope in Scope for purpose in Purpose}
            for version, keys in document["versions"].items():
                if not version.isascii() or not version.isalnum() or len(version) > 16:
                    raise ValueError
                if set(keys) != expected:
                    raise ValueError
                decoded = {}
                for name, value in keys.items():
                    key = base64.b64decode(value, validate=True)
                    if len(key) != 32 or key in all_keys:
                        raise ValueError
                    decoded[name] = key
                    all_keys.add(key)
                versions[version] = decoded
            if document["active"] not in versions:
                raise ValueError
            return cls(document["active"], versions)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            raise SecurityError("Key file is missing, invalid, or insecure.") from None

    def key(self, scope: Scope, purpose: Purpose, version: str) -> bytes:
        try:
            return self._versions[version][f"{Scope(scope)}.{Purpose(purpose)}"]
        except (KeyError, ValueError):
            raise SecurityError("Unknown encryption context.") from None


class ScopeCipher:
    """AAD prevents ciphertext being moved between records, fields, or domains."""

    def __init__(self, keyring: Keyring, scope: Scope) -> None:
        self._keyring = keyring
        self.scope = Scope(scope)

    def encrypt(self, plaintext: bytes, *, record: str, field: str, purpose: Purpose) -> bytes:
        if purpose not in {Purpose.CONTENT, Purpose.ROUTING}:
            raise SecurityError("Invalid encryption purpose.")
        version = self._keyring.active
        nonce = os.urandom(12)
        aad = frame("AFB1", version, self.scope, purpose, record, field)
        payload = AESGCM(self._keyring.key(self.scope, purpose, version)).encrypt(
            nonce, plaintext, aad
        )
        return b"AFB1:" + version.encode("ascii") + b":" + nonce + payload

    def decrypt(self, ciphertext: bytes, *, record: str, field: str, purpose: Purpose) -> bytes:
        try:
            if purpose not in {Purpose.CONTENT, Purpose.ROUTING}:
                raise ValueError
            magic, version_bytes, payload = ciphertext.split(b":", 2)
            version = version_bytes.decode("ascii")
            if magic != b"AFB1" or len(payload) < 28:
                raise ValueError
            aad = frame("AFB1", version, self.scope, purpose, record, field)
            key = self._keyring.key(self.scope, purpose, version)
            return AESGCM(key).decrypt(payload[:12], payload[12:], aad)
        except (InvalidTag, ValueError, TypeError, AttributeError, SecurityError):
            raise SecurityError("Encrypted data could not be authenticated.") from None

    def tag(self, namespace: str, *parts: str, version: str | None = None) -> bytes:
        version = version or self._keyring.active
        key = self._keyring.key(self.scope, Purpose.INDEX, version)
        return hmac.digest(key, frame("AFB1", self.scope, namespace, *parts), hashlib.sha256)
