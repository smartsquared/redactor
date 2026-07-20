# Alias-space contract

**Status:** ratified interface for milestone m01. Every later story (detection,
alias assignment, entity resolution, the outbound/inbound proxy, the leak-check
falsifier) speaks this contract. Change it only through the escalation path in
[CLAUDE.md](../CLAUDE.md) — it is part of the four-artifact privacy model in
[brief.md](brief.md).

An **alias** is the stable pseudonym that stands in for a real, identifying
string once it has crossed into alias-space. "Alias-space" is everything that
may leave the local machine: model context, agent-written artifacts, redacted
records. The real↔alias mapping table stays local and is the crown jewel; this
document specifies only the *tokens*, never their bindings.

## 1. Token grammar

An alias token is:

```
<CLASS>-<n>
```

* `CLASS` — an uppercase entity class from the closed set below (`[A-Z]+`).
* `-` — a single ASCII hyphen-minus (`U+002D`), the **canonical** separator.
* `n` — a positive base-10 integer with no leading zeros, issued per class,
  starting at `1` and increasing monotonically.

Canonical (emitted) form is exactly this: uppercase class, single hyphen, bare
integer — `PAYEE-7`, `ACCT-1`, `INST-3`. The canonical form is what the
detector emits, what redacted records store, and what the mapping table keys on.
Section 4 defines the *looser* set of shapes the inbound matcher must still
recognise, because models mangle tokens.

### 1.1 Entity classes

The class set is closed for m01; adding a class is a contract change.

| Class    | Stands for                                             | Example  |
|----------|--------------------------------------------------------|----------|
| `PAYEE`  | A merchant / counterparty resolved from memo strings   | `PAYEE-7`|
| `ACCT`   | One of the user's own accounts (checking, card, …)     | `ACCT-1` |
| `INST`   | A financial institution / bank                         | `INST-2` |
| `PERSON` | A named natural person (payroll counterparty, etc.)    | `PERSON-4` |
| `CARD`   | A payment-card instrument, distinct from its `ACCT`    | `CARD-1` |

Amounts and dates are deliberately **not** aliased — per the brief they are the
least-identifying, most conversationally necessary fields, and stay real.

### 1.2 Why the shape is deliberately weird

The `CLASS-n` shape is chosen so tokens essentially never occur in natural
statement text or model prose. That is what makes the inbound matcher (§4) able
to be simultaneously mangle-tolerant and false-positive-free: there is no
ordinary English in which "PAYEE-7" appears except as one of our tokens.

## 2. Collision & issuance rules

1. **Bijective within a class.** At any time the mapping table holds a
   one-to-one relation between real entities and aliases of a given class. Two
   real entities never share an alias; one real entity never holds two aliases
   of the same class.
2. **Namespaced by class.** `ACCT-1` and `PAYEE-1` are unrelated. The `(CLASS,
   n)` pair is the identity, never `n` alone.
3. **Monotonic, gap-tolerant issuance.** New entity in a class → next unused
   integer for that class. Numbers are **never reused**, even if an entity is
   deleted, so a stale redacted record can never be silently re-bound to a
   different real entity. Gaps are legal.
4. **Stability is forever.** Once `WHOLEFDS #1029 SEA` resolves to `PAYEE-7`,
   every future variant of that entity resolves to `PAYEE-7`. Alias stability
   across months *is* entity resolution (brief) and is specified as the S1.3
   moat; this contract only fixes the invariant that resolution must preserve.
5. **Resolution never guesses silently.** A memo string that cannot be resolved
   to an existing entity above the confidence threshold gets a **new** entity
   and a **new** alias — never a coerced merge into an existing one. False
   merges are re-identification bugs.

## 3. Mangle-tolerant matching rule (inbound / lens boundary)

The gateway is a bidirectional proxy. Outbound (human→model) it forward-redacts
real strings to canonical tokens. Inbound (model→human), at the local render
boundary only, it substitutes tokens back to real strings. Models do not echo
tokens verbatim — they lowercase, re-space, re-punctuate, pluralise. The
inbound matcher must recover the intended `(CLASS, n)` from these manglings
**without ever matching non-alias text.**

### 3.1 Recognised manglings

For a canonical `CLASS-n`, all of the following resolve to the same
`(CLASS, n)`:

* **Case-insensitive** on the class: `PAYEE-7`, `payee-7`, `Payee-7`.
* **Separator variants:** hyphen, underscore, or a single space —
  `PAYEE-7`, `PAYEE_7`, `PAYEE 7`. (The canonical separator remains `-`.)
* **Possessive / plural suffix:** a trailing `'s`, `’s`, or `s` immediately
  after the number — `PAYEE-7's`, `PAYEE-7’s`, `PAYEE-7s` → `PAYEE-7`.

The number itself is matched literally: no case, no separators inside it.

### 3.2 Never false-positive

The matcher must not fire on text that is not one of our tokens. In particular:

* **Whole-number boundary.** `PAYEE-70` is `(PAYEE, 70)`, never `(PAYEE, 7)`
  followed by `0`. The digit run is matched greedily and bounded by a
  non-digit; a longer adjacent number is a *different* alias, not a prefix hit.
* **Class-word boundary.** The class must stand alone as a word — `XPAYEE-7`,
  `REPAYEE-7`, or `payees-7` (extra letters glued to the class) do **not**
  match. Require a non-`[A-Za-z]` boundary (or string start) before the class.
* **Closed class set.** Only the classes in §1.1 match. `FOO-7` is not an alias.
* **Unknown numbers pass through.** A syntactically valid token whose `(CLASS,
  n)` is absent from the local mapping table is **left untouched**, not
  substituted and not errored — it simply was never issued here.

### 3.3 Reference regex

The executable reference implementation lives in `redactor/alias.py`
(`ALIAS_RE`, `find_aliases`, `canonical`). The core pattern is:

```
(?<![A-Za-z0-9])(PAYEE|ACCT|INST|PERSON|CARD)[-_ ](\d+)(?:['’]?s|['’]s)?(?![0-9])
```

read case-insensitively on the class, with the trailing group consuming an
optional possessive/plural. The negative lookbehind enforces the class-word
boundary; the trailing `(?![0-9])` enforces the whole-number boundary. Any
change to the token grammar must update both this document and that module in
the same PR, and the property tests in `tests/test_alias_contract.py` guard the
never-false-positive rule.

## 4. Fixtures & the fake-ness invariant

The synthetic bookkeeping fixtures under `tests/fixtures/bookkeeping/` exercise
this contract: ≥3 months of checking + credit-card statements in CSV and OFX,
with the deliberately-varied memo strings ("WHOLEFDS #1029 SEA" vs "WF MKT 445")
that §2.4 resolution must collapse. `manifest.json` records the payee-variant
ground truth.

Per the repo's first rule — **no real data, ever** — the fixtures are provably
fake and CI enforces it. The fake-ness lint (`redactor/fakeness.py`, run in the
`build-and-test` check) fails the build if any tracked fixture or doc contains a
**real-looking** identifier:

* a 9-digit run that **passes** the ABA routing checksum,
* a 13–19 digit run that **passes** the Luhn card checksum, or
* an SSN-shaped `\d{3}-\d{2}-\d{4}` string.

The fixtures dodge all three on purpose: the routing number `123456789` fails
the ABA checksum, account ids are alphanumeric (`NOWHERE-CHK-000199`), and no
memo carries a card- or SSN-shaped number. Fictional institutions ("Bank of
Nowhere", "Placeholder National Bank") complete the "provably fake" bar.
