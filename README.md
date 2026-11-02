# apo

**AI Project Orchestration.** A local dashboard and MCP server for [Claude Code](https://claude.com/claude-code).

When a Claude Code agent needs you (a decision, something only you can do, work to review), it files a request with apo instead of stopping or waiting in a terminal you aren't looking at. You answer every project's requests from one queue in your browser. The answer is written into the project's repository, under `.claude/decisions/`, where the next session picks it up, even days later.

The dashboard also shows every Claude Code session you're running, across every project: what each one is doing, which ones are waiting on you, and the goals and tasks they're working through.

![The Now page: everything waiting on you, across projects](https://getapo.pages.dev/img/now-light.webp)

Website: https://getapo.pages.dev/

## Install

```
pipx install getapo
apo init
```

The package is called `getapo` on PyPI; it installs the `apo` command. `pip install getapo` works too, in a virtualenv of your own.

`apo init` asks where your projects live and whether to connect Linear (you can skip it), then sets up Claude Code for apo:

- registers the MCP server `apo` for your user (`claude mcp add apo --scope user`), so every session can file requests and is told when to;
- adds hooks to `~/.claude/settings.json` (it saves a backup first) that keep the dashboard up to date and hand answers back to the session that asked;
- copies the `/decisions` skill into `~/.claude/skills/decisions/`.

Running it again updates those entries in place instead of adding more. If `CLAUDE_CONFIG_DIR` is set, it sets up that Claude Code profile instead of `~/.claude`.

Then start a new Claude Code session in one of your project folders (sessions already running keep the tools they started with), and open the dashboard:

```
apo dashboard
```

It starts in the background and prints its address, http://127.0.0.1:8430 by default. `apo dashboard stop` stops it. The [user guide](https://github.com/ahron-maslin/apo/blob/main/docs/user-guide.md), also at `/guide` in the dashboard, explains the rest.

## Requirements

- Python 3.10 or later.
- Claude Code, with `claude` on your PATH.
- Linux or macOS. WSL2 works; native Windows doesn't.
- Optional: `git` for each project's recent changes, `gh` for its pull requests, and `rg` (ripgrep) for searching session transcripts.

## Privacy

apo runs on your machine. There's no account and no telemetry. The dashboard listens only on 127.0.0.1 and keeps its data in a SQLite file in your data folder (`~/.local/share/apo` on Linux, `~/Library/Application Support/apo` on macOS).

It has no AI of its own and spends no tokens to run: everything it shows comes from Claude Code's hooks and its `claude agents` list. The one exception is the **Generate recap** button on a session's page: when you click it, it asks Claude Code (`claude -p`, a small model, through your own sign-in) to summarise that session's transcript.

It reaches the network only for what you turn on: Linear, if you connect it; GitHub pull requests, through `gh`, if it's installed; and the check an action request carries (a DNS record, a web page), when you mark that action done.

## Settings

`apo init` saves its answers to `~/.config/apo/config.env` on Linux (`~/Library/Application Support/apo/config.env` on macOS). Any setting can also be set in the environment, which wins. [.env.example](https://github.com/ahron-maslin/apo/blob/main/.env.example) lists them: `APO_DASHBOARD_PORT`, `APO_DASHBOARD_AUTOSTART` (start the dashboard whenever a Claude Code session starts), `APO_PROJECTS_ROOT`, `APO_DB`, `APO_OFFLINE` and the Linear ones.

## Uninstall

There's no `apo uninstall` yet. To remove apo by hand:

1. `apo dashboard stop`
2. `claude mcp remove apo --scope user`
3. In `~/.claude/settings.json`, delete the hooks whose command ends in `apo session-…`, `apo checklist-item-…` or `session-decisions-nudge.sh`. `apo init` left a backup of the file as it was before, `settings.json.bak-<date>`.
4. Delete `~/.claude/skills/decisions` (its `SKILL.md` says "Installed by apo").
5. `pipx uninstall getapo`
6. If you don't want the data: delete the data folder and `~/.config/apo`. Your repositories keep their `.claude/decisions/` folders, which you can delete too.

## Coming from ai-decision-orchestrator

apo was called ai-decision-orchestrator (ADO) before 0.1.0. An existing install keeps working: the `ado` command is a deprecated alias for `apo`, `ADO_*` settings are still read, and if `~/.claude/ai-decision-orchestrator/` exists, apo keeps using the data in it and moves nothing. Run `apo init` once to replace the old MCP server, hooks and skill with apo's.

## Development

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -m "not wheel"
```

`-m "not wheel"` leaves out the slow test that builds the wheel and installs it in a fresh virtualenv. `apo dashboard start --dev` reloads when the code changes, and `scripts/preview.sh` runs your checkout as a second dashboard on a copy of your data.

## Licence

MIT, see [LICENSE](LICENSE). The icons are Lucide's, under the ISC licence: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
