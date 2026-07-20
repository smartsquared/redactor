# redactor — design brief

**Source:** sakuma whiteboard capture 2026-07-19 (`privacy-gateway-and-finance-partner-proposal.md`) plus the 2026-07-20 design discussion that ratified the un-redaction mechanism. This brief is the product-side home for that design.

## What it is

A privacy gateway: a **bidirectional alias proxy** that makes conversations about sensitive data safe with any chatbot. Deliberately narrow, gather-style — stable contract, domain adapters, nothing conversational inside. First domain: personal finance (consumer: `sakuma-finance`). Later candidates: medical, legal.

## The four-artifact privacy model

Redaction here is **reversible**, which splits the world into four artifacts with distinct handling:

| Artifact | Handling |
|---|---|
| 1. Raw data (statements, account numbers) | Local encrypted store only. Never in any repo. |
| 2. Alias mapping table (`PAYEE-7` ↔ real value) | **Local only, never in any repo.** The crown jewel: holding it reverses every redacted record. |
| 3. Redacted records (aliases + real amounts) | Private repo agents may clone (`smartsquared/finance-records`). Safe *enough* for model context, not anonymous — amounts/dates/stable pseudonyms are re-identifiable in aggregate. Never public. |
| 4. Gateway + consumer code | Ordinary repos; code + fixtures only. |

This amends the original capture's blanket rule ("real financial data never lives in any repo agents clone") to: **raw data and the mapping table never live in any repo; redacted records may, privately.**

## Pipeline

Ingest adapters (bank feeds via Plaid/SimpleFIN, statement files) → local encrypted store → reversible alias layer (stable pseudonyms, local mapping table) → sanitized-context API.

**Detection: wrap Microsoft Presidio; do not build NER** (build-vs-buy decided 2026-07). The unbought composition — the actual product — is:

- **Alias continuity across months** (enterprise redactors work per-request)
- **Personal-scale, local-first deployment** (market is enterprise-only)

## Three threats

1. **Secrets** (account numbers, credentials) — solved by architecture: the model-facing store never contains them.
2. **Identity** — reversible aliasing; amounts stay real (least identifying, most conversationally necessary).
3. **Injection** — statement memos/payee strings are attacker-controlled third-party text; scrub as untrusted input.

## Un-redaction mechanism (ratified 2026-07-20)

- **The gateway is a bidirectional proxy, not an output filter.** Outbound (human → model): forward-redact user text, or the user typing "Whole Foods" breaks alias-space themselves. Inbound (model → human): substitute aliases back at the render boundary.
- **Un-redaction runs only on the machine holding the mapping table.** Anything agent-written (reports, PR bodies, notifications) stays in alias-space forever; humans read those through a local "lens" step.
- **Alias stability is entity resolution.** Bank memo strings vary ("WHOLEFDS #1029 SEA", "WF MKT 445"); all must resolve to the same `PAYEE-7` across months. This is the hard engineering, and the moat.
- **Substitution must be mangle-tolerant but never false-positive.** Models emit "payee-7's", "Payee 7"; match case/punctuation-insensitively against deliberately-weird token shapes.

## v0 falsifier (contract-shaped, not feature-shaped)

The finance tool can hold a useful conversation through the gateway with **no identifiers leaking**, verified against fixtures.

## Marketability

Enterprise lane crowded/sales-heavy; personal lane open but hard to monetize. Built because Seith needs it; marketability is a cheap later lean-startup experiment.

## Open questions

- Finance v0 scope (bookkeeping vs. investments vs. tax) — owned by `sakuma-finance`, but shapes which adapters ship first.
- Encryption-at-rest choice for local store + mapping table (OS keychain vs. age vs. SQLCipher).
- Fleet token wall: agents can't clone personal-org private repos — mooted by placing repos in smartsquared, but verify before wave dispatch.
