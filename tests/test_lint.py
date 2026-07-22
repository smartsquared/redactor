"""Detection-based leak-lint (story S0.2, issue #29).

Fixtures had a seed manifest; real data has no ground truth, so this lint proves
an alias-space projection clean by *detection*, not by comparison to a known set
of seeds. Two pillars, taken verbatim from the story:

  * the finance Detector (Presidio) over the text, and
  * structural checks — fakeness-lint's ABA/Luhn/SSN validators plus
    alias-contract conformance (identifying fields must be canonical tokens).

Acceptance criteria exercised here (issue #29):

  - clean on the fixture projections;
  - bites on planted real-looking identifiers (checksum-valid routing/card
    numbers, detected accounts) and on raw names left in identifying fields;
  - ingest refuses to emit a projection that fails the lint (an override flag
    exists but prints a red warning).
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from redactor.fixtures import Statement, Transaction, load_statements
from redactor.ingest import LintFailure, ingest_statement, ingest_statements
from redactor.lint import LintFinding, lint_file, lint_projection, lint_text
from redactor.store import open_store

KEY = "correct horse battery staple"

# 021000021 is a real, ABA-checksum-valid routing number; 4111111111111111 is
# the canonical Luhn-valid test card. Fixtures dodge both on purpose, so either
# one riding in an alias-space artifact is a genuine leak.
VALID_ABA = "021000021"
VALID_LUHN = "4111111111111111"


# --- pillar 1 + 2: detection + structural over free text --------------------


def test_lint_text_clean_on_pure_alias_space():
    clean = '{"records": [{"account": "ACCT-1", "payee": "PAYEE-3", '
    clean += '"date": "2026-04-05", "amount": -52.18, "ttype": "DEBIT"}]}'
    assert lint_text(clean) == []


def test_lint_bites_on_checksum_valid_routing_number():
    findings = lint_text(f'{{"note": "wire via {VALID_ABA} today"}}')
    assert any(VALID_ABA in f.match for f in findings), findings


def test_lint_bites_on_luhn_valid_card_number():
    findings = lint_text(f'{{"note": "card {VALID_LUHN} on file"}}')
    assert any(VALID_LUHN in f.match for f in findings), findings


def test_lint_bites_on_ssn_shaped_string():
    findings = lint_text("ssn 123-45-6789 leaked")
    assert findings, findings


def test_lint_bites_on_detected_account_id():
    # The finance Detector's account-id pattern fires with no manifest at all.
    findings = lint_text('{"account": "PLACEHOLDER-CC-000042"}')
    assert any("PLACEHOLDER-CC-000042" in f.match for f in findings), findings


def test_lint_ignores_the_fixture_fake_routing_number():
    # 123456789 fails the ABA checksum, so it is not real-looking; but a bare
    # 9-digit run is still an account-shaped span the Detector may flag. What we
    # assert here is the structural checksum check does NOT call it a routing
    # number (that would make the fake fixtures un-lintable).
    findings = lint_text("routing 123456789")
    assert not any(f.kind == "aba_routing" for f in findings), findings


# --- pillar 2: alias-contract conformance -----------------------------------


def test_conformance_flags_raw_name_in_payee_field():
    leaking = {
        "version": 1,
        "records": [
            {"account": "ACCT-1", "institution": "INST-1",
             "payee": "Whole Foods Market", "date": "2026-04-05", "amount": -52.18},
        ],
    }
    # detection/structural alone may miss a bare name; conformance must catch it
    # because "Whole Foods Market" is not a canonical alias token.
    from redactor.lint import lint_projection_dict

    findings = lint_projection_dict(leaking)
    assert any(f.kind == "alias_nonconformance" for f in findings), findings
    assert any("Whole Foods Market" in f.match for f in findings), findings


def test_conformance_clean_when_all_identifying_fields_are_tokens():
    clean = {
        "version": 1,
        "records": [
            {"account": "ACCT-1", "institution": "INST-2",
             "payee": "PAYEE-7", "date": "2026-04-05", "amount": -52.18,
             "ttype": "DEBIT", "category": "Groceries"},
        ],
    }
    from redactor.lint import lint_projection_dict

    assert lint_projection_dict(clean) == []


# --- acceptance: clean on the fixture projections ---------------------------


def test_fixture_projection_is_lint_clean(tmp_path):
    statements = load_statements()
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)
    assert lint_projection(projection) == [], lint_projection(projection)


def test_lint_file_clean_on_written_fixture_projection(tmp_path):
    statements = load_statements()
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)
    out = tmp_path / "projection.json"
    projection.write_json(out)
    assert lint_file(out) == [], lint_file(out)


# --- acceptance: ingest refuses to emit a lint-failing projection -----------


def _leaky_statement() -> Statement:
    """A statement whose non-aliased passthrough field carries a real-looking
    identifier — the exact leak aliasing alone cannot catch, so the lint must.
    """
    return Statement(
        account_type="checking",
        format="csv",
        month="2026-04",
        institution="Bank of Nowhere",
        account_id="NOWHERE-CHK-000199",
        source=__import__("pathlib").Path("leaky.csv"),
        transactions=[
            Transaction(
                date="2026-04-05",
                amount=-52.18,
                description="SOME MERCHANT",
                # category is a non-identifying passthrough — but here it smuggles
                # a checksum-valid routing number into alias-space.
                category=f"SWEEP {VALID_ABA}",
            )
        ],
    )


def test_ingest_refuses_lint_failing_projection(tmp_path):
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        with pytest.raises(LintFailure) as excinfo:
            ingest_statement(store, _leaky_statement())
    assert excinfo.value.findings, "LintFailure must carry the findings"


def test_ingest_override_emits_projection_with_red_warning(tmp_path):
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            projection = ingest_statement(store, _leaky_statement(), allow_leaks=True)
    assert projection, "override must still return the projection"
    warning = stderr.getvalue()
    assert warning, "override must print a warning"
    # A red ANSI escape marks the warning as loud, not silent.
    assert "\x1b[" in warning, f"expected a red warning, got: {warning!r}"


def test_ingest_statements_clean_fixtures_do_not_raise(tmp_path):
    statements = load_statements()
    with open_store(tmp_path / "store.db", KEY, create=True) as store:
        projection = ingest_statements(store, statements)
    assert projection.records


def test_lint_finding_is_our_dataclass():
    f = LintFinding(kind="ssn", match="123-45-6789", detail="1:5")
    for attr in ("kind", "match", "detail"):
        assert hasattr(f, attr)
