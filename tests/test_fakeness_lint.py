"""S0.2 accept: the fake-ness lint (real-looking routing/account numbers) passes.

Two directions, both required:
  * the committed fixtures + docs are CLEAN (no real-looking identifiers), and
  * the lint actually BITES on planted real-looking numbers (so a green run
    means "nothing real", not "the check is inert").
"""
from __future__ import annotations

from redactor.fakeness import scan_paths, scan_text


def test_repo_fixtures_and_docs_are_clean():
    findings = scan_paths()
    assert findings == [], f"real-looking identifiers found: {findings}"


def test_lint_flags_a_valid_aba_routing_number():
    # 021000021 is a real, ABA-checksum-valid routing number.
    findings = scan_text("wire to routing 021000021 please")
    assert any(f.kind == "aba_routing" for f in findings), findings


def test_lint_ignores_the_fixture_fake_routing_number():
    # 123456789 fails the ABA checksum on purpose.
    findings = scan_text("routing 123456789")
    assert not any(f.kind == "aba_routing" for f in findings), findings


def test_lint_flags_a_valid_luhn_card_number():
    # 4111111111111111 is the canonical Luhn-valid test Visa.
    findings = scan_text("card 4111111111111111 on file")
    assert any(f.kind == "luhn_card" for f in findings), findings


def test_lint_ignores_a_luhn_invalid_long_number():
    findings = scan_text("reference 4111111111111112")
    assert not any(f.kind == "luhn_card" for f in findings), findings


def test_lint_flags_ssn_shaped_string():
    findings = scan_text("ssn 123-45-6789")
    assert any(f.kind == "ssn" for f in findings), findings


def test_findings_carry_location_and_matched_text():
    findings = scan_text("card 4111111111111111")
    f = findings[0]
    assert f.match == "4111111111111111"
    assert f.line == 1
