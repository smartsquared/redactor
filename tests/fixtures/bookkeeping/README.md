# Synthetic bookkeeping fixtures

Provably-fake statement data for milestone m01 (story S0.2). These files are the
shared substrate for detection (S1.1), aliasing (S1.2), payee resolution (S1.3),
ingest (S1.4), and the leak-check falsifier (S2.4).

## What's here

- `checking_2026-{04,05,06}.{csv,ofx}` — 3 months, "Bank of Nowhere" checking.
- `credit_2026-{04,05,06}.{csv,ofx}` — 3 months, "Placeholder National Bank" card.
- `manifest.json` — payee-variant ground truth + account metadata.
- `generate_fixtures.py` — deterministic generator; **edit this, not the outputs**.

Load them with `redactor.fixtures.load_statements()`.

## Why they're safe (no real data, ever)

Every identifier is deliberately fake and CI enforces it via the fake-ness lint
(`redactor/fakeness.py`, run in the `build-and-test` check):

- Institutions are fictional.
- Routing number `123456789` **fails** the ABA checksum.
- Account ids are alphanumeric (`NOWHERE-CHK-000199`), matching no real format.
- No memo/amount/date carries a Luhn-valid card number or an SSN-shaped string.

See [docs/alias-contract.md](../../../docs/alias-contract.md) §4 for the contract.

## Regenerating

```bash
python tests/fixtures/bookkeeping/generate_fixtures.py
```

Output is byte-for-byte reproducible (no randomness).
