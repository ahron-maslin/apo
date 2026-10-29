"""`apo dashboard start|stop|status|run`, and keeping it up without a terminal: with
APO_DASHBOARD_AUTOSTART on, any Claude Code session starting brings it up if it's
down (WSL has no systemd by default, so there's often no service manager to ask)."""
import socket

import pytest


@pytest.fixture
def cli(db_path, monkeypatch):
    from apo import cli
    from apo import config

    monkeypatch.setenv("APO_DASHBOARD_AUTOSTART", "")
    return cli


@pytest.fixture
def spawned(cli, monkeypatch):
    calls = []

    class Proc:
        pid = 4242

    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or Proc())
    return calls


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_running_means_something_answers_on_the_port(cli):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert cli._dashboard_running(listener.getsockname()[1])
    assert not cli._dashboard_running(_free_port())


def test_start_launches_the_installed_dashboard_detached(cli, spawned, db_path):
    """From the installed package, wherever it lives, and without --reload: watching
    for code changes is for working on apo itself, not for using it."""
    port = _free_port()

    assert cli._start_dashboard(port) == "started"

    [(cmd, kw)] = spawned
    assert cmd[1:] == ["-m", "uvicorn", "apo.web.app:app", "--host", "127.0.0.1", "--port", str(port)]
    assert "cwd" not in kw and kw["start_new_session"]
    assert (db_path.parent / "dashboard.pid").read_text().strip() == "4242"


def test_dev_mode_reloads_on_code_changes(cli, spawned):
    cli._start_dashboard(_free_port(), dev=True)

    [(cmd, _)] = spawned
    assert cmd[-1] == "--reload"


def test_the_dashboard_command_uses_the_configured_port(cli, monkeypatch, capsys):
    monkeypatch.setenv("APO_DASHBOARD_PORT", "9123")

    cli.main(["dashboard", "status"])

    assert "127.0.0.1:9123" in capsys.readouterr().out


def test_dashboard_run_serves_in_the_foreground_on_the_configured_port(cli, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: calls.append((target, kw)))
    monkeypatch.setenv("APO_DASHBOARD_PORT", "9124")

    cli.main(["dashboard", "run"])

    assert calls == [("apo.web.app:app", {"host": "127.0.0.1", "port": 9124, "reload": False})]


def test_start_leaves_a_running_dashboard_alone(cli, spawned):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert cli._start_dashboard(listener.getsockname()[1]) == "running"
    assert spawned == []


def test_stop_ends_the_whole_process_group(cli, db_path, monkeypatch):
    (db_path.parent / "dashboard.pid").write_text("4242\n")
    killed = []
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: killed.append(pgid))

    assert cli._stop_dashboard() == "stopped"
    assert killed == [4242] and not (db_path.parent / "dashboard.pid").exists()


def test_stop_without_a_pid_file_says_so(cli, db_path):
    assert cli._stop_dashboard() == "not started here"


def test_a_session_starting_brings_the_dashboard_up_only_when_asked(cli, spawned, monkeypatch, tmp_path):
    from apo import config

    monkeypatch.setenv("APO_DASHBOARD_PORT", str(_free_port()))
    cli._session_start({"session_id": "s-1", "cwd": str(tmp_path)})
    assert spawned == []

    monkeypatch.setenv("APO_DASHBOARD_AUTOSTART", "1")
    cli._session_start({"session_id": "s-2", "cwd": str(tmp_path)})
    assert len(spawned) == 1
