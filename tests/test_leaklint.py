"""Leak-lint (story S1.4): teeth behind "zero seeded identifiers in alias-space".

The lint scans an alias-space projection for any seeded identifier (raw memo
variant, account id, institution, routing number, display mask) and for
real-looking numbers via the fake-ness checks. A projection that has been
correctly aliased carries none of these; a deliberately-seeded leak must be
caught (mutation test).
"""
from __future__ import annotations

from redactor.leaklint import scan_projection, seeds_from_manifest

MANIFEST = {
    "accounts": {
        "checking": {
            "institution": "Bank of Nowhere",
            "account_id": "NOWHERE-CHK-000199",
            "routing_number": "123456789",
            "display_mask": "••••0199",
        },
    },
    "payees": {
        "Whole Foods Market": ["WHOLEFDS #1029 SEA", "WF MKT 445 SEATTLE WA"],
    },
}


def test_seeds_from_manifest_collects_all_identifier_kinds():
    seeds = seeds_from_manifest(MANIFEST)
    assert "NOWHERE-CHK-000199" in seeds
    assert "Bank of Nowhere" in seeds
    assert "123456789" in seeds
    assert "WHOLEFDS #1029 SEA" in seeds


def test_clean_projection_passes():
    clean = '{"records": [{"account": "ACCT-1", "payee": "PAYEE-3", "amount": -52.18}]}'
    assert scan_projection(clean, seeds_from_manifest(MANIFEST)) == []


def test_leak_of_raw_memo_is_caught():
    leaked = '{"records": [{"account": "ACCT-1", "payee": "WHOLEFDS #1029 SEA"}]}'
    leaks = scan_projection(leaked, seeds_from_manifest(MANIFEST))
    assert any("WHOLEFDS #1029 SEA" in leak.match for leak in leaks)


def test_leak_of_account_id_is_caught():
    leaked = '{"records": [{"account": "NOWHERE-CHK-000199"}]}'
    leaks = scan_projection(leaked, seeds_from_manifest(MANIFEST))
    assert leaks, "an account id in alias-space is a leak"


def test_leak_of_institution_is_caught():
    leaked = '{"records": [{"institution": "Bank of Nowhere"}]}'
    assert scan_projection(leaked, seeds_from_manifest(MANIFEST))


def test_scan_is_case_insensitive_on_seeds():
    leaked = '{"records": [{"payee": "wholefds #1029 sea"}]}'
    assert scan_projection(leaked, seeds_from_manifest(MANIFEST))
