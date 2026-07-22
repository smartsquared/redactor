"""The ``redactor export`` command (issue #41).

``redactor export`` is the missing CLI path that produces a **lint-attested
alias-space projection** — the file ``sakuma-finance import --projection`` refuses
to consume unless it carries redactor's green lint attestation. It builds a
month's projection from the store, runs the S0.2 detection lint over it, and:

  * **green** → writes the projection file with a ``provenance`` attestation block
    (verdict / timestamp / linter version), so the m02 import seam can trust it;
  * **red** → writes *nothing*, prints findings to stderr (local render), exits
    non-zero.

Everything the summary prints stays alias-space (month, row count, verdict); the
written file is alias-space only (no raw memo, account id, or institution name).
"""
from __future__ import annotations

import json

from redactor.cli import main
from redactor.export import LINTER_VERSION
from redactor.fixtures import FIXTURES_DIR, load_manifest
from redactor.store import open_store

KEY = "correct horse battery staple"

CHECKING_MAY_CSV = FIXTURES_DIR / "checking_2026-05.csv"
CREDIT_MAY_OFX = FIXTURES_DIR / "credit_2026-05.ofx"


def _all_raw_identifiers() -> list[str]:
    manifest = load_manifest()
    seeds: list[str] = []
    for variants in manifest["payees"].values():
        seeds.extend(variants)
    for acct in manifest["accounts"].values():
        seeds.append(acct["account_id"])
        seeds.append(acct["institution"])
    return seeds


def _ingest_may(db) -> None:
    rc = main([
        "ingest", str(CHECKING_MAY_CSV), str(CREDIT_MAY_OFX),
        "--store", str(db), "--key", KEY,
    ])
    assert rc == 0


def test_export_writes_attested_projection_for_month(tmp_path, capsys):
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()  # discard ingest output

    out = tmp_path / "proj-2026-05.json"
    rc = main([
        "export", "--store", str(db), "--key", KEY,
        "--month", "2026-05", "--out", str(out),
    ])
    assert rc == 0
    assert out.exists(), "a green export must write the projection file"

    envelope = json.loads(out.read_text(encoding="utf-8"))
    assert envelope["records"], "the month's records must be present"
    assert "provenance" in envelope, "the file must carry a provenance attestation block"

    prov = envelope["provenance"]
    assert prov["verdict"] == "green"
    assert prov["linter_version"] == LINTER_VERSION
    assert isinstance(prov.get("timestamp"), str) and prov["timestamp"]
    assert isinstance(prov.get("linter"), str) and prov["linter"]


def test_export_provenance_shape_is_the_cross_repo_contract(tmp_path, capsys):
    """Lock the provenance block shape so the m02 seam can't drift.

    Must match sakuma_finance.records_import.read_attestation: verdict, timestamp,
    linter version (see docs/sanitized-context-api.md §Export provenance)."""
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()

    out = tmp_path / "proj.json"
    main(["export", "--store", str(db), "--key", KEY, "--month", "2026-05", "--out", str(out)])
    prov = json.loads(out.read_text(encoding="utf-8"))["provenance"]
    assert set(prov) == {"verdict", "timestamp", "linter", "linter_version"}


def test_export_file_is_alias_space_only(tmp_path, capsys):
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()

    out = tmp_path / "proj.json"
    main(["export", "--store", str(db), "--key", KEY, "--month", "2026-05", "--out", str(out)])
    blob = out.read_text(encoding="utf-8")
    for identifier in _all_raw_identifiers():
        assert identifier not in blob, f"raw identifier leaked into projection: {identifier!r}"


def test_export_summary_is_alias_space_only(tmp_path, capsys):
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()

    out = tmp_path / "proj.json"
    main(["export", "--store", str(db), "--key", KEY, "--month", "2026-05", "--out", str(out)])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "2026-05" in blob  # month is alias-space
    for identifier in _all_raw_identifiers():
        assert identifier not in blob, f"raw identifier leaked into summary: {identifier!r}"


def test_exported_file_passes_redactor_lint(tmp_path, capsys):
    """The end-to-end contract: what export writes, `redactor lint` calls clean."""
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()

    out = tmp_path / "proj.json"
    main(["export", "--store", str(db), "--key", KEY, "--month", "2026-05", "--out", str(out)])
    capsys.readouterr()

    rc = main(["lint", str(out)])
    assert rc == 0, "an exported projection must be lint-clean"


def test_export_red_store_writes_no_file_and_exits_nonzero(tmp_path, capsys):
    """A store whose month projects with an un-aliased identifying field lints red;
    export must write nothing and exit non-zero (the lint is the gate)."""
    db = tmp_path / "store.db"
    _ingest_may(db)
    capsys.readouterr()

    # Inject a raw transaction whose account was never aliased: its account field
    # cannot resolve to an ACCT token, so the projection carries a raw value and
    # the detection lint bites.
    with open_store(db, KEY, create=False) as store:
        store.add_raw_transaction(
            account_id="UNALIASED-ACCT-000777",
            posted_date="2026-05-15",
            amount_cents=-1234,
            raw_payee="SOME UNRECORDED MEMO",
            institution="Ghost Bank of Nowhere",
        )

    out = tmp_path / "proj.json"
    rc = main([
        "export", "--store", str(db), "--key", KEY,
        "--month", "2026-05", "--out", str(out),
    ])
    assert rc != 0, "a red projection must exit non-zero"
    assert not out.exists(), "a red projection must not be written"


def test_export_all_months_writes_one_file_per_month(tmp_path, capsys):
    db = tmp_path / "store.db"
    # Ingest two different months so --all-months has more than one to write.
    main([
        "ingest",
        str(FIXTURES_DIR / "checking_2026-04.csv"),
        str(FIXTURES_DIR / "checking_2026-05.csv"),
        "--store", str(db), "--key", KEY,
    ])
    capsys.readouterr()

    outdir = tmp_path / "projections"
    rc = main([
        "export", "--store", str(db), "--key", KEY,
        "--all-months", "--out", str(outdir),
    ])
    assert rc == 0
    written = sorted(p.name for p in outdir.glob("*.json"))
    assert any("2026-04" in n for n in written)
    assert any("2026-05" in n for n in written)


def test_export_refuses_missing_store(tmp_path, capsys):
    out = tmp_path / "proj.json"
    rc = main([
        "export", "--store", str(tmp_path / "nope.db"), "--key", KEY,
        "--month", "2026-05", "--out", str(out),
    ])
    assert rc != 0
    assert not out.exists()
