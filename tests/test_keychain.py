"""Tests for the OS-keychain key-custody seam (story S0.1).

ADR-0001 "Key custody": SQLCipher answers *how* the store is encrypted; the
keychain answers *where the key lives*. This module is the ``keyring`` seam —
the real backend is the OS keychain (macOS first), and tests inject an in-memory
fake so no real secret store is touched in CI.
"""
from __future__ import annotations

import pytest

from redactor import keychain


class FakeKeyring:
    """An in-memory stand-in for the ``keyring`` backend, keyed like the real
    one on ``(service, account)``."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        del self._store[(service, account)]


def test_generated_key_is_high_entropy_and_distinct():
    a = keychain.generate_key()
    b = keychain.generate_key()
    assert a != b
    # token_urlsafe(32) is >= 43 chars of URL-safe base64.
    assert len(a) >= 43


def test_set_then_get_key_roundtrips(tmp_path):
    backend = FakeKeyring()
    db = tmp_path / "store.db"
    key = keychain.generate_key()

    keychain.set_key(db, key, backend=backend)
    assert keychain.get_key(db, backend=backend) == key


def test_get_key_is_none_when_absent(tmp_path):
    backend = FakeKeyring()
    assert keychain.get_key(tmp_path / "store.db", backend=backend) is None


def test_key_is_scoped_per_store_path(tmp_path):
    backend = FakeKeyring()
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    keychain.set_key(a, "key-a", backend=backend)
    keychain.set_key(b, "key-b", backend=backend)
    assert keychain.get_key(a, backend=backend) == "key-a"
    assert keychain.get_key(b, backend=backend) == "key-b"


def test_delete_key_removes_it(tmp_path):
    backend = FakeKeyring()
    db = tmp_path / "store.db"
    keychain.set_key(db, "k", backend=backend)
    keychain.delete_key(db, backend=backend)
    assert keychain.get_key(db, backend=backend) is None


def test_missing_keyring_library_raises_actionable_error(monkeypatch):
    def boom():
        raise keychain.KeychainUnavailable(
            "the 'keyring' package is not installed"
        )

    monkeypatch.setattr(keychain, "_default_backend", boom)
    with pytest.raises(keychain.KeychainUnavailable) as exc:
        keychain.get_key("/tmp/whatever.db")
    assert "keyring" in str(exc.value).lower()
