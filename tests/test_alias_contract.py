"""Executable guard for the alias-space matching rule (docs/alias-contract.md §3).

The contract's inbound matcher must be mangle-tolerant but never
false-positive; these tests pin both halves so a later change to the grammar
can't silently loosen it.
"""
from __future__ import annotations

import pytest

from redactor.alias import canonical, find_aliases


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PAYEE-7", ("PAYEE", 7)),
        ("payee-7", ("PAYEE", 7)),
        ("Payee-7", ("PAYEE", 7)),
        ("PAYEE_7", ("PAYEE", 7)),
        ("PAYEE 7", ("PAYEE", 7)),
        ("PAYEE-7's", ("PAYEE", 7)),
        ("payee-7s", ("PAYEE", 7)),
        ("ACCT-1", ("ACCT", 1)),
        ("inst-2", ("INST", 2)),
        ("PERSON-40", ("PERSON", 40)),
        ("CARD-1", ("CARD", 1)),
    ],
)
def test_recognised_manglings(text, expected):
    hits = find_aliases(text)
    assert len(hits) == 1
    assert (hits[0].cls, hits[0].n) == expected


@pytest.mark.parametrize(
    "text",
    [
        "the payee was happy",          # class word, no token
        "repayment-7 schedule",         # class glued into a longer word
        "XPAYEE-7",                     # letters before the class
        "payees-7",                     # trailing letters on the class
        "FOO-7",                        # class not in the closed set
        "PAYEE-",                       # no number
        "7",                            # bare number
        "invoice #7 total",             # number without a class
    ],
)
def test_never_false_positive(text):
    assert find_aliases(text) == []


def test_whole_number_boundary():
    hits = find_aliases("PAYEE-70")
    assert len(hits) == 1
    assert (hits[0].cls, hits[0].n) == ("PAYEE", 70)  # not (PAYEE, 7)


def test_multiple_tokens_in_prose():
    text = "Move PAYEE-7's charge off ACCT-1 and onto card-2."
    got = {(h.cls, h.n) for h in find_aliases(text)}
    assert got == {("PAYEE", 7), ("ACCT", 1), ("CARD", 2)}


def test_canonical_form():
    assert canonical("payee", 7) == "PAYEE-7"
    assert canonical("ACCT", 1) == "ACCT-1"
    with pytest.raises(ValueError):
        canonical("FOO", 1)          # class outside the closed set
    with pytest.raises(ValueError):
        canonical("PAYEE", 0)        # n must be positive
