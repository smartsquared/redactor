"""Tests for stable alias assignment against the mapping table (story S1.2).

Acceptance criteria exercised here:
  - re-running ingest over the same fixtures issues zero new aliases
  - a property test for collision-freedom

S1.2 is *issuance*, not resolution: it binds an already-resolved entity
``(entity_type, real_value)`` to a stable alias token. Collapsing memo-string
variants ("WHOLEFDS #1029 SEA" vs "WF MKT 445") to one entity is the S1.3 moat
and is deliberately out of scope here — this layer only guarantees that the same
entity key always yields the same token and a new key gets the next one.
"""
import os
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from redactor.alias import CLASSES, find_aliases
from redactor.fixtures import load_statements
from redactor.store import open_store

KEY = "correct horse battery staple"


def _ingest(store) -> None:
    """Assign aliases for every entity in the bundled fixtures.

    Institutions -> INST, account ids -> ACCT, raw memo strings -> PAYEE. No
    resolution: each distinct raw string is its own entity (S1.3's job to merge).
    """
    for stmt in load_statements():
        store.mapping.assign("INST", stmt.institution)
        store.mapping.assign("ACCT", stmt.account_id)
        for txn in stmt.transactions:
            if txn.description:
                store.mapping.assign("PAYEE", txn.description)


# ---- acceptance 1: re-ingest issues zero new aliases ------------------------

def test_reingest_over_same_fixtures_issues_zero_new_aliases(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        _ingest(store)
        after_first = len(store.mapping)
        assert after_first > 0  # the fixtures actually produced aliases

        _ingest(store)  # same fixtures, same process
        assert len(store.mapping) == after_first


def test_reingest_across_reopen_issues_zero_new_aliases(tmp_path):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        _ingest(store)
        after_first = len(store.mapping)

    # A fresh session (counter must persist in the store, not in memory).
    with open_store(db, KEY, create=False) as store:
        _ingest(store)
        assert len(store.mapping) == after_first


# ---- stable issuance semantics ----------------------------------------------

def test_same_entity_same_alias_forever(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        first = store.mapping.assign("PAYEE", "WHOLEFDS #1029 SEA")
        again = store.mapping.assign("PAYEE", "WHOLEFDS #1029 SEA")
        assert first == again == "PAYEE-1"


def test_new_entity_gets_next_token(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        assert store.mapping.assign("PAYEE", "one") == "PAYEE-1"
        assert store.mapping.assign("PAYEE", "two") == "PAYEE-2"
        assert store.mapping.assign("PAYEE", "three") == "PAYEE-3"
        # revisiting an old entity does not consume a number
        assert store.mapping.assign("PAYEE", "one") == "PAYEE-1"
        assert store.mapping.assign("PAYEE", "four") == "PAYEE-4"


def test_numbering_is_namespaced_per_class(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        assert store.mapping.assign("PAYEE", "x") == "PAYEE-1"
        assert store.mapping.assign("ACCT", "x") == "ACCT-1"
        assert store.mapping.assign("INST", "x") == "INST-1"
        assert store.mapping.assign("PAYEE", "y") == "PAYEE-2"
        assert store.mapping.assign("ACCT", "y") == "ACCT-2"


def test_entity_type_is_case_insensitive_on_the_class(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        canonical = store.mapping.assign("PAYEE", "same")
        assert store.mapping.assign("payee", "same") == canonical
        assert store.mapping.assign("Payee", "same") == canonical


def test_assign_rejects_unknown_class(tmp_path):
    import pytest
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        with pytest.raises(ValueError):
            store.mapping.assign("FOO", "bar")


def test_issued_tokens_round_trip_through_the_mapping(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        token = store.mapping.assign("PAYEE", "WHOLEFDS #1029 SEA")
        # forward: real -> token; reverse: token -> sealed real
        assert store.mapping.resolve_token("PAYEE", "WHOLEFDS #1029 SEA") == token
        assert store.mapping.resolve_alias(token).reveal() == "WHOLEFDS #1029 SEA"


def test_numbers_are_not_reused_after_a_row_is_deleted(tmp_path):
    # Contract §2.3: numbers are never reused, even if an entity is deleted, so
    # a stale redacted record can never be silently re-bound. The issuance
    # counter is durable and independent of the current row set.
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        store.mapping.assign("PAYEE", "a")  # PAYEE-1
        store.mapping.assign("PAYEE", "b")  # PAYEE-2
        store.mapping.assign("PAYEE", "c")  # PAYEE-3
        # Simulate a future deletion of PAYEE-2 at the storage layer.
        store._conn.execute("DELETE FROM alias_mapping WHERE alias_token = 'PAYEE-2'")
        store._conn.commit()
        # The freed number 2 must NOT be handed out again.
        assert store.mapping.assign("PAYEE", "d") == "PAYEE-4"


# ---- acceptance 2: property test for collision-freedom ----------------------

_entities = st.tuples(
    st.sampled_from(CLASSES),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=12),
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_entities, max_size=40))
def test_assignment_is_collision_free_and_bijective(ops):
    with tempfile.TemporaryDirectory() as d:
        with open_store(os.path.join(d, "s.db"), KEY, create=True) as store:
            seen: dict[tuple[str, str], str] = {}
            for cls, val in ops:
                token = store.mapping.assign(cls, val)

                # stability: an entity always resolves to the same token
                if (cls, val) in seen:
                    assert token == seen[(cls, val)]
                seen[(cls, val)] = token

                # well-formed: exactly one alias of the right class
                matches = find_aliases(token)
                assert len(matches) == 1
                assert matches[0].cls == cls.upper()

            # collision-freedom / bijection: distinct entities -> distinct
            # tokens, and no token is shared between two entities.
            assert len(set(seen.values())) == len(seen)
