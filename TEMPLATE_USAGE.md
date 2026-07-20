# Using this template

`smartsquared/seedling-product` is a GitHub template repository. New **products of the sakuma process** start from here.

Not sure this is the right template? Use the sibling [smartsquared/seedling-component](https://github.com/smartsquared/seedling-component) for new *ecosystem members* (substrates, infrastructure, methodology repos). The test: components serve the pipeline; products serve users. If it has a brief and an audience outside the ecosystem, it's a product.

## Create a new repo from this template

Products can live in **any org** — personal orgs are fine; they don't need to live in smartsquared.

```bash
gh repo create <owner>/<new-repo-name> \
  --template smartsquared/seedling-product \
  --private \
  --description "<one-line description>"

# Auto-delete head branches after a PR merges (ecosystem default — template
# settings don't carry over to repos created from it, so set it explicitly).
gh repo edit <owner>/<new-repo-name> --delete-branch-on-merge
```

Then clone locally:

```bash
cd ~/Code && gh repo clone <owner>/<new-repo-name>
```

## Fill in the placeholders

The template ships with `<placeholder>` markers in:

- `README.md` — including the **provenance table** (brief, wave plan, driving loop)
- `CLAUDE.md`

Each file has an HTML-commented checklist at the bottom describing what to fill in. Delete the comment block once placeholders are replaced.

## Set develop as default

The template's default branch is `main`. Per ecosystem gitflow, products use `develop` as default:

```bash
cd ~/Code/<new-repo-name>
git checkout -b develop && git push -u origin develop
gh api -X PATCH repos/<owner>/<new-repo-name> --field default_branch=develop
```

## Apply branch protection

Match the ecosystem baseline (PR-required, no force push, no deletions, 0 approvals, admins exempt):

```bash
for branch in main develop; do
  gh api --method PUT "repos/<owner>/<new-repo-name>/branches/$branch/protection" \
    --input - <<EOF
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
EOF
done
```

## Register provenance (NOT the ecosystem map)

Products are **not** ecosystem members — do not add a row to atlas/ecosystem.md unless the product is a methodology validation target (like clonezone), in which case it goes under "Validation targets".

Instead:

- Fill the provenance table in the product's README (brief, wave plan, driving loop).
- If a sakuma loop is driving the build, add a section to [sakuma/loops/INDEX.md](https://github.com/smartsquared/sakuma/blob/develop/loops/INDEX.md) and link the product repo from it.

## Delete this file

Once the new repo is live and the placeholders are filled in, delete `TEMPLATE_USAGE.md` — it's only relevant for the bootstrap.
