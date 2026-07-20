"""Leak-check falsifier harness — the m01 milestone gate (story S2.4).

This is the falsifier the whole of m01 was built to survive. It scripts a
multi-turn bookkeeping conversation over the synthetic fixtures and drives it
through the *entire* pipeline, then tries to falsify the product's one promise:

    a consumer can hold a useful bookkeeping conversation through the gateway
    with **no identifiers leaking** (docs/m01-stories.md).

Two assertions, mirroring the S2.4 acceptance criteria:

  1. **No leak.** Everything that crosses the model boundary — the alias-space
     context JSON shipped as model input, every forward-redacted user turn, and
     every alias-space model reply — is scanned with the S1.4 leak-lint
     (:mod:`redactor.leaklint`). No seeded identifier (raw memo variant, account
     id, institution, routing number, display mask) and no real-looking number
     may appear. The *locally-rendered* reply (post-lens, real values) is
     deliberately **excluded** from the boundary set: un-redaction is the local
     render boundary and never crosses the wire (CLAUDE.md).
  2. **Useful.** Each answer the stub model computes *purely from the alias-space
     context* is checked against an oracle computed independently from the raw
     fixture files. Amounts and dates are real on both sides (contract §1.1), so
     the arithmetic grounds in genuine fixture facts, not in the pipeline's own
     output.

The stub model is deliberately dumb and offline: it only ever sees alias tokens
plus real amounts/dates, and it answers by arithmetic over the context window —
which is exactly the guarantee under test (a model that never saw a real name
can still answer bookkeeping questions). Because the model is injectable, the
harness is *mutation-testable*: feed it a model that leaks a real value, or that
does the arithmetic wrong, and the corresponding half of the gate goes red.

Crown-jewel rule (CLAUDE.md) still holds here: this module reads real values
only where the pipeline already does (locally, to build the client and to render
at the lens), serializes none of them, and ships nothing off-machine.
"""
from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from redactor.alias import find_aliases
from redactor.context_api import ContextWindow, SanitizedContextClient
from redactor.fixtures import Statement, Transaction, load_manifest, load_statements
from redactor.ingest import ingest_statements
from redactor.leaklint import Leak, scan_projection, seeds_from_manifest
from redactor.lens import lens, resolver_from_table
from redactor.outbound import OutboundRedactor
from redactor.resolve import PayeeResolver
from redactor.store import open_store

# A throwaway passphrase for the standalone run's throwaway store. A real
# deployment sources the key from the OS keychain (docs/adr/0001). Fine here: the
# store holds only synthetic fixtures and is deleted on exit.
_HARNESS_KEY = "falsifier-harness-throwaway-key"


# --- the scripted conversation ---------------------------------------------- #


@dataclass(frozen=True)
class Probe:
    """One scripted user turn plus how to score its answer.

    ``kind`` selects the stub model's intent and the oracle's ground-truth
    computation. ``account_type`` scopes the alias-space context request (a
    consumer legitimately says "let's talk about my checking account"); ``None``
    means all accounts. ``merchant`` names the manifest payee for ``payee_spend``
    probes. ``start``/``end`` are inclusive ISO date bounds.
    """

    name: str
    kind: str                       # payee_spend | account_net | account_count | largest_expense
    question: str
    account_type: str | None = None  # "checking" | "credit" | None (all)
    merchant: str | None = None      # canonical manifest payee, for payee_spend
    start: str | None = None
    end: str | None = None
    window_label: str = "the window"


# A plausible multi-turn bookkeeping conversation. Every figure it asks about is
# checkable against the raw fixtures. Merchants are chosen so the S1.3 resolver
# groups their memo variants completely over the window (the oracle asserts the
# attribution count too, so an incomplete grouping would surface as a wrong
# answer rather than pass silently).
DEFAULT_SCRIPT: list[Probe] = [
    Probe(
        name="costco-may-spend",
        kind="payee_spend",
        question="How much did I spend at Costco in May?",
        merchant="Costco",
        start="2026-05-01",
        end="2026-05-31",
        window_label="May",
    ),
    Probe(
        name="checking-may-net",
        kind="account_net",
        question="What was the net on my checking account in May?",
        account_type="checking",
        start="2026-05-01",
        end="2026-05-31",
        window_label="May",
    ),
    Probe(
        name="credit-may-count",
        kind="account_count",
        question="How many purchases were on my credit card in May?",
        account_type="credit",
        start="2026-05-01",
        end="2026-05-31",
        window_label="May",
    ),
    Probe(
        name="largest-may-expense",
        kind="largest_expense",
        question="What was my single largest expense in May?",
        start="2026-05-01",
        end="2026-05-31",
        window_label="May",
    ),
    Probe(
        name="credit-may-net",
        kind="account_net",
        question="And what was the net on the credit card in May?",
        account_type="credit",
        start="2026-05-01",
        end="2026-05-31",
        window_label="May",
    ),
]


