"""The ``redactor ingest`` batch CLI (story S0.3).

Ingests real-world statement files into the local encrypted store and prints a
per-file summary — rows ingested, new payees, aliases issued, lint verdict —
that is **alias-space only**: no raw memo, account id, or institution name may
appear in anything the terminal prints (it could be copy-pasted into a chat).

Malformed files fail with an actionable, value-free error and never abort the
batch; the offending cell appears only behind ``--show-raw``.
"""
from __future__ import annotations

import pytest

from redactor import keychain
from redactor.cli import main
from redactor.fixtures import FIXTURES_DIR, load_manifest
from redactor.store import open_store

KEY = "correct horse battery staple"


class _FakeKeyring:
    """An empty in-memory keychain: custodies nothing, so get_key is None."""

    def get_password(self, service, account):
        return None

    def set_password(self, service, account, password):  # pragma: no cover
        pass

    def delete_password(self, service, account):  # pragma: no cover
        pass


@pytest.fixture
def empty_keychain(monkeypatch):
    monkeypatch.setattr(keychain, "_default_backend", lambda: _FakeKeyring())
    monkeypatch.delenv("REDACTOR_KEY", raising=False)
CHECKING_CSV = FIXTURES_DIR / "checking_2026-04.csv"
CREDIT_OFX = FIXTURES_DIR / "credit_2026-04.ofx"


def _all_raw_identifiers() -> list[str]:
    manifest = load_manifest()
    seeds: list[str] = []
    for variants in manifest["payees"].values():
        seeds.extend(variants)
    for acct in manifest["accounts"].values():
        seeds.append(acct["account_id"])
        seeds.append(acct["institution"])
    return seeds


def test_ingest_reports_per_file_summary(tmp_path, capsys):
    db = tmp_path / "store.db"
    rc = main([
        "ingest", str(CHECKING_CSV), str(CREDIT_OFX),
        "--store", str(db), "--key", KEY,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # One summary line per file, naming the file and its counts.
    assert CHECKING_CSV.name in out
    assert CREDIT_OFX.name in out
    assert "rows" in out.lower()
    assert "payee" in out.lower()
    assert "alias" in out.lower()
    assert "lint" in out.lower()


def test_ingest_persists_raw_rows_to_store(tmp_path, capsys):
    db = tmp_path / "store.db"
    main(["ingest", str(CHECKING_CSV), "--store", str(db), "--key", KEY])
    with open_store(db, KEY, create=False) as store:
        rows = list(store.raw_transactions())
    assert len(rows) == 11


def test_ingest_summary_is_alias_space_only(tmp_path, capsys):
    db = tmp_path / "store.db"
    main([
        "ingest", str(CHECKING_CSV), str(CREDIT_OFX),
        "--store", str(db), "--key", KEY,
    ])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    for identifier in _all_raw_identifiers():
        assert identifier not in blob, f"raw identifier leaked into CLI output: {identifier!r}"


def test_reingest_reports_zero_new_aliases(tmp_path, capsys):
    db = tmp_path / "store.db"
    main(["ingest", str(CHECKING_CSV), "--store", str(db), "--key", KEY])
    capsys.readouterr()  # discard first run
    rc = main(["ingest", str(CHECKING_CSV), "--store", str(db), "--key", KEY])
    assert rc == 0
    out = capsys.readouterr().out
    # Second pass issues no new aliases (stability).
    assert "0" in out


def test_malformed_file_fails_value_free_and_batch_continues(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Date,Description,Amount\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,SEKRET-AMOUNT-9\n",
        encoding="utf-8",
    )
    db = tmp_path / "store.db"
    rc = main([
        "ingest", str(bad), str(CHECKING_CSV),
        "--store", str(db), "--key", KEY,
    ])
    # A file failed, so a non-zero exit; but the good file still ingested.
    assert rc != 0
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "SEKRET-AMOUNT-9" not in blob  # value-free by default
    # The good file's rows still landed.
    with open_store(db, KEY, create=False) as store:
        assert len(list(store.raw_transactions())) == 11


def test_show_raw_reveals_offending_value(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Date,Description,Amount\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,SEKRET-AMOUNT-9\n",
        encoding="utf-8",
    )
    db = tmp_path / "store.db"
    main([
        "ingest", str(bad),
        "--store", str(db), "--key", KEY, "--show-raw",
    ])
    captured = capsys.readouterr()
    assert "SEKRET-AMOUNT-9" in (captured.out + captured.err)


def test_ingest_requires_a_key(tmp_path, capsys, empty_keychain):
    # No --key, no $REDACTOR_KEY, nothing custodied -> actionable value-free exit.
    db = tmp_path / "store.db"
    rc = main(["ingest", str(CHECKING_CSV), "--store", str(db)])
    assert rc == 2
    assert "key" in capsys.readouterr().err.lower()


def _collapsing_csv(path, rows: int) -> None:
    """A statement whose rows all resolve to a single payee — the shape the real
    ingest collapse (issue #37) produced. Provably-fake merchant, invalid amounts."""
    lines = ["Date,Description,Amount,Balance"]
    for i in range(rows):
        lines.append(f"2026-04-{(i % 27) + 1:02d},STARBUCKS STORE {5000 + i} SEATTLE WA,-4.75,0.00")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ingest_warns_on_payee_collapse(tmp_path, capsys):
    """A statement with hundreds of rows collapsing to ~one payee must raise a
    loud payee-per-row sanity warning in the summary (issue #37) — the collapse
    has to be visible even before the review flow runs."""
    csv = tmp_path / "stmt.csv"  # neutral name: the warning, not the path, must say "collapse"
    _collapsing_csv(csv, rows=250)
    db = tmp_path / "store.db"
    rc = main(["ingest", str(csv), "--store", str(db), "--key", KEY])
    assert rc == 0
    captured = capsys.readouterr()
    blob = (captured.out + captured.err).lower()
    assert "collapse" in blob, "expected a loud payee collapse warning in the ingest summary"
    assert "ratio" in blob, "the warning must report the payee-per-row ratio"


def test_ingest_collapse_warning_is_alias_space_only(tmp_path, capsys):
    csv = tmp_path / "stmt.csv"
    _collapsing_csv(csv, rows=250)
    db = tmp_path / "store.db"
    main(["ingest", str(csv), "--store", str(db), "--key", KEY])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    # The warning is a ratio and counts — never a raw memo or display label.
    assert "STARBUCKS" not in blob.upper()


def test_healthy_ingest_does_not_warn_about_collapse(tmp_path, capsys):
    db = tmp_path / "store.db"
    main(["ingest", str(CHECKING_CSV), str(CREDIT_OFX), "--store", str(db), "--key", KEY])
    captured = capsys.readouterr()
    blob = (captured.out + captured.err).lower()
    assert "collapse" not in blob, "a healthy statement must not trip the collapse warning"


def test_ingest_map_flag_parses(tmp_path, capsys):
    opaque = tmp_path / "opaque.csv"
    opaque.write_text(
        "col_a,col_b,col_c\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,-1850.00\n",
        encoding="utf-8",
    )
    db = tmp_path / "store.db"
    rc = main([
        "ingest", str(opaque),
        "--store", str(db), "--key", KEY,
        "--map", "date=col_a,description=col_b,amount=col_c",
    ])
    assert rc == 0
    with open_store(db, KEY, create=False) as store:
        assert len(list(store.raw_transactions())) == 1
