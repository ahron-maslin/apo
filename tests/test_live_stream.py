"""Pages update the moment something changes. Hooks write to the database from other
processes, so the server watches for any commit and tells open pages over a
server-sent event stream; the pages' own polling stays as the fallback.

Against a real server: TestClient reads a whole response before returning, and this
one never ends."""
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apo import db

ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port(db_path, tmp_path):
    port = _free_port()
    env = dict(os.environ, APO_DB=str(db_path), APO_OFFLINE="1", APO_CLAUDE_BIN=str(tmp_path / "no-claude"),
               APO_POLL_SECONDS="100000", APO_LINEAR_API_KEY="", APO_LINEAR_TEAM="")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "apo.web.app:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _stream(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream")
    return conn, conn.getresponse()


def _event_within(resp, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = resp.readline().decode().strip()
        if line.startswith("event:"):
            return line
    return None


def test_the_stream_says_when_anything_commits(port):
    project = db.get_or_create_project(db.get_connection(), "acme")
    conn, resp = _stream(port)
    try:
        assert resp.getheader("content-type").startswith("text/event-stream")
        assert resp.readline().decode().startswith("retry:")

        db.create_task(db.get_connection(), project_id=project["id"], title="Written by a hook")

        assert _event_within(resp, 4) == "event: change"
    finally:
        conn.close()


def test_nothing_is_sent_while_nothing_changes(port):
    db.get_connection()
    conn, resp = _stream(port)
    try:
        resp.readline()
        resp.readline()
        resp.fp.raw._sock.settimeout(2.5)
        with pytest.raises(socket.timeout):
            resp.readline()
    finally:
        conn.close()


def test_an_open_stream_doesnt_hold_up_a_restart(db_path, tmp_path):
    """The live dashboard runs under --reload, so every deploy restarts it; a stream
    left open by a browser tab must not keep that restart waiting."""
    db.get_connection()
    port = _free_port()
    env = dict(os.environ, APO_DB=str(db_path), APO_OFFLINE="1", APO_POLL_SECONDS="100000", APO_LINEAR_API_KEY="")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "apo.web.app:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        conn, resp = _stream(port)
        resp.readline()

        proc.terminate()

        proc.wait(timeout=8)
        conn.close()
    finally:
        proc.kill()
