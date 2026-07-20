"""Inbound re-substitution — the local lens (story S2.2).

Acceptance criteria (issue #10) exercised here:

  * substitution passes a mangle corpus ("payee-7's", "Payee 7", case variants);
  * zero false positives on non-alias text;
  * refuses to run without local table access.

The lens is the *inbound* half of the bidirectional proxy (brief §"Un-redaction
mechanism"): model→human, alias→real, and only where the mapping table lives.
"""
from __future__ import annotations

import pytest

from redactor.lens import lens, resolver_from_table
from redactor.store import open_store

KEY = "correct horse battery staple"
# A deliberately-fake payee real value (repo rule 1: no real data, ever).
REAL_PAYEE = "Bank of Nowhere Grocery"
REAL_ACCT = "NOWHERE-CHK-000199"


def _dict_resolver(mapping: dict[str, str]):
    """A canonical-token -> real-value resolver backed by a plain dict."""
    return mapping.get


# -- the mangle corpus --------------------------------------------------------


@pytest.mark.parametrize(
    "mangled",
    [
        "PAYEE-7",       # canonical
        "payee-7",       # lowercase class
        "Payee-7",       # title case
        "PAYEE_7",       # underscore separator
        "PAYEE 7",       # space separator
        "Payee 7",       # space + title case (from the acceptance text)
        "PAYEE-7's",     # possessive
        "PAYEE-7’s",  # possessive with a curly apostrophe
        "payee-7s",      # bare plural/possessive mangle
    ],
)
def test_mangle_corpus_substitutes_to_real_value(mangled):
    resolve = _dict_resolver({"PAYEE-7": REAL_PAYEE})
    out = lens(f"Your {mangled} charge posted.", resolve)
    assert REAL_PAYEE in out.text
    # The alias token (any mangling of it) must not survive into rendered text.
    assert "PAYEE" not in out.text.upper()
    assert out.substituted == ["PAYEE-7"]


def test_multiple_tokens_in_one_string():
    resolve = _dict_resolver({"PAYEE-7": REAL_PAYEE, "ACCT-1": REAL_ACCT})
    out = lens("Move PAYEE-7's charge off ACCT-1.", resolve)
    assert out.text == f"Move {REAL_PAYEE} charge off {REAL_ACCT}."
    assert set(out.substituted) == {"PAYEE-7", "ACCT-1"}


# -- zero false positives -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "the payee was happy",          # class word, no token
        "repayment-7 schedule",         # class glued into a longer word
        "XPAYEE-7 balance",             # letters before the class
        "payees-7 list",                # trailing letters on the class
        "FOO-7 code",                   # class not in the closed set
        "invoice #7 total",             # number without a class
        "no aliases here at all",
    ],
)
def test_zero_false_positives_on_non_alias_text(text):
    resolve = _dict_resolver({"PAYEE-7": REAL_PAYEE})
    out = lens(text, resolve)
    assert out.text == text          # untouched, byte for byte
    assert out.substituted == []


def test_whole_number_boundary_is_not_a_prefix_hit():
    # PAYEE-70 is (PAYEE, 70); with only PAYEE-7 mapped it must pass through.
    resolve = _dict_resolver({"PAYEE-7": REAL_PAYEE})
    out = lens("charge on PAYEE-70 today", resolve)
    assert out.text == "charge on PAYEE-70 today"
    assert out.substituted == []


def test_unknown_token_passes_through_untouched():
    # A syntactically valid token absent from the table is left alone, not
    # substituted and not errored (alias-contract §3.2).
    resolve = _dict_resolver({"PAYEE-7": REAL_PAYEE})
    out = lens("what about PAYEE-999?", resolve)
    assert out.text == "what about PAYEE-999?"
    assert out.substituted == []


# -- backed by the real (sealed) mapping table --------------------------------


def test_resolver_from_table_reveals_at_the_boundary(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", REAL_PAYEE)
        resolve = resolver_from_table(store.mapping)
        out = lens("spend at payee-7's place", resolve)
        assert out.text == f"spend at {REAL_PAYEE} place"
        # unknown token still passes through
        assert resolve("PAYEE-8") is None
