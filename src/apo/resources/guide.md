# apo user guide

apo is one place to see every Claude Code session you're running, across every project, and to handle what they need from you: a decision, an action only you can take, something to review. Agents file those requests instead of stopping. You answer when it suits you. The answer goes back into the repository, where the next session picks it up.

It costs no LLM tokens to run. Everything you see comes from Claude Code's own hooks and its `claude agents` list.

## Finding your way

The top bar has four places:

- **Now:** everything that needs you, across all projects.
- **Projects:** the home. Each project has its own page with tabs.
- **Search.**
- **Settings.**

Everything under **Projects** has the same layout, with two controls that are always there:

- **The sidebar picks the scope:** *All projects* at the top, then each project (pinned first, worst health next).
- **The tabs pick the view:** **Overview · Work · Decisions · Agents · Activity** (`1`–`5`).

Each keeps the other: choose a project in the sidebar and you land on the same tab for that project; choose a tab and you stay in the same scope. So "acme → Decisions" and "All projects → Decisions" are the same page, filtered or not. `[` and `]` step through All projects and each project, staying on the tab. On a phone the sidebar becomes a project picker at the top.

| Tab | All projects | One project |
|---|---|---|
| Overview | Every project's health and counts, and every goal | Brief, status, goals, what needs you |
| Work | Everything open across projects, by where it stands | The project's backlog |
| Decisions | What's waiting on you, and the log of everything decided | The same, for the project |
| Agents | Every session, live | The project's sessions |
| Activity | Everything that happened | The project's history |

## The ideal way to work with it

### 1. Start every session in its project's folder

```
cd ~/projects/acme && claude
```

**The folder is the project.** A session started in `~` belongs to no project:

- what it files has nowhere to go back to;
- `/decisions` there finds nothing, because answers live in each project's `.claude/decisions/`;
- it shows up as "~ (no project)".

**Resume in the same folder too.** `claude -r <id>` from `~` runs the session in `~`, even though it belongs to its project. apo still files it under the right project (it reads where the transcript lives) and tells the agent where its project is, but `/decisions` only works in the project's folder. Use `cd ~/projects/acme && claude -r <id>`; the resume commands apo gives you already do this.

**Start a fresh session now and then.** A resumed session keeps the tools it was created with, so one started before an apo upgrade won't have newer tools such as `claim_task`. The `/decisions` skill falls back to the CLI (`apo claim` / `apo complete`) when they're missing. A fresh `claude` gets everything.

The first session in a folder registers the project; there's nothing to set up. If an agent named it something you don't recognise (`~/projects/acme-web` showing as "Storefront"), **Rename** it on its project page. The Projects list shows the folder name whenever it differs from the project name.

### 2. Say what the project is for

On the project's **Overview**, write the **brief**: its **purpose** (why it exists, in a line or two) and its links (repo, site, Linear). How you'll know you're done belongs to each goal. The brief rarely changes, and agents read it before planning.

Then start **goals**: the concrete results you're working toward, each with a checklist of steps, every step owned by you, an agent, or someone else. A project usually has several over time ("Launch the listing", then "First 10 customers"). Agents can start goals and add steps too. That's what turns a pile of sessions into progress you can see.

### 3. Let agents ask instead of stop

Agents file a **decision** (pick an option) or an **action** (something only you can do) through the MCP server. Then they keep going on whatever's safe. You don't watch terminals: Now, and notifications if you turn them on, tell you when you're needed.

For work that doesn't need you at the keyboard, add it under **New work** on the project's Work tab (a task or a bug) and tick *Start a background session on it now*. Every session started from the dashboard goes through a work item, so what it was asked to do stays on the Work tab.

### 4. Answer from Now

- Most items are one click. With a few minutes free, **I have 5 min** runs a lightning round.
- For an action: do the thing, then press **Done**. If it has a check, the check runs first.
- **Snooze** what can wait.
- Answer each item in **one** place. If you settle something in a chat with the agent, the agent records it with `answer_decision_request`, so the dashboard and the repo stay in step.

### 5. Get the answer applied

| The session that asked is… | Do this |
|---|---|
| Still open | Just prompt it ("go"). It receives the answer automatically on your next message |
| Closed | **Apply in background** on the item, or open a session in that project's folder and run `/decisions` |
| You're not sure where it is | `apo pending --all` lists every project with answers waiting, its folder, and the exact command to run |

