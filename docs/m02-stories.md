# m02 — real statements (redactor side)

**Goal:** the human can take real bank exports, ingest them into the local encrypted store with keychain-custodied keys, and produce alias-space projections that are provably clean by *detection* (real data has no seed manifest) — ready for sakuma-finance's first real close.

**The rule this milestone lives under:** real data never leaves the human's machine, never enters a repo, never appears in CI or an agent sandbox. Every story here is built and tested against fixtures; the real-data path is exercised only by the human, via the m02 runbook (sakuma-finance side). The **milestone gate is runbook-shaped, not CI-shaped**: m02 closes when the human completes a real ingest with the detection lint green locally.

**Sibling:** [sakuma-finance m02](https://github.com/smartsquared/sakuma-finance/blob/develop/docs/m02-stories.md) — records import + first real close.

---

## Wave 0 (parallel-safe)

### S0.1 — `redactor init`: store lifecycle + keychain key custody
Implements the ADR-0001 "Key custody" follow-through: `redactor init` creates the encrypted store with a generated high-entropy key stored in the OS keychain (macOS Keychain first; `keyring` library for the seam), `redactor status` reports store location/schema version/key availability without touching data. `--key`/`REDACTOR_KEY` stay as overrides (CI/tests); keychain becomes the human default so no passphrase lives in shell history or env.
**Accept:** init→status→open round trip with keyring-backed key (mocked keyring in tests); wrong/missing key paths fail with actionable messages; no code path prints or logs the key.

### S0.2 — Detection-based leak-lint (`redactor lint`)
Fixtures had a seed manifest; real data has no ground truth. Add a detection sweep: run the finance Detector (Presidio) + structural checks (fakeness-lint's ABA/Luhn/SSN validators, alias-contract conformance) over any alias-space projection or artifact, zero-findings = clean. This becomes the lint every projection must pass **before it leaves the store** — wired as the default final step of ingest, and standalone as `redactor lint <file>`.
**Accept:** clean on the fixture projections; bites on planted real-looking identifiers (checksum-valid routing/card numbers, detected names/accounts) in an otherwise-alias-space file; ingest refuses to emit a projection that fails the lint (override flag exists but prints a red warning).

### S0.3 — Tolerant statement parsing + ingest report
Real bank exports are messier than fixtures (header variants, encodings, date formats, OFX dialects). Harden the CSV/OFX loaders: column-mapping heuristics with an explicit `--map` escape hatch, per-file error reporting that **never echoes raw field values** into terminal output that might be copy-pasted (show row numbers + field names, values only behind `--show-raw`). `redactor ingest <files...>` batch CLI: per-file summary (rows ingested, new payees, aliases issued, lint verdict).
**Accept:** fixture files plus deliberately-mangled variants (BOM, extra columns, MM/DD/YYYY, OFX 1.x/2.x) all ingest or fail with actionable, value-free errors; summary output is alias-space only.

## Wave 1

### S1.1 — Entity-resolution review flow (`redactor payees`)
Real memo garbage will produce low-confidence groupings and wrong display labels. `redactor payees --review` lists entities with variant counts, confidence, and display labels (local render — this output is for the human's eyes on the machine holding the table); `redactor payees --merge A B` / `--split` / `--rename` fix groupings and labels, updating the mapping table (alias stability preserved: merges alias the loser to the winner, never renumber). Below-threshold groupings are flagged for review at ingest time.
**Accept:** merge/split/rename round-trip on fixtures with alias stability property tests; ingest summary flags low-confidence groupings; all mutations journaled in the store (auditable).

---

## Milestone gate (runbook-shaped)

m02 closes when the human has, on their own machine, per the runbook: initialized a keychain-custodied store, ingested ≥1 month of real statements from ≥2 accounts, reviewed payee groupings, and produced projections with `redactor lint` green. No CI artifact can prove this; the human's confirmation on the milestone's gate issue closes it.

## Deferred

Plaid/SimpleFIN live feeds · alias rotation tooling (documented procedure only for now) · non-finance domains · Windows/Linux keychain backends (seam exists, macOS first).
