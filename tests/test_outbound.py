"""Outbound forward-redaction — the user→model half of the proxy (story S2.1).

Acceptance criteria exercised here (issue #9):

  - Fixture utterances mentioning known entities redact correctly: the real
    string is replaced by its stable alias token before the text could leave the
    machine, resolving against the mapping table + entity resolution (S1.2/S1.3).
  - Unknown entities pass with a *warning surface*, not a silent leak: a mention
    that resolves to nothing known is flagged, so the local proxy can decide —
    it never slips through unnoticed.

Redaction runs on the machine that holds the mapping table (the outbound proxy),
so these tests build the redactor from an ingested fixture store plus the same
payee resolver used at ingest.
"""
from __future__ import annotations

from redactor.alias import find_aliases
from redactor.fixtures import load_statements
from redactor.ingest import ingest_statements
from redactor.outbound import OutboundRedactor
from redactor.resolve import PayeeResolver
from redactor.store import open_store

KEY = "correct horse battery staple"


def _build_redactor(store):
    """Ingest the bundled fixtures with the S1.3 resolver wired into S1.4's seam,
    then build the outbound redactor from the resulting store + resolver."""
    resolver = PayeeResolver()
    # The intended wiring of the S1.3 moat into the S1.4 ingest seam: the resolver
    # collapses memo variants to one stable entity, whose id is the alias key.
    def resolve_payee(memo: str) -> str:
        return str(resolver.resolve(memo))

    ingest_statements(store, load_statements(), resolve_payee=resolve_payee)
    return OutboundRedactor.from_store(store, resolver)


def _payee_token_for(store, resolver, memo: str) -> str:
    eid = resolver.match(memo)
    assert eid is not None
    token = store.mapping.resolve_token("PAYEE", str(eid))
    assert token is not None
    return token


# --- known entities redact correctly ---------------------------------------- #


def test_known_payee_mention_redacts_to_its_token(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        resolver = PayeeResolver()
        ingest_statements(
            store, load_statements(),
            resolve_payee=lambda m: str(resolver.resolve(m)),
        )
        redactor = OutboundRedactor.from_store(store, resolver)

        result = redactor.redact("How much did I spend at Whole Foods last month?")

        expected = _payee_token_for(store, resolver, "WHOLE FOODS MKT #10029")
        assert "Whole Foods" not in result.text
        assert expected in result.text
        assert any(s.token == expected for s in result.substitutions)
        assert not result.warnings


def test_friendly_name_and_raw_memo_share_one_token(tmp_path):
    """Entity resolution: the friendly mention and a garbage memo variant must
    forward-redact to the *same* PAYEE token (alias-contract §2.4 stability)."""
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)

        friendly = redactor.redact("Anything at Costco?")
        garbage = redactor.redact("what about COSTCO WHSE #0044")

        ft = [s.token for s in friendly.substitutions]
        gt = [s.token for s in garbage.substitutions]
        assert ft and gt and ft == gt


def test_known_institution_redacts(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)

        result = redactor.redact("Show my Bank of Nowhere checking balance")

        assert "Bank of Nowhere" not in result.text
        tokens = [m.canonical for m in find_aliases(result.text)]
        assert any(t.startswith("INST-") for t in tokens)


def test_redacted_text_carries_no_real_identifier(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)
        result = redactor.redact(
            "I paid Netflix and Amazon, and topped up at Shell near Bank of Nowhere"
        )
        for leak in ("Netflix", "Amazon", "Shell", "Bank of Nowhere"):
            assert leak not in result.text, f"outbound leak: {leak!r}"
        assert not result.warnings


def test_repeated_mention_is_stable(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)
        first = redactor.redact("spend at Starbucks?")
        second = redactor.redact("and again at Starbucks?")
        assert [s.token for s in first.substitutions] == [
            s.token for s in second.substitutions
        ]


# --- unknown entities warn, never silently leak ----------------------------- #


def test_unknown_entity_warns_not_silent_leak(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)

        result = redactor.redact("How much did I spend at Freshmart Grocers?")

        # Not silent: the unresolved entity is surfaced as a warning.
        assert result.warnings, "unknown entity leaked silently — no warning"
        assert any("Freshmart" in w.surface for w in result.warnings)
        assert not result.clean


def test_benign_prose_yields_no_warnings_or_substitutions(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)

        result = redactor.redact("How much did I spend last month in total?")

        assert result.text == "How much did I spend last month in total?"
        assert not result.substitutions
        assert not result.warnings
        assert result.clean


def test_redact_convenience_returns_str(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        redactor = _build_redactor(store)
        out = redactor.redact_text("Anything at Costco?")
        assert isinstance(out, str)
        assert "Costco" not in out


# --- resolver.match() is non-mutating --------------------------------------- #


def test_resolver_match_is_non_mutating():
    resolver = PayeeResolver()
    resolver.resolve("WHOLE FOODS MKT #10029")
    before = resolver.entity_count()

    assert resolver.match("Whole Foods") is not None  # known -> resolves
    assert resolver.match("Totally Unknown Merchant XYZ") is None  # unknown -> None
    assert resolver.entity_count() == before, "match() must not create entities"