Never run `/decisions` from `~` or from another project's folder. It only works the folder it's in, and it'll point you to the right one.

### 6. Review what finished

The **Review** lane holds turns that finished while you weren't looking. Read the recap and reply to the session, or press **Reviewed**. When an outcome's steps are all done, mark it **achieved**. Archive a project you're finished with.

### 7. Once a week

- **Weekly report** (Projects → Weekly report, or a project's Overview): per project, what got done and decided, what's blocked on you or overdue, how long agents waited on you, and commits. **Copy as Markdown** for a status update, or **Print**.
- **Projects:** anything red or amber, anything dormant you meant to keep going.
- **Time in your court** on Now: are agents waiting on you more than they should?
- **Activity** for what happened.

### Usage limits

A session that's still open continues by itself when your usage limit resets: Claude Code's *Continue automatically at usage limit* setting, in `/config`. Now counts it as *waiting for the usage limit to reset*, not as a problem.

If you closed it in the meantime, it shows in Your move with:

- **Resume at** — prefilled with the reset time Claude Code reported, so it starts by itself then;
- **Resume now**.

### Don't

- Start sessions in `~`.
- Put passwords or API keys in answers: they're written into the repo. An action's secret fields are the safe place.
- Answer the same item in two places.

## The daily loop

1. **Open Now** (`/`, or `g` then `n`). It's everything that needs you, across all projects, best move first.
2. **Clear what you can.** Most items are one click: accept the agent's recommendation, pick an option, or mark an action done. With a few minutes free, press **I have 5 min** (or `t`) for a lightning round.
3. **Check Review** for turns that finished while you weren't looking. Prompt that session, or press **Reviewed**.
4. **Leave.** Turn on notifications in Settings and apo tells you when something has waited long enough to matter, within a daily budget.

When you come back after 30 minutes or more, Now opens with **Since you were away**: what was filed, answered and finished, and each project's latest recap. **Mark seen** clears it.

## What's in it

**Projects.** One per folder. A session belongs to the project of the folder it runs in. A git worktree counts as its main repository, so a project's worktree sessions show up under it. Two names for one folder (like `demo_diagrams` and `demo-diagrams`) can be merged.

**Sessions.** Each Claude Code process. Its status comes from the hooks:

| Status | Means |
|---|---|
| Running | Doing a turn |
| Needs permission / Needs your input | Waiting in its terminal for you |
| Needs decision / Needs action | Waiting on an item it filed for you |
| Usage limit | Hit your plan's usage limit. Claude Code continues it by itself when the limit resets (its "Continue automatically at usage limit" setting, on by default) |
| API error | The turn stopped on an API error and won't continue by itself |
| Idle | Finished its turn, sitting at the prompt. Normal, not an alarm |
| Quiet (a flag on Running) | Nothing heard for a while: a long command, or hung |
| Ended / Crashed | The process is gone (crashed means mid-turn) |

**Items.** What agents (or you) put in front of someone:

| Kind | Whose move | What it is |
|---|---|---|
| Decision | Yours | Pick between options. Usually comes with a recommendation |
| Action | Yours | Something only you can do: add a DNS record, merge a PR, grant access |
| Review | Yours | Look something over |
| Task | An agent's | Work for the next agent session in that repo |
| Wait | Someone else's | Shows up for you only when its follow-up date passes |

**Outcomes.** A result you want ("Launch the site"), broken into steps split between you, agents and other people.

## Now

The page has lanes:

- **Problems:** faults only you can recover from. A session that crashed mid-turn or stopped on an API error, Linear sync failing, two projects claiming one folder.
- **Your move:** ranked by how blocked the agent is, times how long it has waited, divided by how long it'll take you. Each item shows its rough time (`~2m`).
- **Asks:** requests whose agent is still working on other things meanwhile. Not urgent yet.
- **In transit:** answered, on the way to an agent.
- **Review:** finished turns you haven't looked at.

At the top:

- **The summary line:** how many items are waiting on you and roughly how long they'll take.
- **Time in your court:** how long agents waited on you today (added up across items), your median answer time this week, and the oldest move you're holding.

The list doesn't reshuffle under your pointer. New things show as an **Updates** pill; press `r` to apply them.

Add `?project=name` to the URL (or use **Open in Now** on a project's page) to see one project only.

### Answering

A decision shows the agent's **Suggested** answer and how sure it is. Then:

- **Accept recommendation** (`y`): go with what the agent suggested.
- **An option** (`1`–`9`): each option says what the agent will do and whether it's easy to reverse (⇄) or not (⟶).
- **You decide:** let the agent use its judgment, preferring what's easiest to undo.
- **Need more info:** write what you need first. The agent files a new, better question.
- **Decline:** don't do it.
- **Your own words:** the text box. Anything you type alongside a quick answer goes with it as a note.

You can paste, drop or attach screenshots (up to 6). They're copied into the repo with the answer.

If what you type looks like a password or API key, you're asked to confirm first, because answers are written into the repository as plain text.

### Actions

An action has **Done** (`e`), **I'm stuck…** (write what's unclear first), and **Can't do this**.

An agent can make an action precise:

- **a link** to where you do it;
- **steps**;
- **values** with **Copy** buttons;
- **fields** for what it needs back from you;
- **Done when**: how you know you've finished.

If it includes a **check** (a DNS record exists, a URL answers, a file or `.env` key is present, a PR is merged), **Done** runs it first. If the check fails you see why, and **Mark done anyway** is one click.

A field marked secret (an API key, a webhook secret) goes into the encrypted local store, never the repo. The agent fetches it with `get_action_secret`.

### Snooze

**Snooze** takes an item out of the queue for 2 hours, until tonight, tomorrow 9:00 or next week. **Until it blocks** hides it only while the agent that asked keeps working; it comes back the moment that agent is stuck on you.

## After you answer

Your answer is written into the repository at `.claude/decisions/` (an `INDEX.md` plus one file per item). From there:

1. **The session that asked hears it** on your next prompt there. You'll see "Answered #31: …" in its context. Say "go" there and it applies it.
2. **Any other session in that repo** is told at start-up that answers are waiting. Run `/decisions` in it to apply them.
3. **If nothing is running in that repo**, the item says so. After an hour it comes back to Your move as *answered, but nothing will pick it up*. Then:
   - **Apply in background** starts a session for you (below).
   - Or copy the **resume command** and run it yourself.

Each item's delivery is tracked: *pending*, then *delivered* (the session was told), then *applied* (the work was done and closed).

When an agent closes an item or a task from the repo, the item says where the work is: its branch and commit, and whether that's merged into the default branch yet ("Work is on branch `fix-nav` (commit abc1234) — not merged into `master`"). Work done in a worktree or on a side branch isn't in your checkout until it's merged. Closing an item yourself from the dashboard records nothing.

### Apply in background

**Apply in background** runs `claude --bg` with `/decisions #N`:

- If the session that asked has ended, it resumes that session, so the agent has its full context.
- If that session is still open in a terminal, it starts a new background session instead. Two conversations in one transcript would interleave.

Only answers you gave here, from the CLI, or in chat start a session with one click. An answer that came in from a Linear comment needs **Approve and apply**, because that text came from outside this machine.

**What a background agent may do:** by default, exactly what a `claude` you open in that folder may do: apo passes no permission flags. A project's permission level (on its Overview, under **How agents work here**) can loosen or tighten that for the sessions apo starts there: **Accept file edits** passes `--permission-mode acceptEdits` (edits go through; commands still ask), **Plan only** passes `--permission-mode plan` (it reads and plans, changes nothing). Modes that skip permission checks altogether aren't offered. With default settings it reads and searches freely, and stops to ask before every file edit, shell command and tool call that your settings (user-level or the repo's `.claude/settings*.json`) don't already allow. A background session that stops to ask waits for you on the dashboard: its card asks **Allow** / **Deny** for a permission, and links to its page for a question (pick the options, or answer in your own words) or a plan (**Approve the plan**, or **Keep planning** with what to change). Your answer goes back to Claude Code as if you'd given it in its own dialog. **In the terminal** hands the prompt back to Claude Code's dialog instead, for `claude attach <id>`; so does an hour without an answer. Sessions running in a terminal or an editor are never held: their own dialog shows straight away.

## Lightning round

**I have 2 / 5 / 15 min** (or `t` for 5) fills that much time with your moves: the most valuable per minute first. It always includes at least one item.

The round shows one card at a time:

| Key | Does |
|---|---|
| `↵` | Accept the recommendation, or Done for an action |
| `1`–`9` | Pick that option |
| `d` | You decide |
| `a` | Write an answer |
| `s` | Snooze until tomorrow |
| `n` | Skip |
| `Esc` | Leave |

The plan is fixed when the round starts. Answering doesn't pull new items into the time you chose.

## Accept in bulk

When agents are sure, you shouldn't have to click through each one. **Accept in bulk** (`b` on Now, or `accept all` in the palette) lists the open decisions where:

- the agent is at least 85% sure, and
- it marked its pick as **easy to reverse**.

Unknown reversibility counts as a one-way door and never shows up here. Uncheck anything you want to look at properly, then accept the rest.

## Projects

**Projects → All projects → Overview** (`g` then `p`) has one row per project:

- **The dot** is the worst thing going on in it, never an average: red problem, amber your move, violet review, blue in transit, green working, grey at rest or idle, and a hollow dot for dormant.
- **The counts:** `2 you · 1 review · 1 working`.
- **Timing:** how old the oldest open item is, and when anything last happened.

**Pin** (☆, or `p` on a selected row) keeps a project on top. Pinned projects also get notifications straight away instead of after 30 minutes.

**Dormant** projects (nothing open, nothing running, no activity for 14 days) fold away. Anything new wakes them.

A project's page is where you work on it. Its header shows the health dot and the **status**; its tabs (`1`–`5`) hold the rest.

**Status** is suggested from what's going on, and you can override it on the Overview (**Status and priority**) with a note:

| Status | Suggested when |
|---|---|
| On track | Work is open and moving |
| At risk | Something is past its due date, or work is open but nothing has moved for 5 days |
| Blocked | Something has waited on you for 2 days |
| Quiet | Nothing is open |
| Paused / Done | Only when you set it |

**Overview** has:

- **what it's for**: the project's purpose and links;
- **goals**: concrete results, each with its done-when, a checklist of steps and its progress, and the switch for [Goals from Linear](#goals-from-linear);
- what needs you here;
- **How agents work here**: what background sessions may do without asking, and the standing rules (both below);
- **What changed**: the last 7 days of commits and pull requests, by day (see below);
- what happened recently;
- **Status and priority**, saved together;
- an at-a-glance count of the work.

**What changed** reads `git log` in each of the project's folders (commits on any branch, so work an agent hasn't merged yet counts) and, where the folder has a GitHub remote and `gh` is signed in, its pull requests: merged, open and drafts. It's local and read-only. Pull requests are fetched in the background every 15 minutes, so the first visit to a project may show commits only. What's new since you last looked has a dot; a "look" lasts until you've been away for 30 minutes, so refreshing doesn't clear it. The Activity tab lists the commits among each day's events.

**Priority** (on the Overview, with the status) is hot, normal or background:

| Priority | Now | Notifications |
|---|---|---|
| Hot | Its moves rank twice as high | Interrupt as soon as they arrive, like a pinned project |
| Normal | As is | As usual |
| Background | Its moves rank half as high | Never interrupt; they wait in Now |

**Weekly report** (a button on the Overview; **Weekly report** on All projects for every project at once) covers the 7 days ending today, and **Previous week** steps back. For each project: what got done (tasks, bugs, actions, goals achieved), what was decided and the answer, what's blocked on you or overdue at the end of the week, how many hours agents waited on you and your median answer time, how much was filed, done per week over the last 4 weeks, and commits and merged pull requests. The same week always reads the same, so the link (`?end=YYYY-MM-DD`) is a permanent record. **Copy as Markdown** puts it on the clipboard; **Print** gives a clean page.

**Standing rules** (on the Overview, under How agents work here) are answers you'd give every time, written down once: "Pick the cheapest reversible option without asking", "Use Postgres for anything new". Agents get them in the project brief and in `.claude/decisions/POLICY.md` in each of the project's folders, and follow a rule instead of filing a decision. Add one by hand, or with **Make this a standing rule** on an answered decision (it starts from your answer; edit it before saving). When you've accepted the agent's recommendation, or told it to use its judgment, on at least 80% of a project's recent decisions (5 or more in 60 days), the Overview suggests a rule that lets agents go ahead when they're confident and the choice is easy to undo. **Retire** a rule and agents ask again. It's the one thing here that makes fewer questions reach you, rather than helping you answer them faster.

**Due dates** go on goals (when you start one, or on its page) and on any item: a task, a bug or your own action (on its page, or when you add it under New work). They show as a badge: **Due Fri**, **Due tomorrow**, **Overdue 2d**. Anything open and past due makes the project's suggested status **At risk**.

**Work** is one list of everything in the project, whoever holds it:

- **Needs you:** your decisions, actions and reviews.
- **Ready for an agent:** tasks and bugs nobody has claimed yet.
- **In progress:** claimed tasks, and answers on their way to an agent.
- **Waiting:** blocked on another item, or on someone outside.
- **Done this week.**

A **Do it** row (an action of yours) has its own **Done**, so something you've already done closes without opening it, and you stay on Work. `e` does the same for the row you've picked with `j`/`k`. If the action has a check, it runs first; if it fails, the item opens with why, and **Mark done anyway**.

**New work** on the Work tab is the one way to add something to a project (`a` jumps to it). Pick who it's for:

- **An agent:** a READY task in the repo's `INDEX.md`, so the next session (or `/decisions`) picks it up.
- **A bug** (`b`): what's wrong, and optionally what happened, what you expected, how to reproduce it and how bad it is. It's a task for the project's agents, labelled bug.
- **Me:** it lands on your Now.

Any of them can go under one of the project's goals and have a due date. For an agent's task or a bug, tick **Start a background session on it now** to have one start straight away, with the project's permission level.

When a project has more than one folder (say a root folder and a `dashboard` subfolder), New work and the goal page's **Add a step** ask which **Folder** the work belongs in. It starts on the folder a session last worked in. Agents only see work through a folder's `.claude/decisions/INDEX.md`, so this decides which sessions find it. To move an item later, use **Move to folder** on its page, or `apo move <id> <folder>` (a path, or the folder's name). An item with no folder shows **No folder** on its page and "no folder chosen" in `apo pending --all`.

**Decisions** has what's waiting on you, then the **decision log**: every answered decision with its question, the answer, when, and who answered. Filter it with `/`. It's how you avoid deciding something twice.

**Agents** has the project's session cards with their checklists, and sessions that ended recently. To start a session, use New work on the Work tab.

**Activity** is the project's event history, with repeats collapsed, and its commits among each day's events.

Also on the Overview: **Rename**, **Merge into another project** (everything moves; this project's name becomes an alias of the other) and, in the header, **Archive** (closes what's still open and hides the project; nothing is deleted).

## Agents

**Agents** (`g` then `s`; All projects → Agents) shows a live card per session, grouped by project. A project's own sessions are on its **Agents** tab. It updates as soon as a session does something: the dashboard hears about every change the hooks record and tells open pages straight away (only the tab you're looking at listens; a hidden tab catches up when you come back). A card shows:

- what you last asked it;
- what it last did;
- what it's blocked on, if anything;
- its checklist;
- how much context it carries (**151k**): the tokens its next turn sends the model again. A big number is a reason to start the next piece of work in a fresh session rather than continue this one.

On a card:

- **Details** opens the session's own page (below).
- **VS Code** opens the session in VS Code's Claude Code panel. From WSL, VS Code needs to be connected to this distro.
- **Stop** (background sessions only) stops it. Its conversation is kept, and `claude attach <id>` reopens it.

A session's page has the whole conversation from its transcript: what you asked, what it said, the tools it ran (folded, one line each), and Claude Code's own recaps. Subagents' chatter and tool output are left out, and a very long session shows its latest part. **Generate recap** writes a fresh summary of it with a small model (Haiku) through your Claude Code sign-in, so no API key is needed. The call runs with hooks, tools and MCP servers off and isn't saved, so it never shows up as a session itself. The recap replaces the card's "last done".

**Continue** at the bottom of the page replies to a session that has finished its turn:

- **Continue this session** resumes it with your message (a background session that's still idle is stopped first, so two processes never share one conversation). It keeps everything it knows, and its next turn re-reads all of it.
- **Continue in a fresh session** starts a new one from a short handoff: your message, what this one was asked, its recap, and where its transcript is if a detail is needed. It's a task on the Work tab like any other dashboard start.

The page recommends one. Fresh once the session carries 100k tokens or more, or has been idle longer than the prompt cache lasts (about 5 minutes), since then continuing re-reads everything at full price; otherwise continuing, which is cheap and keeps the context. A session still open in a terminal or editor is answered there, and one in the middle of a turn can be replied to once it finishes.

## Goals

**Goals** (outcomes; `g` then `o`) live on the **Overview**: all of them on All projects, a project's own on its Overview. They're results you want with a checklist. Each step has an owner (you, an agent, or someone else), and steps can wait on each other.

- A step that's blocked stays out of Now until what it waits on is done.
- Agent steps become **tasks** in the repo's `INDEX.md`, so the next session (or `/decisions`) picks them up.
- When every step is done, the outcome shows up in Review: **Mark achieved**, or add more steps.

Agents can create outcomes and steps too.

### Goals from Linear

You can plan a goal in Linear instead: create an issue and break it into sub-issues. apo turns the issue into a goal and its sub-issues into the goal's steps, and a step you add here becomes a sub-issue there.

Turn it on per project, on the project's Overview: **Goals from Linear → Turn on**. It's off by default, because a step from Linear can be work for an agent. It needs Linear connected in Settings, with sync on.

- **Which Linear project:** the one with the same name as the apo project, which is where its agents' decisions already go (if it doesn't exist yet, create it in Linear). To use another one, choose **Choose another Linear project** in the same panel and pick from your team's projects. The import then starts again from that moment. Once found, the project is remembered, so renaming the project here doesn't lose it. apo remembers which Linear workspace you connected. Reconnecting to the same workspace, even after a disconnect, keeps everything. Connecting to a different workspace changes four things, since the old workspace's projects and issues don't exist there: every project forgets its Linear project and falls back to the one with its name in the new workspace; the import restarts from that moment, as turning the switch on does, so nothing older in the new workspace comes in; goals imported from the old workspace, and agents' requests already pushed there, are unlinked, so they stay here with their steps and answers but stop syncing (an unlinked request isn't pushed again); and each affected project's Activity says how many goals and requests were unlinked. The switch stays on or off as it was. Decisions still go to the Linear project with the project's current name.
- **Which issues:** only ones you created, with the Linear account apo connects as. Issues and sub-issues a teammate or an integration creates never come in, because an imported step can be work for an agent. apo also skips:
  - issues it filed itself (titled `[AI] …`);
  - issues that are already done or canceled;
  - sub-issues of sub-issues: only the issue's direct sub-issues become steps.
- **From when:** only issues created after you turn it on. Turning it off and on again starts over from that moment, so nothing filed while it was off comes in.
- **The goal** takes the issue's title and description, and links back to the issue on the goal page and on its card.
- **The steps:** each sub-issue becomes a step, in the order they were created, including sub-issues added later. A canceled sub-issue is skipped. Linear decides who owns each step:
  - a label `agent` makes it an agent's; a label `me` or `human` makes it yours;
  - with neither label, the assignee decides: you → yours, someone else → a wait on them, nobody → an agent's.

  An agent step is a READY task like any other, with the sub-issue's description as its brief and a link to it. If the owner is wrong, change it on the goal page.
- **Steps you add here** become sub-issues of the goal's issue, with the step's details as the description. One you own is assigned to you in Linear; an agent's stays unassigned; a wait stays unassigned and its description starts with who it's waiting on. That holds while the goal and its issue are both open. An agent's own questions and actions on the goal stay separate Linear issues, as they always were.
- **Closing, both ways:**
  - a sub-issue completed in Linear marks its step done, and a canceled one cancels its step;
  - a step done here, by you or an agent, moves its sub-issue to completed, and a step cancelled here moves it to canceled;
  - the issue completed in Linear marks the goal achieved, and canceled drops it. Either way, the goal's open agent tasks (ready or in progress) are cancelled, so no agent picks up work for a finished goal. Your own steps and waits stay as they are;
  - **Mark achieved** here moves the issue to completed, and **Drop** moves it to canceled, along with the sub-issues of the steps the drop cancels.

  Only a change crosses over, and only once. A change made because of the other side isn't sent back: the agent tasks cancelled when the issue closed in Linear don't cancel their sub-issues. Something already closed in Linear is never overridden. Reopen a goal here and Linear won't close it again; reopen an issue or sub-issue in Linear and apo won't close it again. If you reopen a goal here and achieve it again, Linear is told again.
- **What doesn't sync:** later edits to titles and descriptions, owners after import, and reopening (in either direction).

It runs on the dashboard's poll, every 30 seconds, so the dashboard has to be running. Turning it off for a project stops imports and status sync for that project; turning Linear sync off in Settings stops all of it. If you delete a goal's issue in Linear, drop the goal here too: until you do, Now shows Linear sync failing.

## Search

Press `/` anywhere (or `g` then `f`), or type anything in the palette and choose **Search everything**.

- **What it searches:** questions, summaries, context, recommendations, answers, session recaps and outcomes.
- **How it matches:** any part of a word, and every word you type has to match.
- **Search transcripts too** also greps the session transcripts on disk. Slower, but finds what was said in a conversation.

In the results, `j`/`k` move and `↵` opens. `Esc` leaves the search box first.

## Notifications

Off until you turn them on: **Settings → Notifications → Turn on** (once per browser). Then a move interrupts you only when:

- it has waited 30 minutes, or sits in a pinned or hot project (a background project never interrupts);
- you've had fewer than 6 today, the last was at least 20 minutes ago, and none came from that project in the last hour;
- it's within your active hours (set in Settings);
- you're not in the middle of a prompt in a session (you're watching your own agent; it waits for that turn to end).

