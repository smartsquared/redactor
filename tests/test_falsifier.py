"""Leak-check falsifier harness — the m01 milestone gate (story S2.4).

This test file *is* the gate running in CI. It drives a scripted multi-turn
bookkeeping conversation over the synthetic fixtures through the full pipeline
(ingest -> alias-space context -> outbound redaction -> stub model -> inbound
receive -> local lens) and asserts two things about it, matching the S2.4
acceptance criteria (issue #12):

  1. **No leak.** No seeded identifier — a raw memo variant, an account id, an
     institution name, a routing number, a display mask — nor any real-looking
     number appears in *anything that crossed the model boundary* (the shipped
     context JSON and every sent/received turn).
  2. **Useful.** The conversation answers real bookkeeping questions correctly:
     each answer the stub model computes purely from the alias-space context
     matches an oracle computed independently from the raw fixture files.

And it is *mutation-tested*: deliberately-seeded regressions (a model that leaks
a real institution, a projection that carries a raw identifier, a model that
does the arithmetic wrong) must each flip the harness red. A gate that cannot
fail is not a gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from redactor.context_api import SanitizedContextClient
from redactor.falsifier import (
    DEFAULT_SCRIPT,
    ModelReply,
    build_client,
    canonical_statements,
    falsify,
    run_conversation,
)
from redactor.fixtures import load_manifest
from redactor.leaklint import seeds_from_manifest
from redactor.projection import AliasRecord, AliasSpaceProjection
from redactor.store import open_store

KEY = "correct horse battery staple"


# --- passes on the real pipeline -------------------------------------------- #


def test_falsifier_passes_on_real_pipeline(tmp_path):
    """The whole point: run the real pipeline end to end and the gate is green —
    no leaks and every answer correct."""
    with open_store(tmp_path / "s.db", KEY) as store:
        report = falsify(store)

    assert report.ok, (
        f"falsifier should pass on the real pipeline; "
        f"leaks={report.leaks} answer_failures={report.answer_failures}"
    )
    assert report.leaks == []
    assert report.answer_failures == []


def test_conversation_is_multi_turn_and_useful(tmp_path):
    """A genuinely multi-turn conversation whose every answer is correct."""
    with open_store(tmp_path / "s.db", KEY) as store:
        run = run_conversation(store)

    assert len(run.records) >= 3, "the gate must exercise a multi-turn conversation"
    assert len(run.records) == len(DEFAULT_SCRIPT)
    # Every scripted answer is correct against the fixtures.
    for rec in run.records:
        assert rec.ok, f"answer for {rec.probe.name!r} was wrong: {rec.detail}"
    # The conversation actually did work: at least one merchant was recognized
    # and redacted to a PAYEE token that the model then reasoned over.
    assert any(rec.probe.kind == "payee_spend" and rec.reply.tokens for rec in run.records)


# --- payees un-redact to a human-readable name, not an entity key ----------- #


def test_payee_unredacts_to_human_name_not_entity_key(tmp_path):
    """A PAYEE token must un-redact at the local lens to a human-readable merchant
    name, never the S1.3 resolver's internal entity key (issue #25).

    The lens faithfully reveals whatever ingest stored as the PAYEE real value, so
    this asserts ingest stored a display name rather than a canonical entity id:
    the accountability render is "you overspent at Costco", not "... at 5".
    """
    with open_store(tmp_path / "s.db", KEY) as store:
        run = run_conversation(store)

    # The Costco payee-spend turn must name the merchant in the rendered output.
    costco = next(r for r in run.records if r.probe.name == "costco-may-spend")
    assert "costco" in costco.rendered.lower(), (
        f"payee did not render to a human name: {costco.rendered!r}"
    )

    # The "... at <payee>." slot of the largest-expense turn must be a name, never
    # a bare integer entity key (the exact symptom in the issue: "... at 5").
    largest = next(r for r in run.records if r.probe.kind == "largest_expense")
    match = re.search(r" at (.+?)\.\s*$", largest.rendered)
    assert match, f"unexpected rendered shape: {largest.rendered!r}"
    payee = match.group(1)
    assert not payee.isdigit(), f"payee rendered as a bare entity key: {payee!r}"
    assert any(c.isalpha() for c in payee), (
        f"payee rendered without any letters: {payee!r}"
    )


# --- no seeded identifier crosses the model boundary ------------------------ #


def test_no_seeded_identifier_crosses_the_boundary(tmp_path):
    """Every seeded identifier is checked, explicitly, against the concatenation
    of everything that crossed the model boundary."""
    manifest = load_manifest()
    seeds = seeds_from_manifest(manifest)
    with open_store(tmp_path / "s.db", KEY) as store:
        run = run_conversation(store)

    haystack = run.boundary_text.lower()
    for identifier in seeds:
        assert identifier.lower() not in haystack, (
            f"seeded identifier {identifier!r} crossed the model boundary"
        )
    # The boundary is not trivially empty: real work crossed it (alias tokens).
    assert "PAYEE-" in run.boundary_text
    assert "ACCT-" in run.boundary_text


def test_boundary_excludes_locally_rendered_real_values(tmp_path):
    """Sanity: the locally-rendered (post-lens) text is NOT part of what the
    harness treats as boundary-crossing — un-redaction lives on-machine only."""
    with open_store(tmp_path / "s.db", KEY) as store:
        run = run_conversation(store)
        # At least one turn rendered a real institution locally...
        assert any("Bank of Nowhere" in rec.rendered for rec in run.records)
    # ...yet no real value is in the boundary text.
    assert "Bank of Nowhere" not in run.boundary_text


# --- mutation tests: the gate must fail on a deliberately-seeded leak -------- #


def test_fails_on_model_reply_that_leaks_a_real_institution(tmp_path):
    """Mutation: a model that echoes a real institution name into its reply. The
    reply crosses the boundary, so the leak-check must catch it."""

    def leaky_model(probe, ctx, sent):
        return ModelReply(
            text="Your Bank of Nowhere account looks healthy this month.",
            value=0.0,
        )

    with open_store(tmp_path / "s.db", KEY) as store:
        report = falsify(store, model=leaky_model)

    assert not report.ok
    assert any("Bank of Nowhere" in leak.match for leak in report.leaks)


def test_fails_on_projection_that_carries_a_raw_identifier(tmp_path):
    """Mutation: an S1.4-style regression where the alias-space projection carries
    a raw institution name instead of a token. Its context JSON crosses the
    boundary, so the leak-check must catch it."""
    with open_store(tmp_path / "s.db", KEY) as store:
        real_client = build_client(store)
        # Taint one record: raw institution where a token belongs.
        tainted = [
            AliasRecord(
                account="ACCT-1",
                institution="Bank of Nowhere",  # <- leak
                payee="PAYEE-1",
                date="2026-05-05",
                amount=-52.18,
            )
        ]
        tainted_client = SanitizedContextClient(
            projection=AliasSpaceProjection(records=tainted),
            redactor=real_client._redactor,
        )
        report = falsify(store, client=tainted_client)

    assert not report.ok
    assert any("Bank of Nowhere" in leak.match for leak in report.leaks)


def test_fails_on_wrong_arithmetic(tmp_path):
    """Mutation: a model that answers with the wrong figure. No identifier leaks,
    but the usefulness half of the gate must go red."""

    def wrong_model(probe, ctx, sent):
        return ModelReply(text="It was +0.00 exactly.", value=0.0, count=0)

    with open_store(tmp_path / "s.db", KEY) as store:
        report = falsify(store, model=wrong_model)

    assert report.leaks == [], "the wrong-arithmetic mutation must not leak"
    assert report.answer_failures, "wrong answers must fail the usefulness gate"
    assert not report.ok


# --- ground-truth oracle is independent of the pipeline --------------------- #


def test_canonical_statements_avoid_double_counting(tmp_path):
    """The oracle reads one format per (account, month) so duplicated CSV/OFX
    fixtures are not double-counted — a precondition for correct answers."""
    statements = canonical_statements()
    keys = [(s.account_type, s.month) for s in statements]
    assert len(keys) == len(set(keys)), "each (account, month) appears once"
    assert {s.format for s in statements} == {"csv"}


# --- the harness is runnable standalone (and in CI) ------------------------- #

_HARNESS = Path(__file__).resolve().parent.parent / "redactor" / "falsifier.py"


def test_harness_runs_as_a_module_and_exits_clean():
    """`python -m redactor.falsifier` runs the gate over the fixtures and exits 0
    on the real pipeline."""
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.falsifier"],
        cwd=_HARNESS.parent.parent,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.lower()
    assert "no leak" in out or "leak-check" in out
    assert "useful" in out or "pass" in out
