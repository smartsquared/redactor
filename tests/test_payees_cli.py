"""The ``redactor payees`` review-flow CLI (story S1.1, issue #31).

``redactor payees --review`` is a **local render** surface: like ``lens`` it
reveals real values, and only on the machine that holds the mapping table.
``--merge`` / ``--split`` / ``--rename`` mutate groupings and labels and print an
**alias-space** confirmation (no raw memo or display value). And a plain
``redactor ingest`` flags low-confidence groupings in its summary, alias-space.
"""
from __future__ import annotations

from redactor.cli import main
from redactor.fixtures import FIXTURES_DIR, load_manifest
from redactor.payees import PayeeRegistry
from redactor.store import open_store

KEY = "correct horse battery staple"
CHECKING_CSV = FIXTURES_DIR / "checking_2026-04.csv"


def _raw_memos() -> list[str]:
    seeds: list[str] = []
    for variants in load_manifest()["payees"].values():
        seeds.extend(variants)
    return seeds


def _seed_store(db) -> None:
    main(["ingest", str(CHECKING_CSV), "--store", str(db), "--key", KEY])


# --------------------------------------------------------------------------- #
# --review: local render of entities (counts, confidence, labels).
# --------------------------------------------------------------------------- #
def test_review_lists_entities_with_labels(tmp_path, capsys):
    db = tmp_path / "store.db"
    _seed_store(db)
    capsys.readouterr()

    rc = main(["payees", "--review", "--store", str(db), "--key", KEY])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAYEE-" in out
    assert "confidence" in out.lower() or "variants" in out.lower()
    # review is the render boundary: at least one revealed label appears.
    assert "Costco" in out or "Netflix" in out or "Whole Foods" in out


def test_review_needs_the_mapping_table(tmp_path, capsys):
    # No store present: refuse, do not render.
    rc = main(["payees", "--review", "--store", str(tmp_path / "nope.db"), "--key", KEY])
    assert rc != 0


# --------------------------------------------------------------------------- #
# --merge / --split / --rename mutate, alias-space confirmation.
# --------------------------------------------------------------------------- #
def test_merge_then_lens_reveals_the_winner(tmp_path, capsys):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")
        winner_real = store.mapping.resolve_alias(winner).reveal()

    rc = main(["payees", "--merge", winner, loser, "--store", str(db), "--key", KEY])
    assert rc == 0
    out = capsys.readouterr().out
    # Alias-space confirmation: names the tokens, not a raw value.
    assert winner in out and loser in out

    # The loser token now lenses to the winner's real value.
    capsys.readouterr()
    main(["lens", "--store", str(db), "--key", KEY, "--text", loser])
    lensed = capsys.readouterr().out
    assert winner_real in lensed


def test_merge_confirmation_is_alias_space_only(tmp_path, capsys):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")

    main(["payees", "--merge", winner, loser, "--store", str(db), "--key", KEY])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    for memo in _raw_memos():
        assert memo not in blob
    assert "Whole Foods" not in blob and "Shell" not in blob


def test_split_unmerges(tmp_path, capsys):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record("WHOLEFDS #1029 SEA")
        loser = reg.record("SHELL OIL 5712341")
        reg.merge(winner, loser)

    rc = main(["payees", "--split", loser, "--store", str(db), "--key", KEY])
    assert rc == 0

    with open_store(db, KEY, create=False) as store:
        # After split the loser resolves to its own value again.
        assert store.mapping.resolve_alias(loser).reveal() != \
            store.mapping.resolve_alias(winner).reveal()


def test_rename_changes_the_label(tmp_path, capsys):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record("WHOLEFDS #1029 SEA")

    rc = main(["payees", "--rename", token, "Whole Foods Market",
               "--store", str(db), "--key", KEY])
    assert rc == 0
    with open_store(db, KEY, create=False) as store:
        assert store.mapping.resolve_alias(token).reveal() == "Whole Foods Market"


def test_mutation_is_journaled_via_cli(tmp_path, capsys):
    db = tmp_path / "store.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record("WHOLEFDS #1029 SEA")

    main(["payees", "--rename", token, "Whole Foods Market",
          "--store", str(db), "--key", KEY])
    with open_store(db, KEY, create=False) as store:
        ops = [e["op"] for e in store.payee_journal()]
    assert "rename" in ops


# --------------------------------------------------------------------------- #
# Ingest summary flags low-confidence groupings (alias-space).
# --------------------------------------------------------------------------- #
def test_ingest_flags_low_confidence_groupings(tmp_path, capsys):
    # Ingest every fixture so the fuzzy Whole Foods grouping ("WF" ~ "WHOLE
    # FOODS") is formed and flagged.
    db = tmp_path / "store.db"
    files = [str(p) for p in sorted(FIXTURES_DIR.glob("*.csv"))]
    rc = main(["ingest", *files, "--store", str(db), "--key", KEY])
    assert rc == 0
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "review" in blob.lower()
    assert "PAYEE-" in blob
    # Still alias-space: no raw memo string leaks into the summary.
    for memo in _raw_memos():
        assert memo not in blob
