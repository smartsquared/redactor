# m01 — v0 falsifier (bookkeeping slice)

**Goal:** a consumer can hold a useful bookkeeping conversation through the gateway with **no identifiers leaking**, verified against fixtures. Contract-shaped, not feature-shaped ([brief](brief.md)).

**Scope decision (human, 2026-07-20):** finance v0 = bookkeeping — transactions and spend. Statement-file ingest only; Plaid/SimpleFIN feeds deferred past m01.

**Wave discipline:** shared interfaces (S0.2, S0.3) land before the parallel wave-1 agents that touch them. Merge each wave before dispatching the next.

---

## Wave 0 — foundations

### S0.1 — Repo tooling + CI
Python 3.11+ package skeleton (`redactor/`), pytest, ruff, and a `build-and-test` GitHub Actions check (that exact name — the ecosystem pre-flight greps for it) running lint + tests on PRs.
**Accept:** CI green on a trivial test; `pip install -e .` works; check name is `build-and-test`.

### S0.2 — Alias-space contract + bookkeeping fixtures
The shared interface for every later story. Spec (`docs/alias-contract.md`): alias token grammar (`ACCT-n`, `PAYEE-n`, `INST-n`...), collision rules, the mangle-tolerance matching rule (case/punctuation-insensitive, never false-positive). Plus synthetic bookkeeping fixtures: ≥3 months of checking + credit-card statements (CSV and OFX) with realistic memo garbage ("WHOLEFDS #1029 SEA"-style variants), provably fake (invalid account formats, fictional institutions).
**Accept:** contract doc reviewed; fixtures load; a fake-ness lint (regex for real-looking routing/account numbers) passes in CI.

### S0.3 — Local store + mapping table schema
Schema and storage decision for the local encrypted store (raw transactions) and the alias mapping table. Resolves the brief's open encryption question — evaluate OS keychain vs. age vs. SQLCipher; pick one, document why in an ADR.
**Accept:** ADR merged; schema module with create/open/migrate; store file is unreadable without the key; **no code path serializes the mapping table anywhere but the local store** (test-asserted).

---

## Wave 1 — core pipeline

### S1.1 — Presidio detection wrapper
Wrap Presidio behind our own `Detector` interface (wrap, don't build NER — decided). Finance-tuned recognizers for account/routing patterns in fixture statements.
**Accept:** every seeded identifier in fixtures detected; interface hides Presidio types from callers.

### S1.2 — Alias assignment + mapping table
Stable alias issuance: same entity → same alias forever; new entity → next token. CRUD against the S0.3 mapping table.
**Accept:** re-running ingest over the same fixtures issues zero new aliases; property test for collision-freedom.

### S1.3 — Payee entity resolution
The moat. Normalize memo-string garbage to canonical entities so "WHOLEFDS #1029 SEA" and "WF MKT 445" resolve to one `PAYEE-n` across months. Deterministic normalization first (strip store numbers, cities, dates); a fuzzy-match layer behind a confidence threshold; below-threshold → new entity (never silently merge).
**Accept:** fixture set includes ≥10 multi-variant payees; ≥90% variant-grouping accuracy; zero false merges.

### S1.4 — Statement-file ingest adapter
CSV + OFX → local store, running detection (S1.1) + aliasing (S1.2/S1.3) at ingest. Raw fields land only in the encrypted store; alias-space projection is a separate output.
**Accept:** fixtures ingest end-to-end; alias-space projection contains zero seeded identifiers (leak-lint in CI).

### S1.5 — Injection scrub of third-party text
Memo/payee strings are attacker-controlled input (brief, threat 3). Scrub/neutralize instruction-shaped content in third-party text fields before they enter alias-space projections.
**Accept:** fixture memos seeded with injection payloads ("ignore previous instructions...") come out neutralized; benign memos pass through legibly.

---

## Wave 2 — proxy + falsifier

### S2.1 — Outbound forward-redaction
User→model direction: text mentioning real entities ("how much at Whole Foods?") is redacted to alias-space before leaving the machine, resolving against the mapping table + entity resolution.
**Accept:** fixture utterances mentioning known entities redact correctly; unknown entities pass with a warning surface, not a silent leak.

### S2.2 — Inbound re-substitution (local lens)
Model→human direction: mangle-tolerant alias→real substitution per the S0.2 matching rule, runnable **only** where the mapping table lives. Ships as a `redactor lens` CLI.
**Accept:** substitution passes a mangle corpus ("payee-7's", "Payee 7", case variants); zero false positives on non-alias text; refuses to run without local table access.

### S2.3 — Sanitized-context API
The contract sakuma-finance consumes: request alias-space context (transactions, balances, date ranges) + send/receive conversation turns through the proxy (S2.1 outbound applied). Stable, versioned, documented.
**Accept:** contract doc + typed client; round-trip demo script over fixtures.

### S2.4 — Leak-check falsifier harness
**The milestone gate.** Scripted multi-turn bookkeeping conversation over fixtures through the full pipeline; harness asserts no seeded identifier (raw or near-variant) appears in anything that crossed the model boundary, and that the conversation was *useful* (questions answered correctly from fixture data).
**Accept:** harness runs in CI; fails on a deliberately-seeded leak (mutation-tested); passes on the real pipeline. m01 closes when this is green.

---

## Explicitly deferred past m01

Plaid/SimpleFIN live feeds · medical/legal adapters · finance-records write path (propose-PRs — sakuma-finance m01) · per-category privacy dial · packaging/distribution.
