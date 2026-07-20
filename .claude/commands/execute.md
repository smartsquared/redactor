You are now in **execute mode** for the rest of this session.

## Mode: execute

In execute mode, your job is to ship the plan that already exists. You dispatch agents, merge PRs, edit code, and run the mechanical work of the milestone — but you do not re-plan, re-scope, or open exploratory threads.

### Execute mode DOES

- Run `agent-lab dispatch`, `agent-lab poll`, `agent-lab milestone run`
- Merge PRs (per the gitflow merge-commit rule — never `--squash`)
- Edit source code, write tests, commit, push
- Apply the escalation protocol when something blocks
- File NEW issues only when an in-progress task reveals a real blocker

### Execute mode does NOT

- Re-plan milestone scope — that's /plan work
- Author user stories or rewrite milestone descriptions — same
- Hold open-ended exploratory conversations — that's /discuss work

### Ledger boundary checks

Per the logging protocol (`~/Code/ledger/docs/logging-protocol.md`):

- **Before your first mutation** (file edit, commit, dispatch, issue): check `cd ~/Code/ledger && ./tools/ledger list --status in-progress` for an open record this goal belongs to. Reattach if one exists; otherwise open one (`./tools/ledger record <slug>`) and fill Intent/Approach.
- **Stamp the board at boundaries** (thread-state protocol): on reattach/open, after a
  claim, on getting blocked, and at close — `./tools/ledger state <slug> "one line, present
  tense"`. The `list` output above shows every other thread's `now:` line — read it before
  claiming shared work; if you've been idle >4h, re-read before your next mutation.
- **When a goal completes or is abandoned**: update the record's Output, set `status` and `ended`, reindex, commit in the ledger repo.
- Never create records on a clock (message count / timer) — only at these boundaries or via `/log-that`.

### When in doubt

If a request feels strategy-flavored ("should we even build X?", "what's the right approach?"), push back: "That's /plan or /discuss work. Want to switch?"

Acknowledge with a single word: **execute**.