# --- stub model (alias-space in, alias-space out) --------------------------- #


@dataclass(frozen=True)
class ModelReply:
    """A stub model's alias-space reply.

    ``text`` is what crosses the boundary back to the consumer (tokens + real
    amounts, never a real identifier). ``value``/``count`` are the figures the
    model computed from the context, surfaced so the harness can score them;
    ``tokens`` are the alias tokens the model reasoned over.
    """

    text: str
    value: float
    count: int = 0
    tokens: tuple[str, ...] = ()


# A model is a callable (probe, context, redacted_user_text) -> ModelReply. It is
# injectable so mutation tests can supply a leaky or wrong one.
Model = Callable[[Probe, ContextWindow, str], ModelReply]


def _payee_tokens(sent_text: str) -> tuple[str, ...]:
    """The PAYEE tokens the outbound redactor put into a user turn."""
    return tuple(m.canonical for m in find_aliases(sent_text) if m.cls == "PAYEE")


def default_model(probe: Probe, ctx: ContextWindow, sent_text: str) -> ModelReply:
    """Answer a probe by arithmetic over the alias-space context only.

    The model never sees a real name: a merchant reaches it as the PAYEE token
    the outbound redactor substituted into the user turn, and it reasons over the
    matching context records. This is the guarantee under test made concrete.
    """
    if probe.kind == "payee_spend":
        tokens = _payee_tokens(sent_text)
        recs = [r for r in ctx.records if r.payee in tokens]
        value = round(sum(r.amount for r in recs), 2)
        who = tokens[0] if tokens else "that merchant"
        text = (
            f"Over {probe.window_label} you spent {value:+.2f} at {who} "
            f"across {len(recs)} transactions."
        )
        return ModelReply(text=text, value=value, count=len(recs), tokens=tokens)

    if probe.kind == "account_net":
        value = round(sum(r.amount for r in ctx.records), 2)
        # When the consumer scoped a single account, name it — in alias-space, by
        # its INST/ACCT tokens. Those render back to real values only at the local
        # lens, never across the boundary; that contrast is the whole demo.
        where, tokens = "", ()
        if len(ctx.balances) == 1:
            b = ctx.balances[0]
            where = f" on {b.institution} {b.account}"
            tokens = (b.institution, b.account)
        text = (
            f"The net{where} over {probe.window_label} was {value:+.2f} "
            f"across {len(ctx.records)} transactions."
        )
        return ModelReply(text=text, value=value, count=len(ctx.records), tokens=tokens)

    if probe.kind == "account_count":
        n = len(ctx.records)
        return ModelReply(
            text=f"There were {n} transactions over {probe.window_label}.",
            value=float(n),
            count=n,
        )

    if probe.kind == "largest_expense":
        if not ctx.records:
            return ModelReply(text="There were no transactions.", value=0.0)
        worst = min(ctx.records, key=lambda r: r.amount)
        value = round(worst.amount, 2)
        text = (
            f"The largest single expense over {probe.window_label} was "
            f"{value:+.2f} at {worst.payee}."
        )
        return ModelReply(text=text, value=value, count=len(ctx.records), tokens=(worst.payee,))

    raise ValueError(f"unknown probe kind: {probe.kind!r}")  # pragma: no cover


# --- oracle: ground truth from the raw fixtures ----------------------------- #


def canonical_statements(directory: Path | None = None) -> list[Statement]:
    """One statement per (account, month), so duplicated CSV/OFX fixtures are not
    double-counted. CSV sorts before OFX, so the CSV view wins deterministically.

    The pipeline is content to ingest both formats (the S2.3 tests only assert
    self-consistent figures); the falsifier asserts *absolute* figures, so it
    reads a single canonical view to make the answers genuinely correct.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Statement] = []
    for st in load_statements(directory):
        key = (st.account_type, st.month)
        if key in seen:
            continue
        seen.add(key)
        out.append(st)
    return out


def _in_window(date: str, start: str | None, end: str | None) -> bool:
    return (start is None or date >= start) and (end is None or date <= end)


def _scope_txns(statements: Iterable[Statement], probe: Probe) -> list[Transaction]:
    """Raw transactions for a probe's account scope + date window."""
    out: list[Transaction] = []
    for st in statements:
        if probe.account_type is not None and st.account_type != probe.account_type:
            continue
        out.extend(t for t in st.transactions if _in_window(t.date, probe.start, probe.end))
    return out


