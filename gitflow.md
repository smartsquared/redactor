# GitFlow

Agent-lab uses the full [GitFlow branching model](https://nvie.com/posts/a-successful-git-branching-model/) with one firm convention: **every PR merges with a merge commit, never a squash.** The richer branch topology in the git graph is the point.

## Branches

Five lane types, each with a defined role. Branch-name format is specified in [docs/naming.md](docs/naming.md); this section covers the *role* of each lane.

```
main                       — stable releases, tagged
hotfix/<slug>              — emergency fixes, branched from a main tag
release/v<N>               — release candidates, branched from develop
develop                    — integration branch, accumulates feature work
feature/m<NN>_<slug>       — milestones and scoped work, branched from develop
m<NN>-i<NNN>-<type>_<slug> — per-ticket work, branched from a feature/*
```

### `main`

- Contains tagged releases only. Every commit on `main` corresponds to a version.
- **Never push directly.** `main` only accepts merges from `release/*` (new version) or `hotfix/*` (patch version).
- Tags: `v0.1`, `v0.2`, `v1.0`, etc. Tag immediately after a merge to `main`.

### `develop`

- Integration branch. All feature work accumulates here before a release.
- **Never push directly.** Accepts merges from `feature/*`, `release/*` (back-merge after release), and `hotfix/*` (back-merge after hotfix).
- **`develop` is the repo's default branch**, not `main`. This is intentional: GitHub auto-closes issues referenced with `Closes #N` only on merges to the default branch. For a GitFlow project, the default integration point *is* `develop` — setting it as the default makes `Closes #N` fire at the right time (PR merge to `develop`) rather than deferring until a release cut. `main` remains the stable release pointer and only accepts `release/*` and `hotfix/*` merges.

### `feature/m<NN>_<slug>`

- Long-lived milestone branches (`feature/m11_hardening`, `feature/m12_convention_propagation`).
- Branched from `develop`. Merged back to `develop` when tested.
- Internal per-ticket PRs target the feature branch, not develop directly.

### `m<NN>-i<NNN>-<type>_<slug>`

- Per-ticket work branches, usually opened by agents via `agent-lab dispatch`.
- Branched from a `feature/*` branch (or directly from `develop` for standalone work).
- Deleted after the PR merges.
- Format, type tags, and examples in [docs/naming.md](docs/naming.md).

### `release/v<N>`

- Release candidate branches (`release/v1.1`, `release/v2.0`).
- Branched from `develop` when it's stable enough to cut a release.
- Only release-prep commits land here: version bumps, changelog, last-minute fixes found during release QA.
- Merged to **both** `main` (with a version tag) **and** back to `develop` (so the release-prep commits persist in the integration line).

### `hotfix/<slug>`

- Emergency fix branches for bugs found in a released version.
- Branched from the `main` tag of the affected version.
- Merged to **both** `main` (with a new patch tag, e.g. `v1.0.1`) **and** back to `develop`.
- Distinct from `feature/*` because they skip the develop → release → main flow — they go straight back to main.

## Linking PRs for review

Any time you ask a human to look at or review a PR, include the **full clickable URL** (`https://github.com/<owner>/<repo>/pull/<N>`) — not just `#<N>` or a bare number. A bare `#N` renders as plain text in a terminal, so the human has to go find it; a full URL is one click. This applies to every hand-off: "ready for review", "take a look", and any status update that references a PR.

## Merge strategy — always merge commits

**`gh pr merge <N> --merge`** (git `--no-ff`) for every PR. **Never `gh pr merge --squash`.**

Squash flattens a branch's commits onto the target, which destroys the branch topology in the graph. Merge commits preserve every branch as a visible lane. The graph should read like the canonical GitFlow diagram, with loops and lanes for every merged branch.

### Commands by scenario

**Per-ticket PR → feature branch:**
```bash
gh pr merge <N> --merge --delete-branch
```

**Feature branch → develop:**
```bash
gh pr merge <N> --merge
# delete the feature branch after confirming it landed
git push origin --delete feature/m<NN>_<slug>
```

**Release (develop → main + back-merge):**
```bash
# Branch from develop
git checkout develop && git pull
git checkout -b release/v1.0
# Version bump, changelog, etc.
git commit -am "Prepare v1.0 release"
git push -u origin release/v1.0

# PR release → main
gh pr create --base main --head release/v1.0 --title "Release v1.0"
gh pr merge <N> --merge
git checkout main && git pull && git tag v1.0 && git push origin v1.0

# Back-merge main → develop
git checkout develop && git pull
git merge main --no-ff -m "Back-merge v1.0 to develop"
git push origin develop

# Delete release branch
git push origin --delete release/v1.0
```

**Hotfix (main tag → main + back-merge):**
```bash
# Branch from main tag
git checkout v1.0
git checkout -b hotfix/<slug>
# Fix, commit
git push -u origin hotfix/<slug>

# PR hotfix → main
gh pr create --base main --head hotfix/<slug> --title "Hotfix: <what>"
gh pr merge <N> --merge
git checkout main && git pull && git tag v1.0.1 && git push origin v1.0.1

# Back-merge main → develop
git checkout develop && git pull
git merge main --no-ff -m "Back-merge v1.0.1 hotfix to develop"
git push origin develop
```

## Why merge commits, not squash

**Merge commits cost us:**
- A "Merge pull request #N" commit per PR (noise in linear first-parent log)
- Bisect is occasionally noisier (intermediate WIP commits might not build cleanly)
- Release notes from `git log` need de-duplication

**Merge commits buy us:**
- Rich visual topology: every branch is a visible lane in the graph
- PR boundaries preserved forever — you can always diff exactly what one PR changed
- Hotfix and release flows are distinct shapes in the graph, making the project's lifecycle legible
- Per-PR WIP commits remain navigable (useful when the squashed commit message is terse)

We accepted the trade-off on 2026-04-20 after the v10 milestone shipped with a linear graph from squash-merges. The richer topology is worth the log noise.

## Hotfix propagation

Hotfixes go `main → main + develop`. If an active `feature/*` branch is out at the time of the hotfix, it does NOT automatically get the fix — the next merge of `develop` into that feature branch (or the next rebase, if using rebase) will pick it up. When dispatching agents to long-running feature branches, periodically merge develop back in to collect accumulated hotfixes.

## Coordinating with the SET agent

The SET run policy ([dispatch_qa.md](src/agent_lab/prompts/dispatch_qa.md) and memory `feedback_set_run_policy.md`) classifies findings as REGRESSION, SPEC_GAP, or PRE_EXISTING. PRE_EXISTING findings route to `main` as hotfix candidates under this gitflow — they become `hotfix/*` branches when the Lieutenant scopes them.

## Related files

- [CLAUDE.md](CLAUDE.md) — project-level conventions, summary Branch Strategy section pointing here
- [src/agent_lab/prompts/dispatch_qa.md](src/agent_lab/prompts/dispatch_qa.md) — SET agent prompt; agents open `issue-N` branches per this spec
- [docs/escalation-protocol.md](docs/escalation-protocol.md) — when to stop and ask a human instead of branching/merging autonomously
