"""`apo dashboard start|stop|status|run`, and keeping it up without a terminal: with
APO_DASHBOARD_AUTOSTART on, any Claude Code session starting brings it up if it's
down (WSL has no systemd by default, so there's often no service manager to ask)."""
import os
import socket
import subprocess
import sys
import time

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

    assert cli._stop_dashboard(_free_port())[0] == "stopped"
    assert killed == [4242] and not (db_path.parent / "dashboard.pid").exists()


def test_stop_with_nothing_running_says_so(cli, db_path):
    assert cli._stop_dashboard(_free_port()) == ("not running", [])


LISTENER = """
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 0))
s.listen()
print(s.getsockname()[1], flush=True)
time.sleep(60)
"""


@pytest.fixture
def listener():
    """A process listening on a free port, with the given words in its command line."""
    procs = []

    def start(*argv):
        proc = subprocess.Popen([sys.executable, "-c", LISTENER, *argv], stdout=subprocess.PIPE, text=True)
        procs.append(proc)
        return proc, int(proc.stdout.readline())

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


def _exited(proc, within=5.0):
    deadline = time.monotonic() + within
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc.poll() is not None


def test_stop_ends_a_dashboard_it_didnt_start(cli, listener):
    """Started another way (an older install, run.sh, a terminal), so no pid file names it."""
    proc, port = listener("uvicorn", "app:app", "--reload")

    assert cli._stop_dashboard(port)[0] == "stopped"
    assert _exited(proc) and not cli._dashboard_running(port)


def test_a_stale_pid_file_doesnt_report_a_running_dashboard_as_stopped(cli, db_path, listener):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    (db_path.parent / "dashboard.pid").write_text(f"{gone.pid}\n")
    proc, port = listener("-m", "uvicorn", "apo.web.app:app")

    assert cli._stop_dashboard(port)[0] == "stopped"
    assert _exited(proc)


def test_stop_leaves_another_program_on_the_port_alone(cli, listener):
    proc, port = listener("some-other-server")

    status, pids = cli._stop_dashboard(port)

    assert status == "not ours" and pids == [proc.pid]
    assert proc.poll() is None


def test_the_stop_command_says_what_it_did(cli, listener, monkeypatch, capsys):
    proc, port = listener("some-other-server")
    monkeypatch.setenv("APO_DASHBOARD_PORT", str(port))

    cli.main(["dashboard", "stop"])

    assert f"port {port}" in capsys.readouterr().out and proc.poll() is None


def test_a_session_starting_brings_the_dashboard_up_only_when_asked(cli, spawned, monkeypatch, tmp_path):
    from apo import config

    monkeypatch.setenv("APO_DASHBOARD_PORT", str(_free_port()))
    cli._session_start({"session_id": "s-1", "cwd": str(tmp_path)})
    assert spawned == []

    monkeypatch.setenv("APO_DASHBOARD_AUTOSTART", "1")
    cli._session_start({"session_id": "s-2", "cwd": str(tmp_path)})
    assert len(spawned) == 1


def test_a_bare_apo_dashboard_starts_it_and_says_where(cli, spawned, monkeypatch, capsys):
    """What the website tells a new user to type."""
    port = _free_port()
    monkeypatch.setenv("APO_DASHBOARD_PORT", str(port))

    cli.main(["dashboard"])

    assert len(spawned) == 1
    assert f"http://127.0.0.1:{port}" in capsys.readouterr().out