def oracle(probe: Probe, statements: list[Statement], manifest: dict) -> tuple[float, int]:
    """The correct answer for a probe, computed straight from the raw fixtures.

    Returns ``(value, count)``. Independent of the alias pipeline: it groups
    payees by the manifest's ground-truth variant list, not by the resolver, so a
    matching model answer is real evidence the pipeline preserved both the
    amounts and the payee attribution.
    """
    txns = _scope_txns(statements, probe)

    if probe.kind == "account_net":
        return round(sum(t.amount for t in txns), 2), len(txns)
    if probe.kind == "account_count":
        return float(len(txns)), len(txns)
    if probe.kind == "largest_expense":
        worst = min((t.amount for t in txns), default=0.0)
        return round(worst, 2), len(txns)
    if probe.kind == "payee_spend":
        variants = set(manifest["payees"][probe.merchant])
        matched = [t for t in txns if t.description in variants]
        return round(sum(t.amount for t in matched), 2), len(matched)

    raise ValueError(f"unknown probe kind: {probe.kind!r}")  # pragma: no cover


def _score(probe: Probe, reply: ModelReply, oracle_value: float, oracle_count: int) -> list[str]:
    """Return the reasons this answer is wrong (empty list == correct)."""
    fails: list[str] = []
    if abs(reply.value - oracle_value) > 0.005:
        fails.append(
            f"{probe.name}: model answered {reply.value:+.2f}, fixtures say {oracle_value:+.2f}"
        )
    # Attribution fidelity: the model must have reasoned over exactly the right
    # transactions (this is what makes an incomplete payee grouping show up).
    if probe.kind in ("payee_spend", "account_count") and reply.count != oracle_count:
        fails.append(
            f"{probe.name}: model reasoned over {reply.count} transactions, "
            f"fixtures have {oracle_count}"
        )
    # A payee question must actually have recognized the merchant.
    if probe.kind == "payee_spend" and not reply.tokens:
        fails.append(f"{probe.name}: merchant was not recognized (no PAYEE token)")
    # The computed figure must actually reach the human in the reply text.
    money = f"{reply.value:+.2f}"
    whole = str(int(reply.value)) if float(reply.value).is_integer() else None
    if money not in reply.text and (whole is None or whole not in reply.text):
        fails.append(f"{probe.name}: the computed answer is not present in the reply text")
    return fails


# --- running the conversation ----------------------------------------------- #


@dataclass(frozen=True)
class TurnRecord:
    """One conversation turn and everything the harness needs to score it."""

    probe: Probe
    sent: str            # forward-redacted user turn (crosses the boundary)
    reply: ModelReply    # alias-space model reply (crosses the boundary)
    received: str        # the reply as received, still alias-space
    rendered: str        # locally rendered at the lens (real values, on-machine)
    context_json: str    # the alias-space context shipped as model input
    ok: bool
    detail: str = ""

    @property
    def boundary_parts(self) -> tuple[str, ...]:
        """The strings from this turn that crossed the model boundary.

        The rendered (post-lens) text is intentionally absent — un-redaction is
        the local render boundary and never leaves the machine.
        """
        return (self.context_json, self.sent, self.received)


@dataclass(frozen=True)
class ConversationRun:
    """The full transcript plus the concatenated boundary-crossing text."""

    records: list[TurnRecord] = field(default_factory=list)

    @property
    def boundary_text(self) -> str:
        """Everything that crossed the model boundary, concatenated for scanning."""
        parts: list[str] = []
        for rec in self.records:
            parts.extend(rec.boundary_parts)
        return "\n".join(parts)


def build_client(store, *, statements: list[Statement] | None = None) -> SanitizedContextClient:
    """Ingest the fixtures into *store* and build the sanitized-context client.

    This is the wiring a consumer performs once at startup: the S1.3 resolver
    feeds the S1.4 ingest seam, and the resulting projection + outbound redactor
    build the S2.3 client. Uses the canonical single-format fixture view so the
    conversation's figures are genuinely correct.
    """
    statements = statements if statements is not None else canonical_statements()
    resolver = PayeeResolver()
    projection = ingest_statements(
        store, statements, resolve_payee=lambda m: str(resolver.resolve(m))
    )
    redactor = OutboundRedactor.from_store(store, resolver)
    return SanitizedContextClient(projection=projection, redactor=redactor)


def _account_scope(store, probe: Probe, manifest: dict) -> list[str] | None:
    """Resolve a probe's account-type scope to alias tokens, locally.

    The account↔token mapping is looked up in the local table (the harness runs
    where the mapping lives); the account id itself never enters the boundary.
    """
    if probe.account_type is None:
        return None
    account_id = manifest["accounts"][probe.account_type]["account_id"]
    token = store.mapping.resolve_token("ACCT", account_id)
    return [token] if token is not None else []


