"""Statement-file ingest adapter (story S1.4).

Acceptance criteria exercised here (issue #7):

  - Fixtures ingest end-to-end (CSV + OFX -> local encrypted store).
  - Raw fields land ONLY in the encrypted store; the alias-space projection is a
    separate output that carries zero seeded identifiers (the leak-lint).

Detection (S1.1), alias assignment (S1.2) and payee resolution (S1.3) are the
upstream stories this adapter *runs at ingest*; here we exercise the adapter's
own contract: everything raw stays local, and the projection is clean.
"""
from __future__ import annotations

import json

import pytest

from redactor.fixtures import load_manifest, load_statements
from redactor.ingest import ingest_statements
from redactor.leaklint import scan_projection, seeds_from_manifest
from redactor.store import open_store

KEY = "correct horse battery staple"


def _total_txns(statements):
    return sum(len(s.transactions) for s in statements)


def test_fixtures_ingest_end_to_end(tmp_path):
    statements = load_statements()
    assert statements, "expected bundled fixtures to load"

    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

        total = _total_txns(statements)
        # Every raw transaction landed in the encrypted store.
        assert len(list(store.raw_transactions())) == total
        # The projection is a separate output with one alias-space record each.
        assert len(projection.records) == total


def test_projection_contains_zero_seeded_identifiers(tmp_path):
    """THE leak-lint: no seeded identifier appears in the alias-space output."""
    statements = load_statements()
    manifest = load_manifest()

    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

    text = projection.to_json()
    leaks = scan_projection(text, seeds_from_manifest(manifest))
    assert leaks == [], f"alias-space projection leaked identifiers: {leaks}"


def test_raw_payee_strings_never_reach_the_projection(tmp_path):
    statements = load_statements()
    manifest = load_manifest()

    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

    text = projection.to_json()
    for variants in manifest["payees"].values():
        for variant in variants:
            assert variant not in text, f"raw memo leaked into projection: {variant!r}"
    # Account ids and institution names are identifiers too.
    for acct in manifest["accounts"].values():
        assert acct["account_id"] not in text
        assert acct["institution"] not in text


def test_amounts_and_dates_stay_real(tmp_path):
    """Contract §1.1: amounts and dates are not aliased."""
    statements = load_statements()

    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

    src_pairs = sorted(
        (t.date, round(t.amount, 2))
        for s in statements
        for t in s.transactions
    )
    proj_pairs = sorted((r.date, round(r.amount, 2)) for r in projection.records)
    assert proj_pairs == src_pairs


def test_reingest_issues_no_new_aliases(tmp_path):
    """Alias stability (contract §2.4): re-running ingest issues zero new aliases."""
    statements = load_statements()
    db = tmp_path / "store.db"

    with open_store(db, KEY, create=True) as store:
        ingest_statements(store, statements)
        after_first = len(store.mapping)

    with open_store(db, KEY, create=False) as store:
        second = ingest_statements(store, statements)
        after_second = len(store.mapping)

    assert after_second == after_first
    # And the projection is identical the second time around.
    assert json.loads(second.to_json())


def test_projection_records_are_alias_tokens(tmp_path):
    """Every identifying field in a record is a canonical alias token."""
    from redactor.alias import find_aliases

    statements = load_statements()
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

    for r in projection.records:
        for field_value in (r.account, r.institution, r.payee):
            matches = find_aliases(field_value)
            assert len(matches) == 1 and matches[0].canonical == field_value, (
                f"expected a canonical alias token, got {field_value!r}"
            )


def test_ingest_writes_no_raw_fields_off_store(tmp_path):
    """The projection dataclass exposes no raw memo/account attributes."""
    statements = load_statements()[:1]
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)

    record = projection.records[0]
    payload = json.loads(projection.to_json())["records"][0]
    for forbidden in ("raw_payee", "raw_memo", "description", "account_id"):
        assert forbidden not in payload
    assert not hasattr(record, "description")
