"""The ways in: the `apo` command, `python -m apo`, `apo mcp`, and `ado`, the old name,
kept for a release or two so existing hooks and habits keep working."""
import json
import os
import subprocess
import sys

import apo


def _run(args, env, stdin=None):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, input=stdin)


def _env(db_path):
    return dict(os.environ, APO_DB=str(db_path))


def _as_ado(*args):
    return ["-c", "import sys; from apo.cli import legacy_main; sys.argv = ['ado', *sys.argv[1:]]; legacy_main()", *args]


def test_python_dash_m_apo_shows_the_apo_help(db_path):
    result = _run(["-m", "apo", "--help"], _env(db_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: apo ")
    assert "Switchboard" not in result.stdout


def test_version(db_path):
    result = _run(["-m", "apo", "--version"], _env(db_path))

    assert result.stdout.strip() == f"apo {apo.__version__}"
    assert apo.__version__ == "0.1.0"


def test_ado_says_it_is_deprecated_on_stderr_then_does_the_same_thing(db_path):
    env = _env(db_path)
    _run(["-m", "apo", "ask", "--project", "acme", "--title", "T", "--question", "Q?"], env)

    new = _run(["-m", "apo", "list", "--project", "acme"], env)
    old = _run(_as_ado("list", "--project", "acme"), env)

    assert old.returncode == 0 and old.stdout == new.stdout
    assert old.stderr.strip().splitlines() == ["ado is deprecated and will be removed; use apo instead."]
    assert new.stderr == ""


def test_ado_hooks_keep_their_json_on_stdout(db_path, tmp_path):
    payload = json.dumps({"session_id": "s-1", "cwd": str(tmp_path), "hook_event_name": "SessionStart"})

    result = _run(_as_ado("session-start"), _env(db_path), stdin=payload)

    assert result.returncode == 0
    json.loads(result.stdout)
    assert "deprecated" in result.stderr


def test_the_mcp_server_is_an_apo_subcommand(db_path):
    result = _run(["-m", "apo", "mcp", "--help"], _env(db_path))

    assert result.returncode == 0, result.stderr
    assert "MCP" in result.stdout


def test_the_mcp_server_is_named_apo():
    from apo import mcp_server

    assert mcp_server.mcp.name == "apo"
