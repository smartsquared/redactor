"""Executable guard for the third-party-text injection scrub (story S1.5).

Statement memos and payee strings are attacker-controlled input (brief
§Three threats, threat 3). Before they enter an alias-space projection that
eventually reaches a model, instruction-shaped content must be neutralized —
without mangling the benign memos that entity resolution depends on.

The corpus is data-driven from ``injection_memos.json`` so seeding a new
payload is a fixture edit, not a code edit.
"""
from __future__ import annotations

import json

import pytest

from redactor.fixtures import FIXTURES_DIR, load_statements
from redactor.scrub import PLACEHOLDER, scrub, scrub_text

_INJECTION_FIXTURE = FIXTURES_DIR / "injection_memos.json"


def _load_injection_fixture() -> dict:
    with open(_INJECTION_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


_FIXTURE = _load_injection_fixture()
_INJECTION_MEMOS = _FIXTURE["injection_memos"]
_BENIGN_MEMOS = _FIXTURE["benign_memos"]


@pytest.mark.parametrize("memo", _INJECTION_MEMOS, ids=[m["carrier"] for m in _INJECTION_MEMOS])
def test_seeded_injection_payloads_are_neutralized(memo):
    """Every seeded instruction-shaped phrase is gone after the scrub."""
    result = scrub_text(memo["raw"])

    assert result.modified, f"scrub failed to flag injection in {memo['raw']!r}"
    lowered = result.text.lower()
    for phrase in memo["must_neutralize"]:
        assert phrase.lower() not in lowered, (
            f"instruction-shaped {phrase!r} survived the scrub: {result.text!r}"
        )


@pytest.mark.parametrize("memo", _INJECTION_MEMOS, ids=[m["carrier"] for m in _INJECTION_MEMOS])
def test_injection_carrier_stays_legible(memo):
    """The benign payee carrier survives so entity resolution still works."""
    result = scrub_text(memo["raw"])
    assert memo["carrier"] in result.text, (
        f"scrub ate the legible carrier {memo['carrier']!r}: {result.text!r}"
    )
    assert PLACEHOLDER in result.text  # neutralized content leaves a visible marker


@pytest.mark.parametrize("memo", _BENIGN_MEMOS)
def test_benign_memos_pass_through_unchanged(memo):
    """Real statement memos must be left byte-for-byte alone (no false positives)."""
    result = scrub_text(memo)
    assert not result.modified, f"benign memo wrongly flagged: {memo!r}"
    assert result.text == memo
    assert scrub(memo) == memo


def test_every_real_fixture_memo_is_benign():
    """The scrub never fires on any memo in the shipped statement fixtures."""
    flagged = []
    for stmt in load_statements():
        for txn in stmt.transactions:
            if scrub_text(txn.description).modified:
                flagged.append((stmt.source.name, txn.description))
    assert not flagged, f"scrub false-positived on real fixture memos: {flagged}"


def test_scrub_is_idempotent():
    """Scrubbing already-scrubbed text changes nothing further."""
    raw = _INJECTION_MEMOS[0]["raw"]
    once = scrub(raw)
    assert scrub(once) == once


def test_scrub_returns_neutralized_text_for_convenience_helper():
    raw = _INJECTION_MEMOS[0]["raw"]
    assert scrub(raw) == scrub_text(raw).text


def test_empty_and_plain_text_are_noops():
    for text in ["", "   ", "just a normal note", "coffee 4.50"]:
        result = scrub_text(text)
        assert not result.modified
        assert result.text == text


def test_fixture_file_exists_and_is_shaped():
    assert _INJECTION_FIXTURE.exists()
    assert _INJECTION_MEMOS and _BENIGN_MEMOS
    for entry in _INJECTION_MEMOS:
        assert {"raw", "carrier", "must_neutralize"} <= entry.keys()
        assert entry["carrier"] in entry["raw"]
