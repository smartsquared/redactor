"""The ``redactor init`` / ``redactor status`` CLI (story S0.1).

Acceptance criteria exercised here:
  - init→status→open round trip with a keyring-backed key (keyring mocked);
  - wrong/missing key paths fail with actionable messages;
  - no code path prints or logs the key.

``--key`` / ``$REDACTOR_KEY`` remain as CI/test overrides; the keychain is the
human default so no passphrase lives in shell history or the environment.
"""
from __future__ import annotations

import pytest

from redactor import keychain
from redactor.cli import main
from redactor.store import BadKeyError, open_store


class FakeKeyring:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self._store.get((service, account))

    def set_password(self, service, account, password):
        self._store[(service, account)] = password

    def delete_password(self, service, account):
        del self._store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setattr(keychain, "_default_backend", lambda: backend)
    # Make sure no ambient override leaks in from the environment.
    monkeypatch.delenv("REDACTOR_KEY", raising=False)
    return backend


# -- init → status → open round trip (keyring-backed key) ---------------------


def test_init_status_open_roundtrip_with_keyring(tmp_path, fake_keyring, capsys):
    db = tmp_path / "store.db"

    rc = main(["init", "--store", str(db)])
    assert rc == 0
    assert db.exists()

    # The key was custodied in the (mocked) keychain — not asked of the user.
    key = keychain.get_key(db, backend=fake_keyring)
    assert key is not None

    # status reports the store as ready without touching data.
    rc = main(["status", "--store", str(db)])
    assert rc == 0
    status_out = capsys.readouterr().out.lower()
    assert "ready" in status_out or "available" in status_out

    # The keyring-custodied key opens the store and round-trips a mapping row.
    with open_store(db, key, create=False) as store:
        store.mapping.put("PAYEE-7", "PAYEE", "Bank of Nowhere Grocery")
        assert store.mapping.resolve_alias("PAYEE-7").reveal() == "Bank of Nowhere Grocery"


def test_init_refuses_to_clobber_existing_store(tmp_path, fake_keyring, capsys):
    db = tmp_path / "store.db"
    assert main(["init", "--store", str(db)]) == 0
    key = keychain.get_key(db, backend=fake_keyring)

    rc = main(["init", "--store", str(db)])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "exist" in err or "already" in err
    # The custodied key is untouched, so the existing store is still openable.
    assert keychain.get_key(db, backend=fake_keyring) == key


# -- overrides (--key / $REDACTOR_KEY) ----------------------------------------


def test_init_with_key_override_does_not_touch_keychain(tmp_path, fake_keyring):
    db = tmp_path / "store.db"
    rc = main(["init", "--store", str(db), "--key", "ci-override-key"])
    assert rc == 0
    # An explicit override is a CI/test key: it must not be written to custody.
    assert keychain.get_key(db, backend=fake_keyring) is None
    with open_store(db, "ci-override-key", create=False) as store:
        assert store.schema_version >= 1


def test_status_uses_env_override(tmp_path, fake_keyring, monkeypatch, capsys):
    db = tmp_path / "store.db"
    assert main(["init", "--store", str(db), "--key", "env-key"]) == 0

    monkeypatch.setenv("REDACTOR_KEY", "env-key")
    rc = main(["status", "--store", str(db)])
    assert rc == 0


# -- wrong / missing key failure paths ----------------------------------------


def test_status_on_uninitialized_store_is_actionable(tmp_path, fake_keyring, capsys):
    db = tmp_path / "nope.db"
    rc = main(["status", "--store", str(db)])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "init" in err or "not" in err


def test_status_without_key_is_actionable(tmp_path, fake_keyring, capsys):
    db = tmp_path / "store.db"
    # Create the store with an override, but leave nothing in custody and no env.
    assert main(["init", "--store", str(db), "--key", "orphan-key"]) == 0

    rc = main(["status", "--store", str(db)])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "key" in err


def test_status_reports_wrong_key_actionably(tmp_path, fake_keyring, monkeypatch, capsys):
    db = tmp_path / "store.db"
    assert main(["init", "--store", str(db), "--key", "right-key"]) == 0

    monkeypatch.setenv("REDACTOR_KEY", "wrong-key")
    rc = main(["status", "--store", str(db)])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "key" in err


def test_init_reports_actionable_error_when_keychain_unavailable(tmp_path, monkeypatch, capsys):
    def boom():
        raise keychain.KeychainUnavailable("the 'keyring' package is not installed")

    monkeypatch.setattr(keychain, "_default_backend", boom)
    monkeypatch.delenv("REDACTOR_KEY", raising=False)
    db = tmp_path / "store.db"

    rc = main(["init", "--store", str(db)])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "keyring" in err or "redactor_key" in err
    # Nothing half-built: a failed custody must not leave a store behind.
    assert not db.exists()


# -- the key must never be printed or logged ----------------------------------


def test_no_command_prints_the_key(tmp_path, fake_keyring, capsys):
    db = tmp_path / "store.db"
    assert main(["init", "--store", str(db)]) == 0
    init_cap = capsys.readouterr()

    key = keychain.get_key(db, backend=fake_keyring)
    assert key  # sanity

    assert key not in init_cap.out
    assert key not in init_cap.err

    assert main(["status", "--store", str(db)]) == 0
    status_cap = capsys.readouterr()
    assert key not in status_cap.out
    assert key not in status_cap.err


def test_bad_key_error_message_never_contains_the_key(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, "the-real-key", create=True):
        pass
    with pytest.raises(BadKeyError) as exc:
        open_store(db, "the-real-key-but-wrong", create=False)
    assert "the-real-key" not in str(exc.value)
