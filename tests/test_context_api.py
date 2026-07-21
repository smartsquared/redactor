"""Sanitized-context API — the contract sakuma-finance consumes (story S2.3).

Acceptance criteria exercised here (issue #11):

  - A *typed client* the consumer uses to (a) request alias-space context —
    transactions, balances, date ranges — and (b) send/receive conversation
    turns through the proxy with S2.1 outbound redaction applied.
  - The context surface is alias-space only: every identifying field is a token,
    amounts/dates stay real, and nothing serializes a real identifier off-store.
  - Stable + versioned: the context envelope carries a version.
  - A round-trip over the bundled fixtures: request context, send a redacted
    turn to a (stubbed) model, receive its alias-space reply, and render it back
    to real values *only* at the local lens boundary (S2.2).

The client runs where the mapping table lives (it is built from an ingested
store + resolver), but its whole job is to keep the consumer in alias-space:
un-redaction is never part of this contract — it is the separate local lens.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from redactor.alias import find_aliases
from redactor.context_api import (
    CONTEXT_API_VERSION,
    AccountBalance,
    ContextWindow,
    SanitizedContextClient,
    Turn,
)
from redactor.fixtures import load_statements
from redactor.ingest import ingest_statements
from redactor.lens import lens, resolver_from_table
from redactor.outbound import OutboundRedactor
from redactor.resolve import PayeeResolver
from redactor.store import open_store

KEY = "correct horse battery staple"

# Every real identifier a leak-check must never see cross the model boundary.
REAL_IDENTIFIERS = (
    "Bank of Nowhere",
    "Placeholder National Bank",
    "NOWHERE-CHK-000199",
    "PLACEHOLDER-CC-000042",
    "Whole Foods",
    "WHOLEFDS",
    "Starbucks",
    "SBUX",
    "Netflix",
    "Amazon",
    "AMZN",
    "Shell",
    "Costco",
)


def _build_client(store) -> SanitizedContextClient:
    """Ingest the bundled fixtures with the S1.3 resolver wired into the S1.4
    seam, then build the sanitized-context client from the resulting projection
    + outbound redactor (the wiring a consumer performs once at startup)."""
    resolver = PayeeResolver()
    projection = ingest_statements(
        store, load_statements(), resolve_payee=resolver.resolve_display
    )
    redactor = OutboundRedactor.from_store(store, resolver)
    return SanitizedContextClient(projection=projection, redactor=redactor)


def _assert_no_real_identifier(text: str) -> None:
    for leak in REAL_IDENTIFIERS:
        assert leak not in text, f"real identifier leaked into alias-space: {leak!r}"


# --- alias-space context: transactions, balances, date ranges --------------- #


def test_context_records_are_alias_space_only(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        ctx = client.context()

        assert isinstance(ctx, ContextWindow)
        assert ctx.records, "expected fixture transactions in the context window"
        _assert_no_real_identifier(ctx.to_json())
        for rec in ctx.records:
            assert rec.account.startswith("ACCT-")
            assert rec.institution.startswith("INST-")
            assert rec.payee.startswith("PAYEE-")


def test_context_is_versioned(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)
        ctx = client.context()
        assert ctx.version == CONTEXT_API_VERSION
        assert json.loads(ctx.to_json())["version"] == CONTEXT_API_VERSION


def test_context_date_range_filters_transactions(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        may = client.context(start="2026-05-01", end="2026-05-31")

        assert may.records, "expected May transactions"
        assert may.start == "2026-05-01"
        assert may.end == "2026-05-31"
        assert all("2026-05-01" <= r.date <= "2026-05-31" for r in may.records)
        # The window is a strict subset of the full history.
        assert len(may.records) < len(client.context().records)


def test_context_balances_aggregate_per_account(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)
        ctx = client.context()

        assert ctx.balances, "expected per-account balances"
        for bal in ctx.balances:
            assert isinstance(bal, AccountBalance)
            assert bal.account.startswith("ACCT-")
            assert bal.institution.startswith("INST-")
            assert bal.txn_count > 0
            # net is the signed sum of that account's transaction amounts.
            recs = [r for r in ctx.records if r.account == bal.account]
            assert bal.txn_count == len(recs)
            assert round(bal.net, 2) == round(sum(r.amount for r in recs), 2)
        _assert_no_real_identifier(ctx.to_json())


# --- send/receive conversation turns through the proxy ---------------------- #


def test_send_turn_applies_outbound_redaction(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        turn = client.send("How much did I spend at Whole Foods last month?")

        assert isinstance(turn, Turn)
        _assert_no_real_identifier(turn.text)
        assert any(t.startswith("PAYEE-") for t in [s.token for s in turn.substitutions])
        assert turn.clean  # a known entity -> no unresolved warning


def test_send_turn_warns_on_unknown_entity(tmp_path):
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        turn = client.send("How much did I spend at Freshmart Grocers?")

        assert turn.warnings, "unknown entity must surface a warning, not leak silently"
        assert not turn.clean


def test_receive_turn_stays_in_alias_space(tmp_path):
    """receive() wraps a model reply as an alias-space turn. It must NOT
    un-redact — that is the local lens boundary, deliberately outside this
    off-machine contract."""
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        reply = "You spent the most at PAYEE-1 and PAYEE-2 this month."
        turn = client.receive(reply)

        assert isinstance(turn, Turn)
        assert turn.text == reply  # untouched — still alias-space
        _assert_no_real_identifier(turn.text)


# --- round-trip over fixtures ----------------------------------------------- #


def test_round_trip_renders_only_at_local_lens(tmp_path):
    """The full contract loop: request context, send a redacted turn, receive an
    alias-space reply, and render real values ONLY at the local lens."""
    with open_store(tmp_path / "s.db", KEY) as store:
        client = _build_client(store)

        # 1. alias-space context out
        ctx = client.context(start="2026-05-01", end="2026-05-31")
        _assert_no_real_identifier(ctx.to_json())

        # 2. user turn -> outbound redaction -> safe to send to the model
        sent = client.send("Did I hit Whole Foods in May?")
        _assert_no_real_identifier(sent.text)
        assert any(s.token.startswith("PAYEE-") for s in sent.substitutions)

        # 3. the model answers in alias-space over context tokens; it never saw a
        #    real name — only ACCT-/INST- tokens from the redacted context.
        bal = ctx.balances[0]
        model_reply = (
            f"Your {bal.institution} checking ({bal.account}) netted {bal.net:+.2f}."
        )
        received = client.receive(model_reply)
        assert received.text == model_reply  # not un-redacted here — still alias-space
        _assert_no_real_identifier(received.text)

        # 4. render to the human — ONLY here, at the local lens (S2.2)
        rendered = lens(received.text, resolver_from_table(store.mapping)).text
        assert find_aliases(rendered) == []          # nothing left in alias-space
        assert "Bank of Nowhere" in rendered         # a real value, produced locally
        assert bal.institution not in rendered       # the token was resolved back


# --- the shipped round-trip demo script ------------------------------------- #

_DEMO = Path(__file__).resolve().parent.parent / "examples" / "sanitized_context_roundtrip.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("_s23_demo", _DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_run_keeps_boundary_alias_space(tmp_path):
    """The demo's round trip: everything crossing the model boundary is
    alias-space; a real value appears only in the locally-rendered reply."""
    demo = _load_demo()
    with open_store(tmp_path / "s.db", demo.DEMO_KEY) as store:
        out = demo.run(store)

    # Context + the two turns that cross the model boundary are alias-space.
    _assert_no_real_identifier(out["context"].to_json())
    _assert_no_real_identifier(out["sent"].text)
    _assert_no_real_identifier(out["received"].text)
    assert find_aliases(out["received"].text)  # the reply is stated in tokens

    # The lens render is the only place a real value surfaces.
    assert "Bank of Nowhere" in out["rendered"]
    assert find_aliases(out["rendered"]) == []


def test_demo_main_runs_clean():
    """`python -m examples.sanitized_context_roundtrip` exits 0 and renders a
    real value only after the lens step."""
    proc = subprocess.run(
        [sys.executable, "-m", "examples.sanitized_context_roundtrip"],
        cwd=_DEMO.parent.parent,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Bank of Nowhere" in proc.stdout  # rendered locally at the lens
    assert "round trip" in proc.stdout.lower()
