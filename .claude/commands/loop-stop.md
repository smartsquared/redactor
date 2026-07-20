Cancel a named polling loop. Idempotent — calling when nothing is running is a no-op, not an error.

## Usage

`/loop-stop [name]`

## What this does

- `name` given → cancel that named loop's queued wake-up
- `name` omitted, exactly one loop active → stop that one
- `name` omitted, multiple loops active → list them and ask which to stop
- No active loops → no-op (success, not error)

## What it does NOT do

- Stop loops started by other commands (e.g. core `/loop` if invoked from Claude Code core)
- Mutate non-loop session state

## Mode gate

Refuses outside execute mode (consistent with `/loop-start`).
