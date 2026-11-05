---
name: decisions
description: Work the answered decision requests and READY agent tasks waiting in this repo. USER-INVOKED ONLY - use when the user explicitly asks to work, pick up, or handle decisions, answers, or tasks. Do NOT invoke it because a session-start notice mentioned waiting answers or tasks, or because .claude/decisions/INDEX.md has ANSWERED or READY rows; those are notices, not requests. Spawns one subagent per item (or, with --here, works a single item in place), then closes each out.
---

# Work answered decisions and assigned tasks

Two kinds of item wait here for an agent: decisions a human has answered (via the
dashboard, Linear, or chat — the answer is already written into the repo), and tasks
the human assigned to an agent. Your job is to carry out the work each one calls for,
then close it out.

Only run this when the user asked for it. Seeing that items are waiting is not the
same as being asked to work them — this spawns subagents and will displace whatever
the session was actually opened to do.

## 1. Find what is actionable

Read the top of `.claude/decisions/INDEX.md` — that's the whole query:

```
grep -E '^\| (ANSWERED|READY) \|' .claude/decisions/INDEX.md
```

Columns are `| status | id | title | file |`. Files live in `.claude/decisions/`.
ANSWERED rows are decisions with an answer waiting; READY rows are tasks.

- A bare `/decisions` works both. `/decisions answers` works only the ANSWERED rows,
  `/decisions tasks` only the READY rows, and `/decisions #<id>` (or naming items in
  words) only the items named.
- Nothing of the kind asked for → say so ("no answered decisions or READY tasks
  waiting") and stop. Do not go looking in the database or the dashboard.
- **No `.claude/decisions/` here at all** (typically a session started in `~` or some
  folder that isn't a project): don't just stop. Run
  `apo pending --all`, which
  lists every project with answers or READY tasks waiting, its folder, and the command
  that works them there. Show the user that list and stop. Never work another repo's
  decisions from here — open a session in that folder instead (or use **Apply in
  background** on the dashboard).
- **OPEN rows are not yours.** They are still waiting on a human. Never act on one,
  and never re-ask a question that is already sitting there open. WAITING rows wait
  on someone outside or on another item finishing first, and IN PROGRESS rows are
  claimed by another session; leave them too.

**Claim each READY task** with `claim_task(task_id=<id>)` *before* spawning its
subagent — if the claim returns false, another session has it; skip it. When the task
is done, close it with `complete_task(task_id=<id>, note="what you did")`.

**If those tools don't exist in this session** (a resumed session keeps the tool list
it was created with, so one older than the task tools won't have them), use the CLI
instead — same effect: `apo claim <id>` (exit code 1 = someone else has it, skip it)
and `apo complete <id> --note "what you did"`. When only part of it is done, pass `outcome="partial"` with `not_done` (and
`done`, `left_for_human`, `branch` if useful) so it stays flagged as partly done.
Work committed on a branch (a worktree's, say) gets `branch=<its exact local branch
name>` either way: the item's page then offers to merge it with one click.
Never leave finished work unclosed. The task's file has the description and,
if it belongs to an outcome, where it fits.

## One item, in a session started for it (`--here`)

When the dashboard starts a background session for a single item ("Start a background
session on it now", **Apply in background**), its prompt is `/decisions #<id> --here`.
That session exists only for this item, so **do not spawn a subagent**: it would start
cold and need the whole record re-explained, paying for a second context to do what
this one can. Work the item yourself, in this session:

- A READY task: `claim_task(task_id=<id>)` first (false means another session has it;
  stop). Read its file, do the work, then `complete_task(task_id=<id>, note="...")`.
- An ANSWERED decision: read its file (question, answer, resume plan), implement the
  answer as given, then `mark_decision_resolved(decision_id=<id>)`.

Everything in sections 1 and 3 still applies: work only that item, follow the repo's
conventions (TDD if it calls for it), and if the answer can't be implemented as stated,
stop and say why without resolving it. Don't file the same question again.

Without `--here`, or with more than one item, spawn subagents as below.

## 2. Spawn one subagent per item

Read each item's file first — you need it to write the subagent's prompt: for a
decision, its answer and resume plan; for a task, its description and, if it belongs
to an outcome, where it fits.

Items whose work touches **disjoint** files: spawn together, in parallel. Items that
touch the **same** files: run one at a time, or the second will be working against a
tree the first is still changing.

Give each subagent everything it needs, because it starts cold:

- For a decision: the original question and the human's answer, quoted verbatim, and
  the resume plan (objective, files, remaining steps, expected outcome).
- For a task: its title and description, quoted verbatim, and the outcome it serves.
- Where the item's file is, so it can re-read the full record.
- What "done" means for that item.

Tell each subagent explicitly:

- Implement the answer or task as given. A decision is settled — it is not being asked
  to re-evaluate the human's choice or re-litigate the options.
- If it turns out to be unimplementable as stated, stop and report why rather than
  inventing a different approach.
- Do **not** file a new decision request, do **not** mark anything resolved, and do
  **not** complete a task. Those are the main session's job. (A subagent filing its own
  decision request is how you get a loop.)
- Follow this repo's own conventions — including TDD if the project calls for it.

## 3. Close each one out

After a subagent reports back, verify the work is actually there (tests run, files
changed as described). Then mark that decision resolved:

- `mark_decision_resolved(decision_id=<id>)` via the apo MCP
  server, or
- `apo resolve <id>`

This is what flips the row to DONE and stops the session-start nudge. A decision whose
work is finished but which was never resolved will keep nudging every session forever,
so do not skip it.

**Only resolve what actually got done.** If a subagent stopped early, could not finish,
or reported a problem: leave that decision ANSWERED, and tell the user what blocked it.
Resolving it would silently bury the unfinished work.

A finished READY task closes with `complete_task(task_id=<id>, note="<what you did>")`
instead. One that couldn't be finished stays claimed; tell the user why. (If this
session ends, the claim is released and the task shows READY again.)

## 4. Report

One short summary per item: what the answer or task was, what you changed, and whether
it is closed or still open and why. Commit only if the user asks or the repo's
conventions call for it.