You get one reminder after 4 hours, then it only ranks. Handling a move clears its notification. Any tab of the dashboard can deliver them, even in the background.

## Activity

**Activity** (`g` then `a`; or a project's Activity tab) is the log: items filed, answered and applied, and sessions starting, waiting, erroring and ending. The same thing happening repeatedly is one line with a count (×7). **Include routine turns** adds every finished turn.

## The command palette

`Ctrl`+`K` (`⌘K` on a Mac) or `:` opens it. Type to jump to a page, project, item or session, or type a command. It always shows what it'll do before you press `↵`.

| Command | Does |
|---|---|
| `accept 12` (or `y 12`) | Accept #12's recommendation |
| `answer 12 2` (or `a 12 2`) | Pick option 2 on #12 |
| `answer 12 use the v2 API` | Answer #12 in your own words |
| `done 7 [note]` (or `e 7`) | Mark action #7 done |
| `decide 12` | You decide |
| `decline 12 [why]` | Decline |
| `snooze 12 2h` / `tonight` / `tomorrow` / `week` / `blocks` | Snooze |
| `apply 13` | Apply #13's answer in the background |
| `accept all` | Open Accept in bulk |
| `plan 5` / `round` | Lightning round: 5 minutes / everything |
| `search stripe webhook` | Search |
| `bug acme Login button does nothing` | Report a bug to a project |
| `go projects` | Go to a page |
| `seen` | Mark "Since you were away" seen |

## Keyboard shortcuts

Press `?` on any page for this list.

| Where | Keys |
|---|---|
| Anywhere | `Ctrl/⌘ K` or `:` palette · `/` search · `?` shortcuts · `Ctrl/⌘ ↵` send what you're typing |
| Anywhere, `g` then… | `n` Now · `p` Projects · `o` Goals · `w` Work · `s` Agents · `d` Decisions · `a` Activity · `f` Search · `h` this guide · `,` Settings |
| Now | `j`/`k` move · `↵` open · `1`–`9` option · `y` accept · `e` done / reviewed · `a` answer · `s` snooze · `c` copy resume command · `r` apply updates · `t` 5-minute round · `b` accept in bulk |
| A decision or action page | `1`–`9` option · `y` accept · `d` you decide · `e` done · `a` answer · `c` copy resume command |
| Lightning round | `↵` accept / done · `1`–`9` · `d` · `a` · `s` snooze · `n` skip · `Esc` leave |
| All projects, Work and Search lists | `j`/`k` move · `↵` or `o` open · `p` pin (Projects) · `e` done (a Do it row on Work) |
| Projects pages | `1`–`5` tabs · `[` / `]` step through All projects and each project · `a` add work (Work) · and for one project: `p` pin · `o` open in Now (Overview) · `b` new work is a bug (Work) |

## For agents

Every session that has the `apo` MCP server gets apo's instructions in its system prompt: when to file a decision or an action, how goals and tasks work, that its own checklist is mirrored automatically, and to work from the project's folder. The tools:

- **Understand the project:** `get_project_brief` (purpose, done-when, links, status, priority, standing rules), `list_outcomes` (goals, their steps and due dates). The standing rules are also in `.claude/decisions/POLICY.md`; agents follow one instead of filing when it answers the question.
- **File things for you:** `create_decision_request`, `create_action_request` (with link, steps, values, inputs, check), `withdraw_request`. `list_open_questions` / `list_resolved_questions` to check before asking.
- **Plan work:** `create_outcome` and `add_step`; `list_tasks`, `claim_task` and `complete_task` for agent work (`complete_task` optionally takes `outcome`, `done`, `not_done`, `left_for_human`, `branch`; `retry_task` puts a task back to ready).
- **Read answers:** `get_action_secret` for secret inputs you provided.
- **Close the loop:** `answer_decision_request` (when you answer in chat) and `mark_decision_resolved` (when the work is done).
- **Their own checklist** (TaskCreate / TodoWrite) needs no tool: apo mirrors it from the hooks.

A session resumed from one started before an apo upgrade keeps its old tool list and instructions; start a fresh session to get the new ones.

Without MCP, or in a session too old to have a tool, there's the `apo` CLI:

- **Decisions:** `apo ask`, `apo act`, `apo answer`, `apo list`, `apo pending [--all]`, `apo resolve`.
- **Tasks:** `apo tasks`, `apo claim <id>`, `apo complete <id> --note "..."`. `apo complete` also takes `--outcome partial`, `--done`, `--not-done`, `--left-for-human` and `--branch`; a partial completion shows as "Partly done". `apo retry <id>` puts a failed, cancelled, finished or stuck-claimed task back to READY (history is kept); `apo stuck` lists claimed tasks whose session has ended. A task's page shows its History: every claim, completion, release and retry.
- **Bugs:** `apo bug "What's wrong" [--happened ...] [--expected ...] [--steps ...] [--severity low|normal|urgent]`, from the project's folder or with `--project NAME`. It files a task labelled bug, like A bug in New work.

## Settings

- **Linear:** paste a personal API key and team key to mirror agents' requests as Linear issues. A comment on the issue counts as an answer (and needs your click before it can start a background agent). Linear can also be where you plan: with **Goals from Linear** on for a project, an issue you create in its Linear project becomes a goal here, its sub-issues become the goal's steps, and closing either side's goal or steps closes the other's (see [Goals from Linear](#goals-from-linear)). Settings lists the projects that have it on; the switch, and the choice of Linear project, are on each project's Overview.
- **Notifications:** turn on or off for this browser, and set active hours.

## When something looks wrong

- **An answer sits in transit:** nothing is running in that repo. Use **Apply in background**, or copy the resume command.
- **`/decisions` says there's nothing to do:** check you're in the project's folder, not `~`. `apo pending --all` shows where answers are waiting.
- **An agent says `claim_task` / `complete_task` don't exist:** its session predates them (it was resumed from an older session). It can use `apo claim` / `apo complete` instead; a fresh session has the tools.
- **A session shows the wrong status:** status refreshes from `claude agents` while the dashboard is open. A session started before the hooks were installed shows up once it's been seen alive.
- **Two projects for one folder:** Now lists it under Problems. Open one of them and use **Merge into another project**.
- **A session hit the usage limit:** an open one continues by itself when the limit resets, and Now counts it as waiting, not a problem. If auto-continue is off (`/config`), it shows as a problem: press Enter in its terminal after the reset. If you closed it meanwhile, it's in Your move with **Resume at** (the reset time) and **Resume now**.
- **Something errored with "Claude Code didn't say which":** the API gave no reason. Open the session (`claude --resume`, or the copy button) and retry.
- **A running session doesn't have the newest tools** (like `create_outcome`): sessions keep the MCP server they started with. Restart the session.
