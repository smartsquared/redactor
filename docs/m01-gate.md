# m01 milestone gate — the leak-check falsifier harness

**Status:** the ratified closing condition for milestone **m01** (story S2.4).
m01 closes when this gate is green in CI.

Reference implementation: [`redactor/falsifier.py`](../redactor/falsifier.py).
CI gate: [`tests/test_falsifier.py`](../tests/test_falsifier.py) runs the harness
under pytest in the `build-and-test` check. A named, standalone gate step
(`python -m redactor.falsifier`) is a proposed operator follow-up — see the PR.
Run it locally: `python -m redactor.falsifier`.

## 1. What it proves

m01's goal is one sentence ([m01-stories.md](m01-stories.md)):

> a consumer can hold a **useful** bookkeeping conversation through the gateway
> with **no identifiers leaking**, verified against fixtures.

The harness is the falsifier for that sentence. It scripts a multi-turn
bookkeeping conversation over the synthetic fixtures and drives it through the
*entire* pipeline — ingest (S1.1–S1.4) → alias-space context (S2.3) → outbound
forward-redaction (S2.1) → a stub model → inbound receive → the local lens
(S2.2) — then makes two assertions:

1. **No leak.** Everything that crosses the model boundary is scanned with the
   S1.4 leak-lint ([`redactor/leaklint.py`](../redactor/leaklint.py)). No seeded
   identifier — a raw memo variant, an account id, an institution name, a routing
   number, a display mask — and no real-looking number (ABA/Luhn/SSN via the
   fake-ness checks) may appear.
2. **Useful.** Each answer the stub model computes is checked against an oracle
   computed **independently** from the raw fixture files.

If either fails, the gate is red and m01 is not done.

## 2. What "crosses the model boundary"

The boundary set is exactly the three things a real deployment would send to or
receive from an off-machine model:

- the **alias-space context JSON** shipped as model input (transactions +
  per-account balances, scoped to a date range / account);
- every **forward-redacted user turn** (`client.send(...)`);
- every **alias-space model reply** (`client.receive(...)`).

Deliberately **excluded**: the locally-rendered, post-lens text. Un-redaction is
the on-machine render boundary and never crosses the wire (CLAUDE.md — "un-redaction
only at the local render boundary"). The harness renders a real institution and
account id *locally* on some turns precisely to demonstrate the contrast: those
real values appear only after the lens, never in the boundary set.

## 3. Why the usefulness check is honest

The stub model is offline and deliberately dumb: it only ever sees alias tokens
plus real amounts/dates, and it answers by arithmetic over the context window.
That is the guarantee under test made concrete — a model that never saw a real
name can still answer bookkeeping questions. A merchant reaches the model *only*
as the `PAYEE` token that outbound redaction substituted into the user turn.

The oracle grounds correctness in real fixture facts, not in the pipeline's own
output:

- it reads a **single canonical fixture view** (one format per account-month),
  so the duplicated CSV/OFX fixtures are not double-counted and the figures are
  genuinely correct (not merely self-consistent);
- account aggregates (net, count, largest expense) are computed straight from the
  raw amounts/dates;
- payee spend is grouped by the **manifest's ground-truth variant list**, not by
  the resolver — and the harness also asserts the *count* of attributed
  transactions, so an incomplete payee grouping surfaces as a wrong answer rather
  than passing silently. Scripted merchants are chosen so the S1.3 resolver groups
  their variants completely over the window.

## 4. The gate can fail (mutation-tested)

A gate that cannot fail is not a gate. The stub model and the client are
injectable, and `tests/test_falsifier.py` seeds three deliberate regressions,
each of which must flip the gate red:

| Mutation | What it models | Which half fails |
| --- | --- | --- |
| model reply echoes a real institution | a model that leaks a real name | leak-check |
| projection carries a raw institution | an S1.4-style projection regression | leak-check |
| model returns the wrong figure | broken bookkeeping arithmetic | usefulness |

## 5. Running it

```console
$ python -m redactor.falsifier
redactor — leak-check falsifier harness (S2.4, m01 gate)
...
leak-check: no leak — nothing seeded crossed the model boundary
usefulness: useful — every answer correct from fixture data

GATE: PASS ✓
```

Exit status is `0` iff the gate is green, so it doubles as a CI check. The
harness already runs in CI through `tests/test_falsifier.py`; running it as its
own named step is an optional operator follow-up (it needs a workflow-file edit,
which is operator-authored).
