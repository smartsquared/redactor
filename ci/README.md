# ci/

Holds the intended GitHub Actions workflow, staged **outside** `.github/workflows/`.

## Why it's here and not in `.github/workflows/`

The agent that authored this branch (`deft-owl`) pushes with a token scoped to
`repo` only — not `workflow`. GitHub refuses to create or update any file under
`.github/workflows/` without the `workflow` scope, so the agent could not push
the workflow to its canonical path.

## Operator followup (one step)

Move the file into place and commit it with a `workflow`-scoped token:

```bash
git mv ci/build-and-test.yml .github/workflows/build-and-test.yml
git rm .github/workflows/.gitkeep   # placeholder no longer needed
git commit -m "ci: install build-and-test workflow (S0.1)"
```

The check is named exactly `build-and-test` (the ecosystem pre-flight greps for
it). It runs `ruff check .` + `pytest` on PRs and on pushes to `develop`/`main`.
