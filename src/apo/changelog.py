"""What changed in a project's folders: git history and GitHub pull requests. Local and
read-only, no tokens. Pull requests need the network, so they're fetched in the
background and pages only ever read the cache."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from apo import config

GIT_TIMEOUT = 5
GH_TIMEOUT = 20
PR_REFRESH_SECONDS = 15 * 60
_SEP = "\x1f"

_pr_cache: dict[str, tuple[float, list[dict]]] = {}


def commits(path: str, since: str, limit: int = 200) -> list[dict]:
    """Commits on any local branch since `since`, newest first. Branches count because
    agents work on them before anything is merged."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "log", "--branches", "--no-merges", f"--since={since}", f"--max-count={limit}",
             f"--pretty=format:%H{_SEP}%h{_SEP}%an{_SEP}%aI{_SEP}%s"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    found = []
    for line in out.stdout.splitlines():
        parts = line.split(_SEP)
        if len(parts) == 5:
            sha, short, author, at, subject = parts
            at = datetime.fromisoformat(at).astimezone(timezone.utc).isoformat()
            found.append({"sha": sha, "short": short, "author": author, "at": at, "subject": subject})
    return found


def _git(path: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def default_branch(path: str) -> str | None:
    """origin/HEAD's branch, else a local main or master."""
    out = _git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if out and out.returncode == 0 and out.stdout.strip().startswith("origin/"):
        return out.stdout.strip()[len("origin/"):]
    for name in ("main", "master"):
        out = _git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        if out and out.returncode == 0:
            return name
    return None


def merged_into(path: str, commit: str, branch: str) -> bool | None:
    """Whether commit is in branch, locally or on origin. None when neither can be asked."""
    verdicts = []
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        out = _git(path, "merge-base", "--is-ancestor", commit, ref)
        if out and out.returncode in (0, 1):
            verdicts.append(out.returncode == 0)
    return any(verdicts) if verdicts else None


def work_location(path: str) -> dict | None:
    """The branch and commit checked out at path, and whether that commit is in the
    default branch. None when git can't say (not a checkout, no commits, no git)."""
    head = _git(path, "rev-parse", "--verify", "--quiet", "HEAD")
    if not head or head.returncode != 0 or not head.stdout.strip():
        return None
    commit = head.stdout.strip()
    out = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    branch = out.stdout.strip() if out and out.returncode == 0 else None
    default = default_branch(path)
    return {"branch": None if branch in (None, "", "HEAD") else branch, "commit": commit,
            "default_branch": default, "merged": merged_into(path, commit, default) if default else None}


def has_pull_requests_cache(path: str) -> bool:
    return os.path.realpath(path) in _pr_cache


def pull_requests(path: str) -> list[dict]:
    cached = _pr_cache.get(os.path.realpath(path))
    return cached[1] if cached else []


def refresh_pull_requests(path: str, max_age: float = PR_REFRESH_SECONDS) -> None:
    """A folder gh can't answer for (no GitHub remote, not signed in) is cached as having
    none, so it isn't asked again until the cache is stale."""
    key = os.path.realpath(path)
    cached = _pr_cache.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return
    prs: list[dict] = []
    try:
        out = subprocess.run(
            [config.gh_bin(), "pr", "list", "--state", "all", "--limit", "30",
             "--json", "number,title,state,mergedAt,createdAt,url,isDraft"],
            capture_output=True, text=True, timeout=GH_TIMEOUT, cwd=path,
        )
        if out.returncode == 0:
            prs = json.loads(out.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        prs = []
    _pr_cache[key] = (time.monotonic(), prs)


def project_changes(folders: list[dict], since: str, seen_at: str | None = None) -> dict:
    """Commits and pull requests across a project's folders since `since`. A commit seen
    from two folders (one inside the other) counts once; anything newer than `seen_at`
    is marked new."""
    seen_shas: set[str] = set()
    found = []
    prs = {}
    for folder in folders:
        for commit in commits(folder["local_path"], since):
            if commit["sha"] in seen_shas:
                continue
            seen_shas.add(commit["sha"])
            found.append({**commit, "folder": folder.get("label") or folder.get("name")})
        for pr in pull_requests(folder["local_path"]):
            when = pr.get("mergedAt") if pr.get("state") == "MERGED" else pr.get("createdAt")
            if when and _after(when, since) and pr.get("state") != "CLOSED":
                prs[pr["url"]] = {**pr, "at": when.replace("Z", "+00:00")}
    found.sort(key=lambda c: epoch(c["at"]), reverse=True)
    pr_list = sorted(prs.values(), key=lambda p: epoch(p["at"]), reverse=True)
    for item in (*found, *pr_list):
        item["new"] = bool(seen_at) and _after(item["at"], seen_at)
    return {
        "commits": found,
        "prs": pr_list,
        "merged": sum(1 for p in pr_list if p["state"] == "MERGED"),
        "opened": sum(1 for p in pr_list if p["state"] == "OPEN"),
        "new": sum(1 for item in (*found, *pr_list) if item["new"]),
    }


def epoch(iso_ts: str) -> float:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def _after(iso_ts: str, than: str) -> bool:
    return epoch(iso_ts) > epoch(than)
