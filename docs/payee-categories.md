# Payee category tags

**Status:** shipped with schema v5 / linter still v1 (the field is additive).
Reference implementation: [`redactor/categories.py`](../redactor/categories.py),
[`redactor/payees.py`](../redactor/payees.py) (`tag` / `untag` /
`propose_categories`), pinned by
[`tests/test_payee_categories.py`](../tests/test_payee_categories.py).

## The problem this solves

Eligibility questions — the motivating one: *"which of my transactions could my
HSA legitimately pay for?"* — are properties of the **counterparty**, and real
counterparty names exist only on the machine holding the mapping table. A
consumer operating in alias-space sees `PAYEE-12 −$85.00` and cannot know that
is a dentist. Pasting real names into a chatbot to ask "which of these are
medical?" is exactly the leak this gateway exists to prevent (it leaks the
medical fact, not just identity).

The answer: **classify locally, ship only the verdict.** A category tag is
bound to the canonical payee *entity* on the local machine; every transaction
resolving to that entity inherits it, and the tag — a closed-vocabulary word,
never a name — rides into projections as `payee_category`.

## The closed vocabulary

| Category | Meaning |
|---|---|
| `pharmacy` | Dedicated pharmacies / drugstores. |
| `medical-provider` | Clinics, hospitals, physicians, labs, therapy. |
| `dental` | Dentists, orthodontists. |
| `vision` | Optometry, ophthalmology, opticians. |
| `mixed-retailer` | Sells eligible **and** ineligible goods (big-box, online). Merchant identity alone cannot settle eligibility — resolve per-receipt downstream. |
| `non-medical` | Explicitly reviewed and ruled out. |

`""` (empty) means **untagged** — not yet reviewed. It is deliberately distinct
from `non-medical`, which is a human verdict.

**The set is closed by construction, twice.** `PayeeRegistry.tag` refuses any
value outside the vocabulary, and the detection lint
([`redactor/lint.py`](../redactor/lint.py), kind `category_nonconformance`)
refuses any projection whose `payee_category` is off-vocabulary. Between them,
the field cannot smuggle a real merchant name off-machine — that is the privacy
invariant. Extend the vocabulary by amending `CATEGORIES`, the lint mirrors it
automatically; never by loosening validation.

## Workflow

```console
$ redactor payees --store ~/.redactor/store.db --categorize
  PAYEE-7  ->  pharmacy  (FAKEVILLE PHARMACY)        # local render: labels revealed
$ redactor payees --store ~/.redactor/store.db --tag PAYEE-7 pharmacy
redactor payees: tagged PAYEE-7 as pharmacy.
$ redactor export --store ~/.redactor/store.db --month 2026-05 --out proj-2026-05.json
```

- `--categorize` runs local keyword rules over real display labels and
  **writes nothing** — like `--review`, it is a local render surface. Absence
  of a proposal is "no opinion", never a `non-medical` verdict.
- `--tag` / `--untag` are the human confirmation, journaled like every payee
  mutation (the journal stays alias-space: a token and a vocabulary word).
- Tags live on the **canonical head**: tagging a merged-away token tags the
  entity it folds into, and re-projection/export picks the tag up for every
  transaction that resolves there — alias continuity is what makes one tag
  cover every memo variant across every month.

## What a consumer does with it

`payee_category` appears on every `AliasRecord`
([sanitized-context-api.md](sanitized-context-api.md) §4), so sakuma-finance
can sum, group, and converse about "medical-ish spending" entirely in
alias-space, with real amounts. **Which categories count as HSA-eligible, and
what to do with `mixed-retailer` (needs itemized receipts), is finance-domain
logic and lives in sakuma-finance** — this repo ships the metadata contract
only. Real-name output for a human (an accountant's list) is rendered through
the local lens, never written to a repo.

## Privacy note (what this widens, deliberately)

Redacted records with medical-kind tags reveal *that* medical spending exists —
a step up from untagged records, accepted deliberately (2026-08-10 decision):
artifact 3 was already "safe enough, not anonymous" (brief §four-artifact
model), the tags are a coarse closed enum, and finance-records stays private.
Nothing new becomes reversible; the crown jewel gains nothing.
