"""Entity-resolution review flow — the payee registry (story S1.1, issue #31).

These tests pin the S1.1 acceptance bars:

  - ``merge`` / ``split`` / ``rename`` round-trip on the fixtures, with
    **alias-stability** property tests (a merge aliases the loser to the winner
    and never renumbers; every token ever issued keeps resolving).
  - low-confidence groupings are flagged (the signal ingest surfaces for review).
  - every mutation is journaled in the store (auditable).

The registry is the persistent, store-backed layer over the in-memory
``PayeeResolver`` (docs/m02-stories.md S1.1). Real values (display labels, memo
variants) live only in the encrypted store; ``review()`` is the local render
boundary that is allowed to reveal them.
"""
from __future__ import annotations

from collections import Counter

import pytest

from redactor.fixtures import load_statements
from redactor.payees import REVIEW_THRESHOLD, PayeeRegistry
from redactor.store import open_store

KEY = "correct horse battery staple"


def _fixture_memos() -> list[str]:
    return [t.description for st in load_statements() for t in st.transactions]


def _ingest(store) -> PayeeRegistry:
    reg = PayeeRegistry(store)
    for memo in _fixture_memos():
        reg.record(memo)
    return reg


# --------------------------------------------------------------------------- #
# record(): resolution + stable issuance + persistence.
# --------------------------------------------------------------------------- #
def test_record_is_stable_for_the_same_memo(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        a = reg.record("WHOLEFDS #1029 SEA")
        b = reg.record("WHOLEFDS #1029 SEA")
        assert a == b


def test_record_groups_variants_onto_one_token(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        t1 = reg.record("WHOLEFDS #1029 SEA")
        t2 = reg.record("WHOLE FOODS MKT #10029")
        assert t1 == t2, "variants of one payee must share a PAYEE token"


def test_record_survives_a_reopen_with_no_new_aliases(tmp_path):
    db = tmp_path / "s.db"
    with open_store(db, KEY, create=True) as store:
        _ingest(store)
        before = set(store.mapping.tokens())
    # A fresh session (new registry, reseeded from the store) must reuse tokens.
    with open_store(db, KEY, create=False) as store:
        reg = PayeeRegistry(store)
        for memo in _fixture_memos():
            reg.record(memo)
        after = set(store.mapping.tokens())
    assert after == before, "re-ingest must issue zero new aliases (stability)"


# --------------------------------------------------------------------------- #
# review(): variant counts, confidence, display labels (local render only).
# --------------------------------------------------------------------------- #
def test_review_lists_entities_with_counts_confidence_and_labels(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = _ingest(store)
        entities = reg.review()

    assert entities, "review must list the resolved payee entities"
    for e in entities:
        assert e.token.startswith("PAYEE-")
        assert e.variant_count >= 1
        assert 0.0 <= e.confidence <= 1.0
        assert e.display and any(c.isalpha() for c in e.display)
    # A multi-variant payee shows a variant_count above 1 for at least one entity.
    assert any(e.variant_count > 1 for e in entities)


def test_review_flags_low_confidence_groupings(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = _ingest(store)
        flagged = reg.flagged()
        review = reg.review()
    # Whole Foods ("WF" ~ "WHOLE FOODS") groups by fuzzy match, not a shared
    # token, so at least one grouping is below the review threshold.
    assert flagged, "expected at least one low-confidence grouping on the fixtures"
    low = [e for e in review if e.low_confidence]
    assert low, "review must surface the low-confidence entities"
    assert all(e.confidence < REVIEW_THRESHOLD for e in low)


# --------------------------------------------------------------------------- #
# merge / split / rename round-trip + alias stability.
# --------------------------------------------------------------------------- #
def test_merge_aliases_loser_to_winner_without_renumbering(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record("SHELL OIL 5712341")
        loser = reg.record("NETFLIX.COM")  # a distinct entity, on purpose
        assert winner != loser

        tokens_before = set(store.mapping.tokens())
        reg.merge(winner, loser)
        tokens_after = set(store.mapping.tokens())

        # Never renumber: no token is deleted or minted by a merge.
        assert tokens_after == tokens_before
        # The loser token still resolves — now to the winner's real value.
        assert (
            store.mapping.resolve_alias(loser).reveal()
            == store.mapping.resolve_alias(winner).reveal()
        )
        # The loser is folded away: review lists the winner, not the loser.
        review_tokens = {e.token for e in reg.review()}
        assert winner in review_tokens
        assert loser not in review_tokens


def test_split_is_the_inverse_of_merge(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")

        before_winner_real = store.mapping.resolve_alias(loser).reveal()
        reg.merge(winner, loser)
        reg.split(loser)  # un-merge

        # The loser is its own entity again, resolving to its own real value.
        assert store.mapping.resolve_alias(loser).reveal() == before_winner_real
        review_tokens = {e.token for e in reg.review()}
        assert winner in review_tokens and loser in review_tokens


def test_rename_changes_the_display_label_only(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record("WHOLEFDS #1029 SEA")
        reg.rename(token, "Whole Foods Market")

        assert store.mapping.resolve_alias(token).reveal() == "Whole Foods Market"
        # Same token — a rename never renumbers.
        assert token in set(store.mapping.tokens())
        entity = next(e for e in reg.review() if e.token == token)
        assert entity.display == "Whole Foods Market"


def test_rename_to_an_existing_label_is_refused(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        a = reg.record("WHOLEFDS #1029 SEA")
        b = reg.record("SHELL OIL 5712341")
        target = store.mapping.resolve_alias(a).reveal()
        with pytest.raises(ValueError):
            reg.rename(b, target)  # would collide with A -> that is a merge


def test_split_variant_pulls_one_variant_into_a_fresh_entity(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        # Force two distinct memos onto one token via an explicit merge, then
        # split one variant back out into its own fresh entity.
        keep = reg.record("WHOLEFDS #1029 SEA")
        other = reg.record("SHELL OIL 5712341")
        reg.merge(keep, other)

        tokens_before = set(store.mapping.tokens())
        new_token = reg.split_variant(keep, "SHELL OIL 5712341")
        assert new_token not in tokens_before, "split must mint a brand-new token"
        # The pulled variant now resolves under the new token.
        assert reg.record("SHELL OIL 5712341") == new_token


# --------------------------------------------------------------------------- #
# Alias-stability property: any mutation sequence keeps every issued token
# resolvable, and never reuses a number.
# --------------------------------------------------------------------------- #
def test_alias_stability_property_over_mutations(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = _ingest(store)
        issued = list(store.mapping.tokens())

        review = reg.review()
        assert len(review) >= 2
        winner, loser = review[0].token, review[1].token
        reg.merge(winner, loser)
        reg.split(loser)
        reg.rename(winner, "Renamed Merchant Zzz")

        # Every token ever issued still resolves to *some* real value.
        for token in issued:
            assert store.mapping.resolve_alias(token) is not None
        # No number was reused: the issued token set only grows.
        assert set(issued).issubset(set(store.mapping.tokens()))
        # Numbers are unique within the PAYEE class (never renumbered).
        payee_ns = [
            t for t in store.mapping.tokens() if t.startswith("PAYEE-")
        ]
        assert len(payee_ns) == len(set(payee_ns))


# --------------------------------------------------------------------------- #
# Journaling: every mutation is auditable in the store.
# --------------------------------------------------------------------------- #
def test_mutations_are_journaled(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        w = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")
        reg.merge(w, loser)
        reg.split(loser)
        reg.rename(w, "Whole Foods Co")

        ops = Counter(e["op"] for e in reg.journal())
    assert ops["merge"] == 1
    assert ops["split"] == 1
    assert ops["rename"] == 1


def test_journal_persists_across_reopen(tmp_path):
    db = tmp_path / "s.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        w = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")
        reg.merge(w, loser)
    with open_store(db, KEY, create=False) as store:
        reg = PayeeRegistry(store)
        assert any(e["op"] == "merge" for e in reg.journal())
