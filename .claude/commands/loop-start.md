Start a named, trackable polling loop. Replaces implicit-self-pacing `/loop` behavior. Execute-mode only.

## Usage

`/loop-start [interval] <prompt>`

- `interval` — optional; default 60s for short-lived work (poll), 300s for long-running observation
- `<prompt>` — the loop body. Canonical case for agent-lab is `poll`.

Examples:

- `/loop-start poll` — pool monitoring at 60s
- `/loop-start 300 poll` — pool monitoring at 5min
- `/loop-start /wave-status` — periodic wave-status snapshot

## What this does

- Records the loop in session-local state with a generated name (or accepts `--name <name>`)
- Schedules the first wake-up
- On each wake, re-fires the prompt and re-schedules
- Continues until `/loop-stop` is called

## What it does NOT do

- Run forever — explicit `/loop-stop` required
- Self-perpetuate without lifecycle (the bug `/loop-start` solves; #388)

## Mode gate

Refuses outside execute mode. Polling implies a fleet to monitor; that's execution-flavored.
