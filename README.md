# redactor

A privacy gateway that makes conversations about sensitive data safe with any chatbot — reversible alias redaction with continuity across months, at personal scale.

## What this is

You want to talk to an LLM about your real finances (or, later, medical or legal documents), but the conversation shouldn't carry your account numbers, payee names, or identity. redactor sits between you and the model as a **bidirectional proxy**: it ingests your real data into a local encrypted store, replaces identifying strings with stable pseudonyms (`ACCT-1`, `PAYEE-7`) before anything reaches a model, and substitutes the real names back when rendering responses to you. The mapping table that makes redaction reversible never leaves your machine. Detection wraps [Microsoft Presidio](https://github.com/microsoft/presidio); the product is the alias continuity and the local-first deployment, which the enterprise redaction market doesn't offer.

This repo is a **product of the sakuma process** — it was built by the sakuma pipeline, but it is not an ecosystem member. It has users, not consumers; a brief, not a substrate role.

## Provenance

| Input | Where |
|---|---|
| Brief / design doc | [docs/brief.md](docs/brief.md) (from sakuma whiteboard capture, 2026-07-19) |
| Wave plan | n/a — not yet planned |
| Driving loop | n/a — pre-loop bootstrap |

## v0 scope

Nothing shipped yet. v0 target is contract-shaped, not feature-shaped: the first consumer (`sakuma-finance`) can hold a useful conversation through the gateway with no identifiers leaking, verified against fixtures. See [docs/brief.md](docs/brief.md).

## Store lifecycle (`redactor init` / `redactor status`)

`redactor init` creates the local encrypted store (SQLCipher, per
[ADR-0001](docs/adr/0001-encryption-at-rest.md)) and, by default, generates a
high-entropy key custodied in the **OS keychain** (macOS Keychain first) — no
passphrase to remember, and none in your shell history or environment. The key
is never displayed or logged.

```console
$ redactor init --store ~/.redactor/store.db
redactor init: created encrypted store at ~/.redactor/store.db; key custodied in the OS keychain.

$ redactor status --store ~/.redactor/store.db
store:  ~/.redactor/store.db
schema: v2
key:    available (keychain)
state:  ready
```

`redactor status` reports the store location, schema version, and whether a key
is available to open it — **without reading any data**. Key resolution across
every subcommand is `--key`, then `$REDACTOR_KEY`, then the keychain; the first
two are CI/test overrides and are never written to custody. Wrong or missing
keys fail with an actionable message and a non-zero exit.

## The local lens (`redactor lens`)

The inbound half of the proxy. A model answers in alias-space (`PAYEE-7`,
`ACCT-1`); the lens substitutes those tokens back to their real values so you
can read the answer in plain language. It is **mangle-tolerant** — it recovers
the token from the manglings models emit (`payee-7`, `PAYEE 7`, `PAYEE-7's`) —
but **never false-positive**: non-alias prose and tokens not in your table pass
through untouched.

```console
$ echo "Your payee-7's charge hit ACCT-1." | redactor lens --store ~/.redactor/store.db
Your Bank of Nowhere Grocery charge hit NOWHERE-CHK-000199.
```

Un-redaction happens **only where the mapping table lives**: the lens opens the
local encrypted store and refuses (non-zero exit, nothing rendered) if no
decryptable table is present. The passphrase comes from `--key` or the
`REDACTOR_KEY` environment variable. Real values are written to stdout (the
local render surface) only — never to any artifact that leaves the machine.

## Ingesting real statements (`redactor ingest`)

The outbound half of getting started: pull a real month of bank exports into the
local store. The loaders are **tolerant of real-world mess** — header-name
variants, a UTF-8 BOM or Windows codepage, `MM/DD/YYYY` dates, split
debit/credit columns, `$`/comma/accounting-paren amounts, and both OFX dialects
(1.x SGML and 2.x XML). When the header heuristics can't place a column, name it
explicitly with `--map`:

```console
$ redactor ingest checking.csv statement.ofx --store ~/.redactor/store.db
checking.csv: 31 rows, 12 new payees, 14 aliases issued, lint: clean
statement.ofx: 28 rows, 3 new payees, 3 aliases issued, lint: clean

$ redactor ingest weird.csv --store ~/.redactor/store.db --map date=Posted,amount=Amt,description=Details
```

