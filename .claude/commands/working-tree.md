Manage sibling worktrees following the `~/Code/<repo>-<slug>/` convention.

## Subcommands

### `/working-tree open <slug>`

Create or attach to a sibling worktree at `~/Code/<repo>-<slug>/` on the right branch.

**Branch resolution from `<slug>`:**

- `develop`, `main` → use as-is
- `release/*`, `hotfix/*` → use as-is
- anything else → `feature/<slug>` (matches `m<NN>_<slug>` milestone naming)

**Branch creation:**

- If branch exists locally or remotely → check it out
- If branch does not exist → create from `develop`

**Idempotency:**

- If the worktree path already exists and is on the right branch → confirm and continue
- If on a different branch → refuse with the conflict reported

After creation, switch the session into the new worktree via Claude Code `EnterWorktree`.

### `/working-tree close [<slug>]`

Remove a sibling worktree.

- `<slug>` given → close that one
- `<slug>` omitted, current cwd is a sibling worktree → confirm, then close it
- `<slug>` omitted and not in a worktree → list active worktrees and ask which to close

Refuses to close the worktree the session is currently in. Switch out first via `/working-tree open <other>` or exit the session.

Does NOT delete the branch. Branch lifecycle is deferred to the cleanup-preference work in #381. Use `git branch -d` after close if you want both.

### `/working-tree list`

Show this repo's worktrees (mirrors `git worktree list`), with **"you are here"** marked on the current worktree. Cross-repo listing across `~/Code/` is out of scope (different abstraction).

## Convention rules baked in

- **Path:** `~/Code/<canonical-repo>-<slug>/`. Canonical repo name is the main checkout's basename (resolved via `git worktree list`), so the command works equally from inside `agent-lab/` or `agent-lab-m20_slash_commands/`.
- **Slug-to-branch mapping** as above.
- **Sibling, never nested.** No worktree under another worktree.

## Mode gate

Available in plan and execute modes (treated as roughly read-only — operations on worktrees, not on remote state). Discuss mode declines, consistent with the no-tooling-mutation rule there.
