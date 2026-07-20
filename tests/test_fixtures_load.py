"""S0.2 accept: the synthetic bookkeeping fixtures load.

Derived from the story's acceptance criterion "fixtures load" plus the fixture
spec: >=3 months of checking + credit-card statements in CSV *and* OFX, with
>=10 multi-variant payees.
"""
from __future__ import annotations

import json

from redactor.fixtures import FIXTURES_DIR, load_manifest, load_statements


def test_statements_load_from_both_formats():
    statements = load_statements()
    assert statements, "no statements loaded"
    formats = {s.format for s in statements}
    assert formats == {"csv", "ofx"}, f"expected CSV and OFX, got {formats}"


def test_at_least_three_months_for_each_account_type():
    statements = load_statements()
    for acct in ("checking", "credit"):
        for fmt in ("csv", "ofx"):
            months = {s.month for s in statements if s.account_type == acct and s.format == fmt}
            assert len(months) >= 3, f"{acct}/{fmt} has only {len(months)} months: {months}"


def test_every_statement_has_parsed_transactions():
    for s in load_statements():
        assert s.transactions, f"{s.source.name} parsed to zero transactions"
        assert s.institution, f"{s.source.name} has no institution"
        assert s.account_id, f"{s.source.name} has no account id"
        for t in s.transactions:
            assert t.date and len(t.date) == 10 and t.date[4] == "-", t.date
            assert isinstance(t.amount, float)
            assert t.description.strip()


def test_csv_and_ofx_agree_on_transaction_count_per_statement():
    # Same month + account should carry the same number of transactions
    # whether it was read from CSV or OFX (they render the same ledger).
    by_key: dict[tuple[str, str], dict[str, int]] = {}
    for s in load_statements():
        by_key.setdefault((s.account_type, s.month), {})[s.format] = len(s.transactions)
    for key, counts in by_key.items():
        assert counts.get("csv") == counts.get("ofx"), f"{key}: {counts}"


def test_manifest_declares_at_least_ten_multi_variant_payees():
    manifest = load_manifest()
    multi = {name: v for name, v in manifest["payees"].items() if len(v) > 1}
    assert len(multi) >= 10, f"only {len(multi)} multi-variant payees"
    assert manifest["multi_variant_payee_count"] == len(multi)


def test_manifest_variants_actually_appear_in_the_statements():
    manifest = load_manifest()
    seen = {t.description for s in load_statements() for t in s.transactions}
    missing = []
    for variants in manifest["payees"].values():
        for v in variants:
            if v not in seen:
                missing.append(v)
    assert not missing, f"manifest variants absent from fixtures: {missing}"


def test_manifest_file_matches_loader():
    with open(FIXTURES_DIR / "manifest.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw == load_manifest()