def run_conversation(
    store,
    *,
    script: list[Probe] = DEFAULT_SCRIPT,
    client: SanitizedContextClient | None = None,
    model: Model = default_model,
    statements: list[Statement] | None = None,
    manifest: dict | None = None,
) -> ConversationRun:
    """Drive the scripted conversation through the full pipeline.

    For each probe: request the alias-space context (scoped), forward-redact the
    user turn, let the (stub) model answer over the context, receive the
    alias-space reply, and render it locally at the lens. Scores each answer
    against the oracle and records every boundary-crossing string.
    """
    manifest = manifest if manifest is not None else load_manifest()
    statements = statements if statements is not None else canonical_statements()
    client = client if client is not None else build_client(store, statements=statements)
    resolve = resolver_from_table(store.mapping)

    records: list[TurnRecord] = []
    for probe in script:
        accounts = _account_scope(store, probe, manifest)
        ctx = client.context(start=probe.start, end=probe.end, accounts=accounts)

        sent = client.send(probe.question)
        reply = model(probe, ctx, sent.text)
        received = client.receive(reply.text)
        rendered = lens(received.text, resolve).text

        oracle_value, oracle_count = oracle(probe, statements, manifest)
        fails = _score(probe, reply, oracle_value, oracle_count)

        records.append(
            TurnRecord(
                probe=probe,
                sent=sent.text,
                reply=reply,
                received=received.text,
                rendered=rendered,
                context_json=ctx.to_json(),
                ok=not fails,
                detail="; ".join(fails),
            )
        )
    return ConversationRun(records=records)


# --- the gate --------------------------------------------------------------- #


@dataclass(frozen=True)
class FalsifierReport:
    """The verdict: what leaked, and which answers were wrong."""

    leaks: list[Leak]
    answer_failures: list[str]

    @property
    def ok(self) -> bool:
        """The gate is green iff nothing leaked and every answer was correct."""
        return not self.leaks and not self.answer_failures


def falsify(
    store,
    *,
    script: list[Probe] = DEFAULT_SCRIPT,
    client: SanitizedContextClient | None = None,
    model: Model = default_model,
    statements: list[Statement] | None = None,
    manifest: dict | None = None,
    seeds: dict[str, str] | None = None,
) -> FalsifierReport:
    """Run the scripted conversation and return the leak + usefulness verdict."""
    manifest = manifest if manifest is not None else load_manifest()
    run = run_conversation(
        store,
        script=script,
        client=client,
        model=model,
        statements=statements,
        manifest=manifest,
    )
    seeds = seeds if seeds is not None else seeds_from_manifest(manifest)
    leaks = scan_projection(run.boundary_text, seeds)
    answer_failures = [rec.detail for rec in run.records if not rec.ok]
    return FalsifierReport(leaks=leaks, answer_failures=answer_failures)


# --- standalone entry point ------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Run the gate over the bundled fixtures in a throwaway store; exit 0 iff green.

    Runnable as ``python -m redactor.falsifier``. The store is a temp file
    removed on exit and holds only synthetic fixtures; no real value is ever
    written or shipped — figures are rendered to stdout, the local surface, only.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with open_store(Path(tmp) / "falsifier.db", _HARNESS_KEY) as store:
            run = run_conversation(store)
            report = falsify(store)

    print("redactor — leak-check falsifier harness (S2.4, m01 gate)")
    print(f"scripted {len(run.records)} turns over synthetic fixtures\n")
    for rec in run.records:
        mark = "ok " if rec.ok else "!! "
        print(f"  {mark}{rec.probe.name:<20} q: {rec.probe.question}")
        print(f"       sent (boundary):     {rec.sent}")
        print(f"       reply (boundary):    {rec.reply.text}")
        print(f"       rendered (local):    {rec.rendered}")
        if not rec.ok:
            print(f"       WRONG: {rec.detail}")

    leak_line = "no leak — nothing seeded crossed the model boundary"
    if report.leaks:
        leak_line = f"LEAK — {len(report.leaks)} seeded identifier(s) crossed the boundary:"
    print(f"\nleak-check: {leak_line}")
    for leak in report.leaks:
        print(f"  - {leak.kind}: {leak.match!r} ({leak.detail})")

    use_line = "useful — every answer correct from fixture data"
    if report.answer_failures:
        use_line = f"NOT useful — {len(report.answer_failures)} wrong answer(s)"
    print(f"usefulness: {use_line}")

    print("\nGATE:", "PASS ✓" if report.ok else "FAIL ✗")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
