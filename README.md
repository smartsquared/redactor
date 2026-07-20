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

## Deployment

Not yet deployed. Local-first by design — the gateway and its mapping table run on the user's machine.

## Branch Strategy

This project follows the agent-lab GitFlow conventions. See [gitflow.md](gitflow.md) for the branch model and merge policy.

## Built with

This product was built by the **sakuma ecosystem** — see [smartsquared/atlas](https://github.com/smartsquared/atlas) for the system that produced it. Process learnings from building this product flow back to [smartsquared/sakuma](https://github.com/smartsquared/sakuma); they do not live here.
