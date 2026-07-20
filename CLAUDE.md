# redactor — project conventions for Claude Code

## What this project is

A privacy gateway: a bidirectional alias proxy that makes conversations about sensitive data safe with any chatbot. It ingests real data into a local encrypted store, redacts identifying strings to stable pseudonyms before model contact, and reverses the aliases at the local render boundary. First consumer: `smartsquared/sakuma-finance`. Full design: [docs/brief.md](docs/brief.md).

This repo is a **product of the sakuma process**, not an ecosystem component. The sakuma pipeline built it; its job now is to serve its users. Process and methodology concerns do not live here — see "What this repo does NOT do" below.

## Your first task in this repo

Read [docs/brief.md](docs/brief.md) first — the four-artifact privacy model and the un-redaction mechanism are ratified design, not suggestions. Bootstrap work (wave plan, first issues) has not been filed yet; when it is, this section will link it.

Per gitflow, do the work on a feature branch off `develop` and open a PR back to `develop` when done.

## Surfacing PRs to the human

**Whenever you ask the human to look at, review, or merge a PR, always give them the link** — the full URL (`https://github.com/<owner>/<repo>/pull/<n>`) or a markdown link. Never reference a PR by number alone when you want them to act on it; the human works across many repos and a bare `#4` makes them hunt. One-click, not a chore.

**Before you surface a PR as ready, pre-flight all three gates** — CI green is not enough:

```bash
gh pr view <N> --json mergeable,mergeStateStatus,statusCheckRollup --jq '{
  merge: .mergeable,
  state: .mergeStateStatus,
  ci:    [.statusCheckRollup[] | select(.name == "build-and-test") | .conclusion] | last
}'
```

Ready means `merge: MERGEABLE`, `state: CLEAN`, and `ci: SUCCESS`. A `CONFLICTING` PR with a green build is not ready; base-branch policy violations show in `mergeStateStatus`, not in the check rollup. If any gate fails, fix it or escalate — never surface as ready.

## Project conventions

- **No real data, ever** — this repo holds code and synthetic fixtures only. Real financial/medical/legal data and the alias mapping table live outside all repos, on the user's machine. A fixture that looks real should be provably fake (test bank names, invalid account formats).
- **The mapping table is the crown jewel** — any code path that could serialize, log, or transmit the alias↔real mapping off-machine is a security bug, not a style issue. Treat it like a private key.
- **Un-redaction only at the local render boundary** — agent-written artifacts (PR bodies, reports, notifications) stay in alias-space. Never write a substitution step into anything that runs off the user's machine.
- **Wrap Presidio, don't build NER** — detection is bought; alias continuity and entity resolution are built. Keep that boundary clean.

## When to log (the logging protocol)

This repo follows the ecosystem logging protocol at `~/Code/ledger/docs/logging-protocol.md`: **open a ledger goal-record at the first mutation toward a stated goal; close it when the goal completes or is abandoned.** Discussions get a record only when they end in a decision (`/log-that`). Never create records on a clock. The SessionStart hook surfaces open records automatically.

## What this repo does NOT do

- **Methodology and process learnings** — if building this product teaches something about how the pipeline should work, that signal goes to [smartsquared/sakuma](https://github.com/smartsquared/sakuma), not here.
- **Pipeline tooling fixes** — agent-lab, blueprint, compass bugs get filed on those repos, not patched around in this one.
- **Finance-domain conversation logic** — the accountability-partner behavior (monthly closes, commitments, drift detection) belongs to [smartsquared/sakuma-finance](https://github.com/smartsquared/sakuma-finance). This repo is the pipe, not the conversation.
- **Storing redacted records** — those live in [smartsquared/finance-records](https://github.com/smartsquared/finance-records) (private).

If you find yourself doing any of these here, you've crossed the product/process boundary.

## Andon awareness

Under active pipeline construction: poll STATUS.md in [smartsquared/andon](https://github.com/smartsquared/andon) and halt when `pulled`.

## Useful state to know

- Repo created 2026-07-20 from template smartsquared/seedling-product.
- Brief / design doc: [docs/brief.md](docs/brief.md); original capture in sakuma `whiteboard/privacy-gateway-and-finance-partner-proposal.md`.
- Deployment target: not yet deployed; local-first by design.
- Sibling repos: [sakuma-finance](https://github.com/smartsquared/sakuma-finance) (first consumer), [finance-records](https://github.com/smartsquared/finance-records) (redacted-record store).

## Branch Strategy

This project follows the agent-lab GitFlow conventions. See [gitflow.md](gitflow.md) for the branch model and merge policy.

## Escalation

When in doubt, stop and ask. Trigger conditions: anything that widens what leaves the local machine (new egress, new serialization of the mapping table, loosening the fixture-only rule), and any change to the four-artifact privacy model in the brief.
