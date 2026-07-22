"""Tests for the local encrypted store + mapping table schema (story S0.3).

Acceptance criteria exercised here:
  - schema module with create / open / migrate
  - store file is unreadable without the key
"""
import sqlite3

import pytest

from redactor.store import (
    SCHEMA_VERSION,
    BadKeyError,
    StoreError,
    open_store,
)

KEY = "correct horse battery staple"
SECRET_PAYEE = "WHOLE FOODS MARKET #1029 SEATTLE WA"


def test_create_then_open_roundtrips_data(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", SECRET_PAYEE)

    # Reopen with the same key: data survives, schema is at current version.
    with open_store(db, KEY, create=False) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.mapping.resolve_alias("PAYEE-7").reveal() == SECRET_PAYEE


def test_open_missing_file_without_create_raises(tmp_path):
    with pytest.raises(StoreError):
        open_store(tmp_path / "nope.db", KEY, create=False)


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        before = store.schema_version
        store.migrate()  # running again is a no-op
        assert store.schema_version == before == SCHEMA_VERSION


def test_wrong_key_is_rejected(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", SECRET_PAYEE)
    with pytest.raises(BadKeyError):
        open_store(db, "wrong key", create=False)


def test_store_file_is_encrypted_on_disk(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", SECRET_PAYEE)

    raw = db.read_bytes()
    # A plaintext SQLite file starts with this magic; an encrypted one must not.
    assert not raw.startswith(b"SQLite format 3\x00")
    # The real value must not appear anywhere in the on-disk bytes.
    assert SECRET_PAYEE.encode() not in raw
    assert b"PAYEE-7" not in raw


def test_plain_sqlite_cannot_read_the_store(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", SECRET_PAYEE)

    with pytest.raises(sqlite3.DatabaseError):
        conn = sqlite3.connect(db)
        conn.execute("SELECT * FROM alias_mapping").fetchall()


def test_raw_transactions_roundtrip(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.add_raw_transaction(
            account_id="ACCT-XXXX",
            posted_date="2026-06-01",
            amount_cents=-4213,
            raw_payee="WHOLEFDS #1029 SEA",
            raw_memo="PURCHASE",
            source_file="checking-2026-06.csv",
        )
        rows = list(store.raw_transactions())
    assert len(rows) == 1
    assert rows[0]["raw_payee"] == "WHOLEFDS #1029 SEA"


def test_raw_transaction_persists_institution(tmp_path):
    # The account -> institution link `redactor export` reconstructs INST tokens
    # from (issue #41). Omitting it is allowed and stays NULL.
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.add_raw_transaction(
            account_id="ACCT-XXXX",
            posted_date="2026-06-01",
            amount_cents=-4213,
            raw_payee="WHOLEFDS #1029 SEA",
            institution="Bank of Nowhere",
        )
        store.add_raw_transaction(
            account_id="ACCT-XXXX",
            posted_date="2026-06-02",
            amount_cents=-100,
            raw_payee="SBUX 8842 SEATTLE WA",
        )
        rows = list(store.raw_transactions())
    assert rows[0]["institution"] == "Bank of Nowhere"
    assert rows[1]["institution"] is None
