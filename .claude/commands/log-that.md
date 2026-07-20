Capture the current work or decision in the ledger (`~/Code/ledger`), per the logging
protocol (`~/Code/ledger/docs/logging-protocol.md`). This is the **human trigger** —
the user is telling you something just happened that the ledger must reflect. Never
refuse on the grounds that it "doesn't meet the bar"; the human invoking this IS the
bar.

Arguments (optional): a hint about what to log. If absent, log the most recent unit of
work or decision in this conversation.

Steps:

1. Identify the unit: a goal being worked (mutations happened) or a decision that
   constrains future work (discussion landed somewhere).
2. Check for an open record it belongs to:
   `cd ~/Code/ledger && ./tools/ledger list --status in-progress`
3. **If an open record covers it** — update that record file: append to its
   Approach/Output/Notes as appropriate. Do not open a sibling record.
4. **If not** — scaffold one: `cd ~/Code/ledger && ./tools/ledger record <kebab-slug>`,
   then fill in the four body fields (Intent / Approach / Output / Notes) and the
   pushed frontmatter (`title`, `unit`, correct `ai` model id, `surface`). For a
   decision-record from a discussion, the decision goes in Output. Leave cost/token
   fields at zero — the harvesters fill those; never hand-type them.
5. Point `sources.transcript` at the current session's transcript file under
   `~/.claude/projects/` if you can identify it.
6. Reindex and commit in the ledger repo:
   `cd ~/Code/ledger && ./tools/ledger index && git add records/ && git commit`
   Commit message: `record: <slug> — <one-line>`. Records are corrected by new
   commits, never silent rewrites.
7. Confirm to the user in one line: which record, opened or updated, and the path.

Notes:
- This works from ANY repo — records always live in `~/Code/ledger`, regardless of
  where the work happened.
- If the ledger repo has uncommitted unrelated changes, commit only `records/` and
  `index.db` is gitignored — never commit it.
- If invoked mid-goal, the record stays `in-progress`; if the user is logging a
  finished thing, set `status: complete` and `ended` (UTC now).
