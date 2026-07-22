"""The `redactor lint` CLI (story S0.2, issue #29).

Standalone detection-based leak-lint over a file. Unlike `redactor lens`, it
needs no key and no store — it scans text and structure only, so it is safe to
run on either side of the crown-jewel boundary. Exit 0 = clean, 1 = findings.
"""
from __future__ import annotations

from redactor.cli import main
from redactor.fixtures import load_statements
from redactor.ingest import ingest_statements
from redactor.store import open_store

KEY = "correct horse battery staple"
VALID_ABA = "021000021"


def test_lint_cli_clean_on_fixture_projection(tmp_path, capsys):
    statements = load_statements()
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)
    out = tmp_path / "projection.json"
    projection.write_json(out)

    rc = main(["lint", str(out)])

    assert rc == 0
    assert "clean" in capsys.readouterr().out.lower()


def test_lint_cli_bites_on_planted_identifier(tmp_path, capsys):
    artifact = tmp_path / "leaky.json"
    artifact.write_text(
        f'{{"records": [{{"payee": "PAYEE-1", "note": "wire {VALID_ABA}"}}]}}',
        encoding="utf-8",
    )

    rc = main(["lint", str(artifact)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert VALID_ABA in err


def test_lint_cli_bites_on_raw_name_in_payee_field(tmp_path, capsys):
    artifact = tmp_path / "leaky.json"
    artifact.write_text(
        '{"records": [{"account": "ACCT-1", "payee": "Whole Foods Market"}]}',
        encoding="utf-8",
    )

    rc = main(["lint", str(artifact)])

    assert rc == 1
    assert "Whole Foods Market" in capsys.readouterr().err


def test_lint_cli_missing_file_errors_cleanly(tmp_path, capsys):
    rc = main(["lint", str(tmp_path / "does-not-exist.json")])
    assert rc == 2
    assert "cannot read" in capsys.readouterr().err