Raw fields land **only** in the encrypted store; the per-file summary is
**alias-space only** (counts and a lint verdict — never a memo or account id).
Parse failures are reported **without echoing the offending cell** — you get the
row number and column name, safe to copy-paste; the raw value appears only when
you pass `--show-raw` on your own machine:

```console
$ redactor ingest broken.csv --store ~/.redactor/store.db
redactor ingest: FAILED broken.csv row 14 field 'amount': amount is not a number
```

A bad file fails on its own and never aborts the batch — the good files still
ingest. Re-ingesting is safe: aliases are stable, so a second pass issues zero
new ones.

When a memo variant groups onto an existing payee by a *fuzzy* match rather than
a shared token, ingest flags that grouping for review (alias-space — a token and
a confidence number, never a memo):

```console
review: 1 low-confidence payee grouping(s) flagged — run `redactor payees --review` to confirm:
  PAYEE-3: confidence 0.90
```

As a coarser backstop, ingest also runs a payee-per-row sanity check. If a large
statement resolves to implausibly few payees — the signature of an
entity-resolution collapse — it prints a loud warning (alias-space: counts and a
ratio, never a memo):

```console
redactor: WARNING — payee collapse suspected: only 1 distinct payee(s) for 9717 rows (ratio 0.0001). Entity resolution may have over-merged; run `redactor payees --review` before trusting this ingest.
```

## Reviewing payee groupings (`redactor payees`)

Real memo garbage produces some wrong groupings and unlovely display labels.
`redactor payees --review` lists the resolved payee entities — variant count,
confidence, and the display label. Like `lens`, it **reveals real values**, so it
runs only on the machine that holds the mapping table:

```console
$ redactor payees --review --store ~/.redactor/store.db
redactor payees: 16 payee entit(y/ies):
  PAYEE-3   variants=3  confidence=0.90  [REVIEW]  Wholefds
  PAYEE-10  variants=1  confidence=1.00            Wf
  ...
```

Fix a grouping or a label with `--merge` / `--split` / `--rename`. Alias
stability is preserved — a merge **aliases the loser to the winner and never
renumbers**, so records already emitted under either token keep un-redacting
correctly — and every mutation is journaled in the store:

```console
$ redactor payees --merge PAYEE-3 PAYEE-10 --store ~/.redactor/store.db
redactor payees: merged PAYEE-10 into PAYEE-3 (aliases preserved).

$ redactor payees --rename PAYEE-3 "Whole Foods Market" --store ~/.redactor/store.db
redactor payees: renamed PAYEE-3.

$ redactor payees --split PAYEE-10 --store ~/.redactor/store.db   # undo the merge
redactor payees: split PAYEE-10 back into its own entity.
```

Mutation output is **alias-space only** (it names tokens, never the memo or the
new label). After a merge, both tokens lens to the winner's real value.

## The sanitized-context API

The contract the first consumer (`sakuma-finance`) speaks to hold a conversation
through the gateway without ever touching real data. A consumer stays **entirely
in alias-space**: it requests redacted bookkeeping context (transactions,
balances, date ranges — identifying fields as tokens, amounts and dates real) and
sends conversation turns through the outbound proxy, which forward-redacts real
mentions before anything leaves the machine.

The API **never un-redacts** — rendering tokens back to real values is the
separate local lens (above), at the render boundary only. That is what keeps a
consumer's transcript and artifacts redacted.

Stable and versioned; full contract in
[docs/sanitized-context-api.md](docs/sanitized-context-api.md). An end-to-end
round trip over the fixtures:

```console
$ python -m examples.sanitized_context_roundtrip
```

## Deployment

Not yet deployed. Local-first by design — the gateway and its mapping table run on the user's machine.

## Branch Strategy

This project follows the agent-lab GitFlow conventions. See [gitflow.md](gitflow.md) for the branch model and merge policy.

## Built with

This product was built by the **sakuma ecosystem** — see [smartsquared/atlas](https://github.com/smartsquared/atlas) for the system that produced it. Process learnings from building this product flow back to [smartsquared/sakuma](https://github.com/smartsquared/sakuma); they do not live here.
