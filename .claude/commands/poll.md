Wrap `agent-lab poll`. Execute-mode only.

## Usage

`/poll`

## What this does

Shells out to `agent-lab poll`, reports finished tasks, surfaces failures distinctly from successes.

## What it does NOT do

- Reimplement polling logic — `agent-lab` CLI is the source of truth.
- Self-perpetuate. For continuous monitoring, use `/loop-start poll` instead so you have a clean `/loop-stop` (#388).

## Mode gate

If invoked outside execute mode, refuse with: "Poll is execute-mode only. Run /execute first."
