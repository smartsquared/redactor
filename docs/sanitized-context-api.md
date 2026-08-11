# Sanitized-context API

**Status:** stable contract for milestone m01, version **1**. This is the seam
the first consumer ([sakuma-finance](https://github.com/smartsquared/sakuma-finance))
speaks to hold a bookkeeping conversation through the gateway without ever
touching real data or the mapping table. It builds on the alias-space contract
([alias-contract.md](alias-contract.md)) and the four-artifact privacy model
([brief.md](brief.md)); change it only through the escalation path in
[CLAUDE.md](../CLAUDE.md).

Reference implementation: [`redactor/context_api.py`](../redactor/context_api.py).
End-to-end demo: [`examples/sanitized_context_roundtrip.py`](../examples/sanitized_context_roundtrip.py).

## 1. What it is for

The gateway is a bidirectional alias proxy. A consumer app should never see a
real account number or merchant name; it operates **entirely in alias-space** and
lets the gateway redact on the way out and (separately, locally) un-redact on the
way in. This API is that operating surface. It does two things:

1. **Serves alias-space context** — the redacted transactions, per-account
   balances, and date-range bounds a bookkeeping conversation needs. Identifying
   fields are alias tokens; amounts and dates stay real (alias-contract §1.1).
2. **Passes conversation turns through the outbound proxy** — a user turn is
   forward-redacted (story S2.1) before it can leave the machine; a model reply
   is received back **in alias-space**, not un-redacted.

### The un-redaction boundary (read this)

This API **never reverses an alias.** Un-redaction happens only at the local
render boundary, through the lens (story S2.2, `redactor lens` /
[`redactor/lens.py`](../redactor/lens.py)), on the machine that holds the mapping
table. Keeping the consumer in alias-space is the whole point: its transcript,
its stored history, and any artifact it writes therefore stay redacted
(CLAUDE.md — *"Un-redaction only at the local render boundary"*). A consumer that
wants to show a model's answer to a human pipes that alias-space answer through
the lens as a distinct, local step — see the round trip in §6.

## 2. Versioning

The context envelope carries a `version` field
(`CONTEXT_API_VERSION`, currently `1`), tracking the alias-space projection
envelope it wraps ([`redactor/projection.py`](../redactor/projection.py)). A
consumer reads `version` and refuses a shape it does not understand. Any change
to the shape of `ContextWindow`, `AccountBalance`, or `Turn` is a version bump
and a contract change; additive, backward-compatible fields may be introduced
without a bump only if existing consumers keep working unchanged.

## 3. Constructing the client

```python
from redactor.context_api import SanitizedContextClient
from redactor.ingest import ingest_statements
from redactor.outbound import OutboundRedactor
from redactor.resolve import PayeeResolver
from redactor.store import open_store

resolver = PayeeResolver()
with open_store(store_path, key) as store:
    projection = ingest_statements(
        store, statements, resolve_payee=lambda m: str(resolver.resolve(m))
    )
    client = SanitizedContextClient(
        projection=projection,
        redactor=OutboundRedactor.from_store(store, resolver),
    )
```

The client is built from two alias-space-safe inputs: the **projection** (the
ingest output — S1.4) and an **outbound redactor** (S2.1). Both derive from the
same store and resolver so their tokens line up. The client itself holds no
mapping-table bindings; the only real values in play are the surface forms the
outbound redactor keeps in memory to *find and replace* (see
[`redactor/outbound.py`](../redactor/outbound.py)), never to serialize.

## 4. Requesting context

```python
ctx = client.context(start="2026-05-01", end="2026-05-31")   # all args optional
```

| Parameter  | Meaning                                                              |
|------------|---------------------------------------------------------------------|
| `start`    | Inclusive ISO `YYYY-MM-DD` lower bound. `None` = unbounded below.    |
| `end`      | Inclusive ISO `YYYY-MM-DD` upper bound. `None` = unbounded above.    |
| `accounts` | Optional list of `ACCT-n` tokens to restrict to. `None` = all.      |

ISO dates sort lexically, so the bounds compare as plain strings.

Returns a **`ContextWindow`**:

| Field      | Type                   | Notes                                         |
|------------|------------------------|-----------------------------------------------|
| `records`  | `list[AliasRecord]`    | Redacted transactions in the window.          |
| `balances` | `list[AccountBalance]` | Per-account aggregates over the same window.  |
| `start`    | `str | None`           | Echoes the requested lower bound.             |
| `end`      | `str | None`           | Echoes the requested upper bound.             |
| `version`  | `int`                  | `CONTEXT_API_VERSION`.                         |

`AliasRecord` (from the projection): `account` (`ACCT-n`), `institution`
(`INST-n`), `payee` (`PAYEE-n`), `date` (real ISO), `amount` (real, signed —
negative = money out), `ttype`, `category`, `payee_category` (entity-level tag
from the **closed** vocabulary in
[payee-categories.md](payee-categories.md); `""` = untagged; lint-enforced so
it can never carry a real name — additive with a default, so the version stays
`1` per §2).

`AccountBalance`: `account` (`ACCT-n`), `institution` (`INST-n`), `net` (signed
sum of amounts), `debits` (sum of money-out), `credits` (sum of money-in),
`txn_count`, `currency`.

`ContextWindow.to_json()` serializes the window. It is **safe to write and to
ship into model context**: by construction it holds only tokens, amounts, and
dates — no real identifier, no mapping-table binding (crown-jewel rule).

## 5. Sending and receiving turns

```python
turn = client.send("Did I hit Whole Foods in May?")
# turn.text        -> "Did I hit PAYEE-3 in May?"   (safe to send to a model)
# turn.substitutions -> [Substitution(surface="Whole Foods", token="PAYEE-3", ...)]
# turn.warnings    -> []      # see below
# turn.clean       -> True    # no unresolved entity surfaced
```

`send(user_text)` applies **outbound forward-redaction** (S2.1): real mentions
become their stable tokens. An entity that resolves to nothing known is **not**
passed through silently — it is surfaced in `turn.warnings` (a `LeakWarning`
per unresolved mention), and `turn.clean` is `False`. A consumer inspects
`turn.clean` before relaying `turn.text` to a model, so an unknown entity is a
loud decision, never a quiet leak.

```python
reply = client.receive("Your INST-1 checking (ACCT-1) netted +6865.64.")
# reply.text is returned untouched — still alias-space, NOT un-redacted.
```

`receive(model_text)` wraps the model's alias-space reply as an inbound `Turn`.
It performs no substitution: rendering tokens back to real values is the local
lens (§1, S2.2), a separate step. This is what keeps the consumer's transcript in
alias-space.

`Turn` fields: `role` (`"user->model"` or `"model->user"` — alias-space labels,
never a real identity), `text`, `substitutions`, `warnings`, and the `clean`
property.

## 6. The round trip

The full loop a consumer runs, demonstrated end-to-end over the bundled fixtures
by [`examples/sanitized_context_roundtrip.py`](../examples/sanitized_context_roundtrip.py)
(stubbed model, throwaway store):

1. `client.context(...)` → alias-space context out (safe to ship into the model).
2. `client.send(user_text)` → outbound redaction → safe-to-send turn.
3. *(the model answers in alias-space; it only ever saw tokens.)*
4. `client.receive(model_text)` → alias-space reply in, **not** un-redacted.
5. `lens(reply.text, resolver_from_table(store.mapping))` → real values, rendered
   **only here**, locally, at the render boundary.

Everything that crosses the model boundary in steps 1–4 is alias-space; a real
value appears only in step 5, produced on the machine that holds the table.

Run it:

```console
$ python -m examples.sanitized_context_roundtrip
```

## 7. What this contract does not do

- **It does not un-redact.** No method reverses an alias; that is the lens (S2.2).
- **It does not serialize the mapping table.** The only persistence sink for the
  mapping is the encrypted store (`redactor.store`); this API exposes no dump.
- **It does not ingest.** Ingest (S1.4) produces the projection the client is
  built from; the client consumes that output, it does not read statement files.
- **It does not hold finance-domain conversation logic.** Monthly-close and
  accountability-partner behavior belongs to the consumer (sakuma-finance). This
  is the pipe, not the conversation.

## 8. Export provenance — the m02 import seam (cross-repo contract)

The in-memory `ContextWindow` above is the live conversation surface. The **m02**
import path is different: sakuma-finance imports a projection *file* off the disk
(`sakuma-finance import --projection <file>`), and it refuses any projection that
does not carry redactor's **green lint attestation**. `redactor export`
([`redactor/export.py`](../redactor/export.py)) is the only command that produces
such a file: it rebuilds a month's projection from the store, runs the S0.2
detection lint, and — only when the lint is green — writes the projection envelope
with a `provenance` block. A red projection writes nothing and exits non-zero.

The written file is the alias-space projection envelope
([`redactor/projection.py`](../redactor/projection.py)) plus one added key:

```json
{
  "version": 1,
  "records": [ ... ],
  "provenance": {
    "tool": "redactor",
    "check": "lint",
    "status": "green",
    "leak_count": 0,
    "timestamp": "2026-07-22T17:04:33.512+00:00",
    "linter": "redactor.lint",
    "linter_version": 1
  }
}
```

**This `provenance` block is a cross-repo contract, and the CONSUMER owns it.**
sakuma-finance's `records_import.LintAttestation.is_green` requires exactly
`tool == "redactor"`, `check == "lint"`, `status == "green"`; `leak_count` is
carried for its records. Extra keys are ignored by its `from_dict` — the
producer's `timestamp` / `linter` / `linter_version` ride along as extras:

| Field            | Type   | Owner    | Meaning                                                    |
|------------------|--------|----------|-----------------------------------------------------------|
| `tool`           | `str`  | consumer | Must be `"redactor"`.                                      |
| `check`          | `str`  | consumer | Must be `"lint"`.                                          |
| `status`         | `str`  | consumer | Always `"green"` in a written file (red is never written). |
| `leak_count`     | `int`  | consumer | `0` in a written file.                                     |
| `timestamp`      | `str`  | producer | ISO-8601 UTC instant the attestation was stamped.          |
| `linter`         | `str`  | producer | The linter identity (`redactor.lint`).                     |
| `linter_version` | `int`  | producer | `redactor.export.LINTER_VERSION` — bump on shape change.   |

Only a green file is ever written, so a consumer that finds a `provenance` block
with `verdict == "green"` and a `linter_version` it understands may trust the
attestation. **Changing any field here is a contract change**: bump
`LINTER_VERSION`, update this table, and update the reader on the sakuma-finance
side in the same wave, so the producer and reader can never silently drift. The
shape is pinned on this side by `tests/test_export_cli.py`
(`test_export_provenance_shape_is_the_cross_repo_contract`).
