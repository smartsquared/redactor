"""Entity-level payee category tags (docs/payee-categories.md).

The feature under test: a **closed-vocabulary** category tag bound to a
canonical PAYEE entity locally, riding into projections as ``payee_category``
so a consumer can answer eligibility questions ("which of these could an HSA
pay for?") entirely in alias-space. The bars pinned here:

  - the vocabulary is closed — free text is refused at tag time AND flagged by
    the lint on the artifact side (the field can never smuggle a real name);
  - tags live on the canonical head, so merges inherit correctly;
  - the proposer is local-only advice: it reads real display labels, proposes
    from keyword rules, and writes nothing;
  - ingest re-projection and ``redactor export`` both carry the tag.

Fixture payees are provably fake (FAKEVILLE / EXAMPLE / TESTTOWN) per the
fixture-only rule; keyword rules may name real national chains, fixtures do not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redactor.categories import CATEGORIES, propose, validate_category
from redactor.cli import main
from redactor.export import export_month
from redactor.fixtures import Statement, Transaction
from redactor.ingest import ingest_statement
from redactor.lint import lint_projection_dict
from redactor.payees import PayeeRegistry
from redactor.store import open_store

KEY = "correct horse battery staple"

PHARMACY_MEMO = "FAKEVILLE PHARMACY #000"
DENTAL_MEMO = "EXAMPLE FAMILY DENTAL LLC"
GROCER_MEMO = "GENERIC GROCER"


def _statement(*memos: str) -> Statement:
    txns = [
        Transaction(date="2026-05-03", amount=-10.0 - i, description=memo)
        for i, memo in enumerate(memos)
    ]
    return Statement(
        account_type="checking",
        format="csv",
        month="2026-05",
        institution="Test Bank of Examplestan",
        account_id="FAKE-CHK-000",
        source=Path("synthetic.csv"),
        transactions=txns,
    )


# --------------------------------------------------------------------------- #
# The closed vocabulary.
# --------------------------------------------------------------------------- #
def test_every_vocabulary_member_validates():
    for category in CATEGORIES:
        assert validate_category(category) == category


def test_free_text_is_refused():
    with pytest.raises(ValueError, match="unknown category"):
        validate_category("Fakeville Pharmacy")  # a real-name-shaped string
    with pytest.raises(ValueError):
        validate_category("")  # untagged is the absence of a tag, not a tag


# --------------------------------------------------------------------------- #
# Registry: tag / untag / category_of / journal.
# --------------------------------------------------------------------------- #
def test_tag_untag_roundtrip_and_review_surface(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record(PHARMACY_MEMO)

        reg.tag(token, "pharmacy")
        assert reg.category_of(token) == "pharmacy"
        by_token = {e.token: e for e in reg.review()}
        assert by_token[token].category == "pharmacy"

        reg.untag(token)
        assert reg.category_of(token) == ""
        by_token = {e.token: e for e in reg.review()}
        assert by_token[token].category == ""


def test_tag_refuses_free_text(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record(PHARMACY_MEMO)
        with pytest.raises(ValueError, match="unknown category"):
            reg.tag(token, "Fakeville Pharmacy #000")
        assert reg.category_of(token) == ""


def test_tag_lands_on_the_canonical_head_across_a_merge(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        winner = reg.record(DENTAL_MEMO)
        loser = reg.record(GROCER_MEMO)
        reg.merge(winner, loser)

        # Tagging via the merged-away loser must tag the entity — the head.
        reg.tag(loser, "dental")
        assert reg.category_of(winner) == "dental"
        assert reg.category_of(loser) == "dental"


def test_tag_and_untag_are_journaled_alias_space(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record(PHARMACY_MEMO)
        reg.tag(token, "pharmacy")
        reg.untag(token)
        ops = [(j["op"], j["winner"], j["note"]) for j in reg.journal()]
        assert ("tag", token, "pharmacy") in ops
        assert ("untag", token, None) in ops
        # Journal stays alias-space: no entry carries the real memo.
        assert all(PHARMACY_MEMO not in str(j) for j in reg.journal())


def test_tags_survive_a_reopen(tmp_path):
    db = tmp_path / "s.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record(PHARMACY_MEMO)
        reg.tag(token, "pharmacy")
    with open_store(db, KEY, create=False) as store:
        assert PayeeRegistry(store).category_of(token) == "pharmacy"


# --------------------------------------------------------------------------- #
# The local proposer (rules over display labels; writes nothing).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("FAKEVILLE PHARMACY #000", "pharmacy"),
        ("EXAMPLE FAMILY DENTAL LLC", "dental"),
        ("TESTTOWN OPTOMETRY CTR", "vision"),
        ("ST EXAMPLE HOSPITAL", "medical-provider"),
        ("EXAMPLE URGENT CARE 000", "medical-provider"),
        ("WALMART SUPERCENTER #0000", "mixed-retailer"),
        ("GENERIC GROCER", None),  # no opinion is not a non-medical verdict
        ("", None),
    ],
)
def test_proposer_rules(label, expected):
    assert propose(label) == expected


def test_dental_clinic_is_dental_not_medical_provider():
    # Rule order is specificity: the dental rule outranks the CLINIC keyword.
    assert propose("EXAMPLE DENTAL CLINIC") == "dental"


def test_propose_categories_covers_only_untagged_entities(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        pharmacy = reg.record(PHARMACY_MEMO)
        reg.record(GROCER_MEMO)  # no rule matches; never proposed

        proposals = reg.propose_categories()
        assert [(p.token, p.category) for p in proposals] == [(pharmacy, "pharmacy")]

        reg.tag(pharmacy, "pharmacy")
        assert reg.propose_categories() == []


# --------------------------------------------------------------------------- #
# The tag rides into projections (ingest re-projection + export).
# --------------------------------------------------------------------------- #
def test_ingest_projection_carries_the_tag(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        reg = PayeeRegistry(store)
        statement = _statement(PHARMACY_MEMO, GROCER_MEMO)
        records = ingest_statement(store, statement, registry=reg)
        assert [r.payee_category for r in records] == ["", ""]  # nothing tagged yet

        reg.tag(reg.record(PHARMACY_MEMO), "pharmacy")
        records = ingest_statement(store, statement, registry=reg)
        by_payee = {r.payee: r.payee_category for r in records}
        assert "pharmacy" in by_payee.values()
        assert "" in by_payee.values()  # the untagged grocer stays untagged


def test_export_carries_the_tag_and_stays_green(tmp_path):
    db = tmp_path / "s.db"
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        ingest_statement(store, _statement(PHARMACY_MEMO, GROCER_MEMO), registry=reg)
        reg.tag(reg.record(PHARMACY_MEMO), "pharmacy")

        out = tmp_path / "proj-2026-05.json"
        result = export_month(store, "2026-05", out)
        assert result.verdict == "green"

    data = json.loads(out.read_text(encoding="utf-8"))
    categories = sorted(r["payee_category"] for r in data["records"])
    assert categories == ["", "pharmacy"]


# --------------------------------------------------------------------------- #
# Lint: the closed set is enforced on the artifact side too.
# --------------------------------------------------------------------------- #
def _record_dict(payee_category: str) -> dict:
    return {
        "account": "ACCT-1",
        "institution": "INST-1",
        "payee": "PAYEE-1",
        "date": "2026-05-03",
        "amount": -10.0,
        "ttype": "",
        "category": "",
        "payee_category": payee_category,
    }


def test_lint_flags_an_off_vocabulary_category():
    data = {"version": 1, "records": [_record_dict("Fakeville Pharmacy")]}
    findings = lint_projection_dict(data)
    assert [f.kind for f in findings] == ["category_nonconformance"]
    assert findings[0].match == "Fakeville Pharmacy"


def test_lint_accepts_vocabulary_members_and_untagged():
    for value in ["", "pharmacy", "mixed-retailer"]:
        data = {"version": 1, "records": [_record_dict(value)]}
        assert lint_projection_dict(data) == []


# --------------------------------------------------------------------------- #
# CLI: --tag / --untag / --categorize.
# --------------------------------------------------------------------------- #
def _seed_store(db) -> str:
    with open_store(db, KEY, create=True) as store:
        reg = PayeeRegistry(store)
        token = reg.record(PHARMACY_MEMO)
        reg.record(GROCER_MEMO)
    return token


def test_cli_categorize_proposes_then_tag_confirms(tmp_path, capsys):
    db = tmp_path / "s.db"
    token = _seed_store(db)

    rc = main(["payees", "--store", str(db), "--key", KEY, "--categorize"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"{token}  ->  pharmacy" in out

    rc = main(["payees", "--store", str(db), "--key", KEY, "--tag", token, "pharmacy"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"tagged {token} as pharmacy" in out

    # The review listing shows the tag; a fresh --categorize has nothing left.
    rc = main(["payees", "--store", str(db), "--key", KEY, "--review"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[pharmacy]" in out
    rc = main(["payees", "--store", str(db), "--key", KEY, "--categorize"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no category proposals" in out


def test_cli_tag_refuses_free_text_value_free(tmp_path, capsys):
    db = tmp_path / "s.db"
    token = _seed_store(db)
    rc = main(["payees", "--store", str(db), "--key", KEY, "--tag", token, "bogus"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unknown category" in captured.err
    # The refusal lists the vocabulary, never a display label / memo.
    assert PHARMACY_MEMO not in captured.err


def test_cli_untag(tmp_path, capsys):
    db = tmp_path / "s.db"
    token = _seed_store(db)
    main(["payees", "--store", str(db), "--key", KEY, "--tag", token, "pharmacy"])
    capsys.readouterr()
    rc = main(["payees", "--store", str(db), "--key", KEY, "--untag", token])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"untagged {token}" in out
