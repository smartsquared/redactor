Wrap `agent-lab dispatch`. Execute-mode only.

## Usage

`/dispatch <issue-ref> [extra-args...]`

Where `<issue-ref>` is `owner/repo#NNN`.

## What this does

Shells out to `agent-lab dispatch --issue <issue-ref>` (opus by default via the configured profile — see CLAUDE.md model selection guidance). Reports the dispatch result.

## What it does NOT do

- Reimplement dispatch logic — `agent-lab` CLI is the source of truth.
- Run in plan or discuss mode — these are mutating actions.

## Defaults

- Model: opus (resolved from the configured profile's `default_model`). Override with `/dispatch <issue> --model sonnet` for trivial tasks.
- Auto-pick worker (no `--agent` unless specified)
- Honors `.agent-lab/dispatch-notes.md` per CLAUDE.md

## Mode gate

If invoked outside execute mode, refuse with: "Dispatch is execute-mode only. Run /execute first."
