"""What changed in a project's folders: git history and GitHub pull requests. Local and
no tokens; read-only except merge_branch, which runs only when you press Merge. Pull
requests need the network, so they're fetched in the background and pages only ever
read the cache."""
import json
import os
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from apo import config

GIT_TIMEOUT = 5
MERGE_TIMEOUT = 60
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
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
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


# Ahead/behind counts and the conflict check depend only on the two commits, so a page
# re-rendered on every database change asks git once per pair, not once per render.
_branch_cache: dict[tuple[str, str, str], dict] = {}


def _sha(path: str, ref: str) -> str | None:
    out = _git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out and out.returncode == 0 and out.stdout.strip() else None


def _is_ancestor(path: str, commit: str, of: str) -> bool:
    out = _git(path, "merge-base", "--is-ancestor", commit, of)
    return bool(out) and out.returncode == 0


def _home(path: str) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


def worktrees(path: str) -> list[dict]:
    """Every checkout of path's repository: [{"path", "branch"}], branch None when detached."""
    out = _git(path, "worktree", "list", "--porcelain")
    if not out or out.returncode != 0:
        return []
    found: list[dict] = []
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            found.append({"path": line[len("worktree "):], "branch": None})
        elif line.startswith("branch refs/heads/") and found:
            found[-1]["branch"] = line[len("branch refs/heads/"):]
    return found


def _checkout_of(path: str, branch: str) -> str | None:
    return next((w["path"] for w in worktrees(path) if w["branch"] == branch), None)


def _blocked(default: str, checkout: str | None) -> str | None:
    """Why a merge into checkout can't run now. Untracked files don't count: git refuses
    by itself if the merge would overwrite one."""
    if not checkout:
        return None
    out = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if not out or out.returncode != 0:
        return f"Can't read the state of {_home(checkout)}, where {default} is checked out."
    if out.stdout.strip():
        return f"{default} is checked out in {_home(checkout)} with uncommitted changes. Commit or stash them first."
    return None


def merge_command(path: str, branch: str, default: str, checkout: str | None) -> str:
    """The git the Merge button runs, in words you could paste."""
    if checkout:
        return f"git -C {shlex.quote(checkout)} merge --no-edit {shlex.quote(branch)}"
    return f"git merge --no-edit {shlex.quote(branch)}, in a temporary checkout of {default}"


def _would_conflict(path: str, default_sha: str, branch_sha: str) -> list[str]:
    """Files a merge would leave conflicted, from git's trivial three-way merge-tree. A hint
    for the list, not a promise: merge_branch finds out for sure, and backs out."""
    base = _git(path, "merge-base", default_sha, branch_sha)
    if not base or base.returncode != 0 or not base.stdout.strip():
        return []
    out = _git(path, "merge-tree", base.stdout.strip(), default_sha, branch_sha)
    if not out or out.returncode != 0:
        return []
    files: list[str] = []
    current = None
    for line in out.stdout.splitlines():
        if line.startswith(("  base ", "  our ", "  their ", "  result ")):
            parts = line.split(None, 3)
            current = parts[3] if len(parts) == 4 else None
        elif line.startswith("+<<<<<<<") and current and current not in files:
            files.append(current)
    return files


def _pair_state(path: str, default_sha: str, branch_sha: str) -> dict | None:
    key = (os.path.realpath(path), default_sha, branch_sha)
    if key not in _branch_cache:
        out = _git(path, "rev-list", "--left-right", "--count", f"{default_sha}...{branch_sha}")
        if not out or out.returncode != 0:
            return None
        behind, ahead = (int(n) for n in out.stdout.split())
        if len(_branch_cache) > 500:
            _branch_cache.clear()
        _branch_cache[key] = {"ahead": ahead, "behind": behind,
                              "conflicts": _would_conflict(path, default_sha, branch_sha) if ahead else []}
    state = _branch_cache[key]
    return {**state, "conflicts": list(state["conflicts"])}


def mergeable_branches(path: str) -> dict:
    """Local branches with commits the default branch doesn't have, newest first: what
    agents finished in worktrees and nobody has merged yet. checkout is where the default
    branch is checked out (the merge happens there), blocked why it can't happen now,
    main_checkout the repository's own folder as opposed to its added worktrees."""
    listing = {"default_branch": None, "checkout": None, "main_checkout": None, "blocked": None, "branches": []}
    default = default_branch(path)
    default_sha = _sha(path, f"refs/heads/{default}") if default else None
    if not default_sha:
        return listing
    trees = worktrees(path)
    checkout = next((w["path"] for w in trees if w["branch"] == default), None)
    checked_out = {w["branch"]: w["path"] for w in trees if w["branch"]}
    listing.update(default_branch=default, checkout=checkout, main_checkout=trees[0]["path"] if trees else None)
    out = _git(path, "for-each-ref", f"--no-merged={default_sha}", "--sort=-committerdate",
               f"--format=%(refname){_SEP}%(objectname){_SEP}%(committerdate:iso-strict){_SEP}%(subject)",
               "refs/heads")
    if not out or out.returncode != 0:
        return listing
    for line in out.stdout.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 4 or not parts[0].startswith("refs/heads/"):
            continue
        ref, sha, at, subject = parts
        name = ref[len("refs/heads/"):]
        state = _pair_state(path, default_sha, sha)
        if name == default or not state or not state["ahead"]:
            continue
        listing["branches"].append({
            "branch": name, "commit": sha, "subject": subject,
            "at": datetime.fromisoformat(at).astimezone(timezone.utc).isoformat(),
            "worktree": checked_out.get(name), "command": merge_command(path, name, default, checkout), **state,
        })
    if listing["branches"]:
        listing["blocked"] = _blocked(default, checkout)
    return listing


