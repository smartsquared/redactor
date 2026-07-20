"""The `redactor lens` CLI (story S2.2).

The lens ships as a CLI (issue #10). Its defining security property: it
**refuses to run without local table access** — un-redaction happens only on the
machine that holds the mapping table (brief §"Un-redaction mechanism").
"""
from __future__ import annotations

import pytest

from redactor.cli import main
from redactor.store import open_store

KEY = "correct horse battery staple"
REAL_PAYEE = "Bank of Nowhere Grocery"


def _seed_store(path):
    with open_store(path, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", REAL_PAYEE)


def test_lens_substitutes_text_from_the_local_table(tmp_path, capsys):
    db = tmp_path / "store.db"
    _seed_store(db)

    rc = main(["lens", "--store", str(db), "--key", KEY, "--text", "your payee-7's bill"])

    assert rc == 0
    out = capsys.readouterr().out
    assert REAL_PAYEE in out
    assert "PAYEE" not in out.upper()


def test_lens_reads_stdin_when_no_text_flag(tmp_path, capsys, monkeypatch):
    db = tmp_path / "store.db"
    _seed_store(db)

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("Payee 7 and PAYEE-7's charge"))
    rc = main(["lens", "--store", str(db), "--key", KEY])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.count(REAL_PAYEE) == 2


def test_lens_refuses_when_store_is_missing(tmp_path, capsys):
    # No table on this machine → the lens must refuse, not silently pass text
    # through (that would be un-redaction attempted without the crown jewel).
    missing = tmp_path / "nope.db"

    rc = main(["lens", "--store", str(missing), "--key", KEY, "--text", "payee-7"])

    assert rc != 0
    captured = capsys.readouterr()
    assert "payee-7" not in captured.out  # nothing rendered
    assert missing.name in captured.err or "store" in captured.err.lower()


def test_lens_refuses_on_wrong_key(tmp_path, capsys):
    db = tmp_path / "store.db"
    _seed_store(db)

    rc = main(["lens", "--store", str(db), "--key", "wrong key", "--text", "payee-7"])

    assert rc != 0
    assert REAL_PAYEE not in capsys.readouterr().out


def test_lens_requires_a_store_argument():
    # Without --store there is no table access at all → argparse rejects it.
    with pytest.raises(SystemExit) as exc:
        main(["lens", "--key", KEY, "--text", "payee-7"])
    assert exc.value.code != 0
