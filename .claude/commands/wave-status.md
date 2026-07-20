Composite read-only situational awareness for an in-flight wave. Available in plan and execute modes.

## Usage

`/wave-status`

## What this does

Returns a single-screen summary composed of:

- `agent-lab pool status` — worker state across the fleet
- `gh pr list --milestone <current> --state open` — open PRs in the active milestone
- `gh issue list --milestone <current> --state open` — remaining open issues
- Tasks idle/stuck > 30 minutes (from `agent-lab status` per task in the pool)

Output formatted for quick scanning: pool table, open-PR list, remaining-issue list, stuck-task callouts.

## Determining "the current milestone"

- Working dir is on a `feature/m<NN>_<slug>` branch → milestone is `m<NN>_<slug>`.
- Otherwise: ask which milestone, or fall back to the most-recent milestone with open issues.

## What it does NOT do

- Dispatch, merge, or mutate anything — strictly read-only.
- Replace `agent-lab pool status` for deep diagnostics — this is the at-a-glance composite.

## Mode gate

Available in plan and execute modes. Refuse in discuss mode (discuss intentionally avoids tooling output that pulls toward action).
