"""The released wheel works on its own: built from this tree, installed into a fresh
virtualenv, it runs `apo --help` and serves the dashboard, templates, static files and
guide included. Slow, and installing the dependencies needs the network, so it skips
when PyPI can't be reached; `-m "not wheel"` leaves it out."""
import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REAL_HOME = Path(os.path.expanduser("~"))

pytestmark = pytest.mark.wheel


def _online() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    """Built from a clean copy, so leftovers of an editable install can't stand in for
    package data the build would otherwise miss, and the build writes nothing here."""
    source = tmp_path_factory.mktemp("source")
    for name in ("pyproject.toml", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(source)],
                   check=True, capture_output=True, text=True)
    [built] = out.glob("getapo-*.whl")
    return built


def test_the_wheel_carries_the_package_data(wheel):
    names = set(zipfile.ZipFile(wheel).namelist())

    for path in ("apo/web/templates/base.html", "apo/web/static/app.css", "apo/web/static/app.js",
                 "apo/resources/guide.md", "apo/resources/skills/decisions/SKILL.md",
                 "apo/resources/hooks/session-decisions-nudge.sh"):
        assert path in names, path
    assert not any(n.startswith(("tests/", "docs/", "scripts/", "site/")) for n in names)
    metadata = zipfile.ZipFile(wheel).read(next(n for n in names if n.endswith("METADATA"))).decode()
    assert "Name: getapo" in metadata and "Version: 0.1.0" in metadata
    assert "License-Expression: MIT" in metadata
    assert {n.rsplit("/", 1)[-1] for n in names if "/licenses/" in n} == {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    entry_points = zipfile.ZipFile(wheel).read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
    assert "apo = apo.cli:main" in entry_points and "ado = apo.cli:legacy_main" in entry_points


@pytest.mark.skipif(not _online(), reason="installing the wheel's dependencies needs PyPI")
def test_the_installed_wheel_runs_and_serves_the_dashboard(wheel, tmp_path):
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    env = dict(os.environ, PIP_CACHE_DIR=str(REAL_HOME / ".cache" / "pip"), PIP_DISABLE_PIP_VERSION_CHECK="1")
    subprocess.run([str(venv / "bin" / "pip"), "install", "-q", str(wheel)], check=True, env=env,
                   capture_output=True, text=True, timeout=600)
    apo = venv / "bin" / "apo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = dict(os.environ, APO_DB=str(tmp_path / "data" / "orchestrator.db"), APO_OFFLINE="1",
               APO_DASHBOARD_AUTOSTART="", PYTHONPATH="")

    help_text = subprocess.run([str(apo), "--help"], capture_output=True, text=True, env=env, cwd=elsewhere, check=True)
    assert help_text.stdout.startswith("usage: apo ")
    where = subprocess.run([str(venv / "bin" / "python"), "-c", "import apo; print(apo.__file__)"],
                           capture_output=True, text=True, env=env, cwd=elsewhere, check=True)
    assert str(venv) in where.stdout

    port = _free_port()
    server = subprocess.Popen([str(apo), "dashboard", "run", "--port", str(port)], env=env, cwd=elsewhere,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                status, page = _get(port, "/")
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"the dashboard didn't come up: {server.stderr.read() if server.poll() is not None else 'timeout'}")
                time.sleep(0.2)
        assert status == 200 and "apo</title>" in page
        assert _get(port, "/static/app.css")[0] == 200
        assert _get(port, "/guide")[0] == 200
    finally:
        server.terminate()
        server.wait(timeout=10)
