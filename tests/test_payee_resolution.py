"""Acceptance guard for payee entity resolution (story S1.3).

The moat: memo-string garbage like "WHOLEFDS #1029 SEA" and "WF MKT 445" must
resolve to one payee entity across months. These tests pin the two acceptance
bars from issue #6 — **≥90% variant-grouping accuracy** on the multi-variant
fixture payees and **zero false merges** — plus the contract invariants that
back them (docs/alias-contract.md §2.4, §2.5): stability is forever, and
resolution never silently merges two distinct entities.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from redactor.fixtures import load_manifest, load_statements
from redactor.resolve import PayeeResolver, normalize


# --------------------------------------------------------------------------- #
# Ground truth from the fixture manifest.
# --------------------------------------------------------------------------- #
def _truth() -> tuple[dict[str, str], list[str]]:
    """Return (variant -> canonical payee) and the list of multi-variant payees."""
    payees = load_manifest()["payees"]
    variant_to_payee: dict[str, str] = {}
    multi: list[str] = []
    for name, variants in payees.items():
        if len(variants) >= 2:
            multi.append(name)
        for v in variants:
            variant_to_payee[v] = name
    return variant_to_payee, multi


def _resolve_variants(memos: list[str]) -> dict[str, int]:
    """Resolve every memo, returning variant -> entity id (order preserved)."""
    resolver = PayeeResolver()
    return {memo: resolver.resolve(memo) for memo in memos}


# --------------------------------------------------------------------------- #
# Acceptance bar 1: zero false merges (a false merge is a re-identification bug).
# --------------------------------------------------------------------------- #
def test_zero_false_merges_over_manifest_variants():
    truth, _ = _truth()
    payees = load_manifest()["payees"]
    variants = [v for vs in payees.values() for v in vs]

    assign = _resolve_variants(variants)

    by_entity: dict[int, set[str]] = defaultdict(set)
    for variant, eid in assign.items():
        by_entity[eid].add(truth[variant])

    false_merges = {eid: labels for eid, labels in by_entity.items() if len(labels) > 1}
    assert not false_merges, f"entities merged distinct payees: {false_merges}"


def test_zero_false_merges_under_real_ingest_order():
    """Resolution is online: statement order must not induce a false merge."""
    truth, _ = _truth()
    descriptions = [
        t.description for st in load_statements() for t in st.transactions
    ]

    resolver = PayeeResolver()
    assign = {d: resolver.resolve(d) for d in descriptions}

    by_entity: dict[int, set[str]] = defaultdict(set)
    for desc, eid in assign.items():
        if desc in truth:  # ignore any non-payee filler
            by_entity[eid].add(truth[desc])
    false_merges = {eid: labels for eid, labels in by_entity.items() if len(labels) > 1}
    assert not false_merges, f"ingest order induced a false merge: {false_merges}"


# --------------------------------------------------------------------------- #
# Acceptance bar 2: >=90% variant-grouping accuracy on the multi-variant payees.
# --------------------------------------------------------------------------- #
def test_variant_grouping_accuracy_manifest():
    truth, multi = _truth()
    payees = load_manifest()["payees"]
    assert len(multi) >= 10, "fixtures must supply >=10 multi-variant payees"

    variants = [v for vs in payees.values() for v in vs]
    assign = _resolve_variants(variants)

    total = correct = 0
    for name in multi:
        vs = payees[name]
        total += len(vs)
        # A variant is correctly grouped if it lands in the entity holding the
        # plurality of its true siblings; splinters count against accuracy.
        counts = Counter(assign[v] for v in vs)
        correct += max(counts.values())

    accuracy = correct / total
    assert accuracy >= 0.90, f"variant-grouping accuracy {accuracy:.3f} < 0.90"


def test_variant_grouping_accuracy_under_real_ingest_order():
    truth, multi = _truth()
    payees = load_manifest()["payees"]
    descriptions = [
        t.description for st in load_statements() for t in st.transactions
    ]
    resolver = PayeeResolver()
    assign = {d: resolver.resolve(d) for d in descriptions}

    total = correct = 0
    for name in multi:
        vs = payees[name]
        total += len(vs)
        counts = Counter(assign[v] for v in vs)
        correct += max(counts.values())
    accuracy = correct / total
    assert accuracy >= 0.90, f"accuracy {accuracy:.3f} < 0.90 under ingest order"


# --------------------------------------------------------------------------- #
# Contract invariants (docs/alias-contract.md §2.4, §2.5).
# --------------------------------------------------------------------------- #
def test_stability_is_forever():
    """Same entity -> same id forever; re-resolving issues no new entity."""
    resolver = PayeeResolver()
    first = resolver.resolve("WHOLEFDS #1029 SEA")
    # Re-present the same and a fresh variant of the same payee.
    assert resolver.resolve("WHOLEFDS #1029 SEA") == first
    again = resolver.resolve("WHOLE FOODS MKT #10029")
    assert again == first, "a known payee's variant must reuse its entity id"
    assert resolver.entity_count() == 1


def test_below_threshold_makes_new_entity_never_merges():
    """Two unrelated payees never collapse into one entity."""
    resolver = PayeeResolver()
    a = resolver.resolve("SHELL OIL 5712341")
    b = resolver.resolve("NETFLIX.COM")
    assert a != b
    assert resolver.entity_count() == 2


def test_ids_are_monotonic_and_positive():
    resolver = PayeeResolver()
    ids = [
        resolver.resolve("SHELL OIL 5712341"),
        resolver.resolve("NETFLIX.COM"),
        resolver.resolve("TARGET T-1123"),
    ]
    assert ids == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Deterministic normalization (strip store numbers, cities, dates).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "memo,expected",
    [
        ("WHOLEFDS #1029 SEA", ["WHOLEFDS"]),          # strip store # and city
        ("SHELL 12345678 KENT WA", ["SHELL"]),          # strip store #, city, state
        ("STARBUCKS STORE 05561 SEA", ["STARBUCKS"]),   # strip filler, store #, city
        ("NETFLIX 8663 LOS GATOS", ["NETFLIX"]),        # strip store #, multi-word city
        ("TARGET.COM *ORDER", ["TARGET"]),              # strip filler tokens
    ],
)
def test_normalize_strips_store_numbers_cities(memo, expected):
    assert normalize(memo) == expected


def test_normalize_strips_dates():
    # Numeric date fragments carry no payee signal and must be dropped.
    assert normalize("COSTCO WHSE 2026-04-12 04/12") == ["COSTCO"]