def branch_state(path: str, branch: str) -> dict | None:
    """One local branch measured against the default branch, for an item's Merge button.
    None when either branch isn't there."""
    default = default_branch(path)
    default_sha = _sha(path, f"refs/heads/{default}") if default else None
    branch_sha = _sha(path, f"refs/heads/{branch}") if branch and not branch.startswith("-") else None
    if not default_sha or not branch_sha or branch == default:
        return None
    state = _pair_state(path, default_sha, branch_sha)
    if state is None:
        return None
    checkout = _checkout_of(path, default)
    return {"branch": branch, "commit": branch_sha, "default_branch": default, "merged": not state["ahead"],
            "checkout": checkout,
            "blocked": _blocked(default, checkout) if state["ahead"] else None,
            "command": merge_command(path, branch, default, checkout), **state}


def _merge_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}


def _merge_in(checkout: str, branch: str, result: dict) -> dict:
    """git merge in checkout; on any failure, back out so the checkout is as it was."""
    branch_sha = _sha(checkout, f"refs/heads/{branch}")
    try:
        out = subprocess.run(["git", "-C", checkout, "merge", "--no-edit", branch], capture_output=True,
                             text=True, timeout=MERGE_TIMEOUT, env=_merge_env())
    except (OSError, subprocess.SubprocessError) as exc:
        out = subprocess.CompletedProcess([], 1, "", str(exc))
    if out.returncode == 0:
        head = _sha(checkout, "HEAD")
        return {**result, "ok": True, "commit": head, "fast_forward": head == branch_sha}
    unmerged = _git(checkout, "diff", "--name-only", "--diff-filter=U")
    conflicts = unmerged.stdout.split("\n") if unmerged and unmerged.returncode == 0 else []
    conflicts = [f for f in conflicts if f]
    if _sha(checkout, "MERGE_HEAD"):
        _git(checkout, "merge", "--abort")
    if conflicts:
        error = (f"{branch} conflicts with {result['into']} in {', '.join(conflicts)}. Nothing was changed; "
                 f"merge it by hand or ask an agent to.")
    else:
        detail = (out.stderr or out.stdout).strip().splitlines()
        error = f"git merge failed: {detail[-1] if detail else 'no output'}"
    return {**result, "error": error, "conflicts": conflicts}


def merge_branch(path: str, branch: str) -> dict:
    """Merge a local branch into the default branch with plain git, no agent. It happens
    in the checkout that has the default branch, which must have no uncommitted changes
    to tracked files. With the default branch checked out nowhere, a fast-forward just
    moves it, and anything else merges in a temporary checkout removed afterwards. A
    conflict is backed out, leaving everything as it was."""
    result = {"ok": False, "error": None, "branch": branch, "into": None, "checkout": None, "commit": None,
              "fast_forward": False, "already": False, "conflicts": []}
    default = default_branch(path)
    default_sha = _sha(path, f"refs/heads/{default}") if default else None
    if not default_sha:
        return {**result, "error": "This folder has no main or master branch to merge into."}
    result["into"] = default
    branch_sha = _sha(path, f"refs/heads/{branch}") if branch and not branch.startswith("-") else None
    if not branch_sha or branch == default:
        return {**result, "error": f"There's no other local branch named {branch!r} to merge into {default}."}
    if _is_ancestor(path, branch_sha, default_sha):
        return {**result, "ok": True, "already": True, "commit": default_sha}
    checkout = _checkout_of(path, default)
    if checkout:
        blocked = _blocked(default, checkout)
        if blocked:
            return {**result, "checkout": checkout, "error": blocked}
        return _merge_in(checkout, branch, {**result, "checkout": checkout})
    if _is_ancestor(path, default_sha, branch_sha):
        out = _git(path, "update-ref", "-m", f"merge {branch}: Fast-forward", f"refs/heads/{default}",
                   branch_sha, default_sha)
        if out and out.returncode == 0:
            return {**result, "ok": True, "fast_forward": True, "commit": branch_sha}
        return {**result, "error": f"Couldn't move {default}: {(out.stderr.strip() if out else '') or 'git failed'}"}
    with tempfile.TemporaryDirectory(prefix="apo-merge-") as scratch:
        target = os.path.join(scratch, "checkout")
        added = _git(path, "worktree", "add", "--quiet", target, default)
        if not added or added.returncode != 0:
            reason = (added.stderr.strip() if added else "") or "git failed"
            return {**result, "error": f"Couldn't make a temporary checkout of {default}: {reason}"}
        try:
            return _merge_in(target, branch, result)
        finally:
            _git(path, "worktree", "remove", "--force", target)
            _git(path, "worktree", "prune")


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
