# <repo-name> — project conventions for Claude Code

## What this project is

<one-paragraph description matching README.md>

This repo is a **product of the sakuma process**, not an ecosystem component. The sakuma pipeline built it; its job now is to serve its users. Process and methodology concerns do not live here — see "What this repo does NOT do" below.

## Your first task in this repo

<what's the bootstrap work? Link to the brief and the first issue.>

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

- **<convention 1>** — <why>
- **<convention 2>** — <why>
- **<convention 3>** — <why>

## When to log (the logging protocol)

This repo follows the ecosystem logging protocol at `~/Code/ledger/docs/logging-protocol.md`: **open a ledger goal-record at the first mutation toward a stated goal; close it when the goal completes or is abandoned.** Discussions get a record only when they end in a decision (`/log-that`). Never create records on a clock. The SessionStart hook surfaces open records automatically.

## What this repo does NOT do

- **Methodology and process learnings** — if building this product teaches something about how the pipeline should work, that signal goes to [smartsquared/sakuma](https://github.com/smartsquared/sakuma), not here.
- **Pipeline tooling fixes** — agent-lab, blueprint, compass bugs get filed on those repos, not patched around in this one.
- <product-specific out-of-scope item — what other repo or service owns this>

If you find yourself doing any of these here, you've crossed the product/process boundary.

## Andon awareness

<does this repo honor andon? Products under active pipeline construction should: poll STATUS.md and halt when `pulled`. A shipped product in maintenance may not need to.>

See the protocol in [smartsquared/andon](https://github.com/smartsquared/andon).

## Useful state to know

- Repo created <YYYY-MM-DD> from template smartsquared/seedling-product.
- Brief / design doc: <link>
- Deployment target: <host/domain, or "not yet deployed">
- <other relevant context: driving sakuma loop, sibling repos (e.g. an asset pipeline), prior artifacts this product replaces>

## Branch Strategy

This project follows the agent-lab GitFlow conventions. See [gitflow.md](gitflow.md) for the branch model and merge policy.

## Escalation

When in doubt, stop and ask. Trigger conditions: <product-specific ambiguity that warrants pausing — e.g. anything user-visible that the brief doesn't ratify>.

<!--
This repo was scaffolded from smartsquared/seedling-product.

If you're filling this template out for a new product:

1. Replace all `<placeholders>` above.
2. Customize project conventions to match what this product actually is.
3. Set the andon-awareness section based on whether the pipeline is actively building here.
4. Delete this comment block.
-->
