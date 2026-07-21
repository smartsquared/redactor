"""Round-trip demo of the sanitized-context API (story S2.3) over the fixtures.

Runs the whole contract loop a consumer (sakuma-finance) performs, end to end,
against the bundled synthetic bookkeeping fixtures — with a *stubbed* model so
the demo is self-contained and offline:

  1. ingest the fixtures into a throwaway encrypted store;
  2. request an alias-space **context window** (transactions + balances) for a
     month — everything that would go into model context is redacted;
  3. **send** a user turn through the proxy — outbound forward-redaction turns
     real names into tokens before anything leaves the machine;
  4. a stub "model" answers *in alias-space* (it only ever saw tokens);
  5. **receive** that reply — still alias-space, deliberately not un-redacted;
  6. render it to real values **only** at the local lens boundary (S2.2).

The point the transcript makes visually: every string that crosses the model
boundary (context JSON, the sent turn, the received reply) is alias-space; real
values appear *only* in step 6, produced locally by the lens.

Run it::

    python -m examples.sanitized_context_roundtrip
    # or: python examples/sanitized_context_roundtrip.py

Nothing here writes real data anywhere: the store is a temp file removed on exit,
and no real value is ever serialized — only rendered to stdout, the local
surface.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from redactor.alias import find_aliases
from redactor.context_api import SanitizedContextClient
from redactor.fixtures import load_statements
from redactor.ingest import ingest_statements
from redactor.lens import lens, resolver_from_table
from redactor.outbound import OutboundRedactor
from redactor.resolve import PayeeResolver
from redactor.store import open_store

# A throwaway passphrase for the throwaway demo store. A real deployment sources
# the key from the environment / OS keychain — never a literal (see the store's
# ADR). Fine here: the store holds only synthetic fixtures and is deleted on exit.
DEMO_KEY = "demo-only-not-a-real-key"

# The month the demo scopes its context and question to.
DEMO_START, DEMO_END = "2026-05-01", "2026-05-31"

# The user turn the demo sends through the proxy. It names a merchant (a PAYEE)
# and the bank (an INST), so the outbound step visibly redacts both.
DEMO_QUESTION = "Did I hit Whole Foods in May, and how is my Bank of Nowhere checking?"


def build_client(store) -> tuple[SanitizedContextClient, PayeeResolver]:
    """Ingest the bundled fixtures and build the client + its resolver.

    Returns the resolver too so the caller can reach the store's mapping table
    for the local lens step — the one place un-redaction is allowed.
    """
    resolver = PayeeResolver()
    projection = ingest_statements(
        store, load_statements(), resolve_payee=resolver.resolve_display
    )
    redactor = OutboundRedactor.from_store(store, resolver)
    return SanitizedContextClient(projection=projection, redactor=redactor), resolver


def run(store) -> dict:
    """Execute the round trip against *store*, returning the artifacts produced.

    The returned dict is what the automated test asserts on; :func:`main` narrates
    the same artifacts to stdout.
    """
    client, _ = build_client(store)

    # 2. alias-space context out — this is what would be shipped into the model.
    ctx = client.context(start=DEMO_START, end=DEMO_END)

    # 3. user turn -> outbound redaction -> safe to send.
    sent = client.send(DEMO_QUESTION)
    checking = next((b for b in ctx.balances if b.account == "ACCT-1"), ctx.balances[0])

    # 4. the stub model answers in alias-space, over the account/institution
    #    tokens from the context. It never saw a real name — only tokens. (The
    #    reply un-redacts cleanly because ACCT/INST tokens carry a real binding;
    #    what a PAYEE token renders back to is set by the ingest seam, not this
    #    API, so the demo keeps the rendered reply to account/institution.)
    model_reply = (
        f"Your {checking.institution} checking ({checking.account}) "
        f"netted {checking.net:+.2f} {checking.currency} in May."
    )

    # 5. receive the reply — stays in alias-space, not un-redacted here.
    received = client.receive(model_reply)

    # 6. render to the human — ONLY here, at the local lens (S2.2).
    rendered = lens(received.text, resolver_from_table(store.mapping)).text

    return {
        "context": ctx,
        "sent": sent,
        "checking": checking,
        "received": received,
        "rendered": rendered,
    }


def _rule(title: str) -> str:
    return f"\n{'─' * 4} {title} {'─' * (68 - len(title))}"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "demo-store.db"
        with open_store(store_path, DEMO_KEY) as store:
            out = run(store)

        ctx = out["context"]
        sent = out["sent"]
        received = out["received"]

        print("redactor — sanitized-context API round trip (S2.3)")
        print("Stubbed model, synthetic fixtures. Everything above the lens is alias-space.")

        print(_rule("1-2. alias-space context out (transactions + balances)"))
        print(f"window: {ctx.start} .. {ctx.end}  (version {ctx.version})")
        print(f"transactions: {len(ctx.records)}   accounts: {len(ctx.balances)}")
        for bal in ctx.balances:
            print(
                f"  {bal.account} @ {bal.institution}: "
                f"net {bal.net:+.2f} {bal.currency} over {bal.txn_count} txns"
            )
        print("  (identifying fields are tokens; amounts/dates stay real)")

        print(_rule("3. user turn -> outbound redaction (safe to send)"))
        print(f"  in : {DEMO_QUESTION}")
        print(f"  out: {sent.text}")
        if sent.warnings:
            print(f"  warnings: {[w.surface for w in sent.warnings]}")

        print(_rule("4-5. model reply, in alias-space (received, not un-redacted)"))
        print(f"  {received.text}")
        leaked = "LEAK" if find_aliases(received.text) == [] else "alias-space ✓"
        print(f"  crosses the model boundary as: {leaked}")

        print(_rule("6. local lens render (S2.2) — real values, on-machine only"))
        print(f"  {out['rendered']}")

        print("\nRound trip complete: real names appeared only after the lens.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
