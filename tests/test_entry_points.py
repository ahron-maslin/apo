"""The ways in: the `apo` command, `python -m apo` and `apo mcp`. The old `ado` name is
gone."""
import os
import subprocess
import sys

import apo


def _run(args, env, stdin=None):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, input=stdin)


def _env(db_path):
    return dict(os.environ, APO_DB=str(db_path))


def test_python_dash_m_apo_shows_the_apo_help(db_path):
    result = _run(["-m", "apo", "--help"], _env(db_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: apo ")
    assert "Switchboard" not in result.stdout


def test_version(db_path):
    result = _run(["-m", "apo", "--version"], _env(db_path))

    assert result.stdout.strip() == f"apo {apo.__version__}"
    assert apo.__version__ == "0.3.0"


def test_there_is_no_ado_command_any_more():
    from apo import cli

    assert not hasattr(cli, "legacy_main")


def test_the_mcp_server_is_an_apo_subcommand(db_path):
    result = _run(["-m", "apo", "mcp", "--help"], _env(db_path))

    assert result.returncode == 0, result.stderr
    assert "MCP" in result.stdout


def test_the_mcp_server_is_named_apo():
    from apo import mcp_server

    assert mcp_server.mcp.name == "apo"
