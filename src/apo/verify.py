"""Declarative checks for an Action marked done ("did the DNS record actually land?").

An agent describes the check; this module runs a fixed set of check types. There is
deliberately no "run this command" type: the spec is agent-supplied text, and turning
it into execution is the trust problem that retired the old background dispatcher.
Every check fails safe — an error is "not verified", never an exception.
"""
import json
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from apo import __version__

TIMEOUT_SECONDS = 10


@dataclass
class Result:
    ok: bool
    detail: str


def _open_url(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": f"apo/{__version__} verify"})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - http(s) only, checked below


def _dns(check: dict, repo_path: str | None) -> Result:
    name, record_type = check.get("name"), (check.get("record_type") or "A").upper()
    if not name or record_type not in {"A", "AAAA", "CNAME", "TXT", "MX", "NS"}:
        return Result(False, "The DNS check needs a name and a record type (A, AAAA, CNAME, TXT, MX, NS).")
    try:
        out = subprocess.run(
            ["dig", "+short", record_type, name], capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(False, f"Couldn't look up DNS: {exc}")
    answers = [line.strip().strip('"') for line in out.splitlines() if line.strip()]
    if not answers:
        return Result(False, f"No {record_type} record for {name} yet (DNS can take a while to propagate).")
    expected = check.get("contains")
    if expected and not any(expected in a for a in answers):
        return Result(False, f"{name} has {record_type} {', '.join(answers)[:200]} — not {expected}.")
    return Result(True, f"{name} {record_type}: {', '.join(answers)[:200]}")


def _http(check: dict, repo_path: str | None) -> Result:
    url = check.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return Result(False, "The HTTP check needs an http(s) URL.")
    want = int(check.get("status") or 200)
    try:
        with _open_url(url, TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read(200_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return Result(False, f"Couldn't reach {url}: {exc}")
    if status != want:
        return Result(False, f"{url} returned {status}, expected {want}.")
    if check.get("contains") and check["contains"] not in body:
        return Result(False, f"{url} returned {status} but doesn't contain {check['contains']!r}.")
    return Result(True, f"{url} returned {status}.")


def _inside(repo_path: str | None, relative: str) -> Path | None:
    if not repo_path or not relative:
        return None
    root = Path(repo_path).resolve()
    target = (root / relative).resolve()
    return target if target == root or root in target.parents else None


def _file(check: dict, repo_path: str | None) -> Result:
    target = _inside(repo_path, check.get("path") or "")
    if target is None:
        return Result(False, f"{check.get('path')!r} is outside the repo (or the project has no repo).")
    if target.exists():
        return Result(True, f"{check['path']} exists.")
    return Result(False, f"{check['path']} doesn't exist yet.")


def _env(check: dict, repo_path: str | None) -> Result:
    name = check.get("name")
    env_file = _inside(repo_path, check.get("file") or ".env")
    if not name or env_file is None:
        return Result(False, "The .env check needs a variable name and a repo.")
    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return Result(False, f"No {env_file.name} in the repo yet.")
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key.strip() == name and value.strip():
            return Result(True, f"{name} is set in {env_file.name}.")
    return Result(False, f"{name} isn't set in {env_file.name} yet.")


def _pr_merged(check: dict, repo_path: str | None) -> Result:
    url = check.get("url") or ""
    if "/pull/" not in url:
        return Result(False, "The PR check needs a pull request URL.")
    try:
        out = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state"], capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        ).stdout
        state = json.loads(out).get("state")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
        return Result(False, f"Couldn't check the PR: {exc}")
    return Result(state == "MERGED", f"The PR is {str(state).lower()}.")


_CHECKS = {"dns": _dns, "http": _http, "file": _file, "env": _env, "pr_merged": _pr_merged}
CHECK_TYPES = tuple(_CHECKS)


def run_check(check: dict | None, repo_path: str | None = None) -> Result:
    runner = _CHECKS.get((check or {}).get("type"))
    if runner is None:
        return Result(False, f"Unknown check type {(check or {}).get('type')!r}; supported: {', '.join(CHECK_TYPES)}.")
    try:
        return runner(check, repo_path)
    except Exception as exc:  # noqa: BLE001
        return Result(False, f"The check failed to run: {exc}")
