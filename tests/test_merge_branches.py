"""Branches waiting to be merged, and a Merge button that runs plain git (no agent).
An agent that finishes a task in a worktree leaves its work on a branch; the project
page lists every such branch and the item's page merges its own."""
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import changelog, db


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Claude")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "c@x")
    changelog._branch_cache.clear()


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    (path / "notes.txt").write_text("one\ntwo\nthree\n")
    _git(path, "add", "notes.txt")
    _git(path, "commit", "-q", "-m", "Initial")
    return path


def _branch(repo, name, filename="feature.txt", content="new\n", base="master"):
    """A branch with one commit, made without leaving the current checkout."""
    _git(repo, "branch", name, base)
    worktree = repo.parent / f"wt-{name}"
    _git(repo, "worktree", "add", "-q", str(worktree), name)
    (worktree / filename).write_text(content)
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-q", "-m", f"Work on {name}")
    return worktree


def _commit_on_master(repo, filename="notes.txt", content="one\nTWO\nthree\n"):
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"Master changes {filename}")


@pytest.fixture
def repo(tmp_path):
    return _repo(tmp_path / "acme")


def _names(listing):
    return [b["branch"] for b in listing["branches"]]


# --- Listing --------------------------------------------------------------------------


def test_lists_branches_with_work_the_default_branch_lacks(repo):
    worktree = _branch(repo, "feature")
    _git(repo, "branch", "already-in")
    _branch(repo, "merged-one")
    _git(repo, "merge", "-q", "--no-edit", "merged-one")

    listing = changelog.mergeable_branches(str(repo))

    assert listing["default_branch"] == "master"
    assert listing["checkout"] == str(repo)
    assert _names(listing) == ["feature"]
    feature = listing["branches"][0]
    assert feature["ahead"] == 1 and feature["behind"] == 1
    assert feature["subject"] == "Work on feature"
    assert feature["worktree"] == str(worktree)
    assert feature["conflicts"] == []


def test_newest_work_first(repo):
    _branch(repo, "older")
    subprocess.run(["sleep", "1.1"], check=True)
    _branch(repo, "newer")

    assert _names(changelog.mergeable_branches(str(repo))) == ["newer", "older"]


def test_flags_a_branch_that_would_conflict(repo):
    _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    _commit_on_master(repo)

    (clash,) = changelog.mergeable_branches(str(repo))["branches"]

    assert clash["conflicts"] == ["notes.txt"]


def test_a_branch_with_non_utf8_content_still_lists(repo):
    worktree = _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    (worktree / "image.bin").write_bytes(b"\x89PNG\r\n\x9c\xff\xfe\x00binary")
    _git(worktree, "add", "image.bin")
    _git(worktree, "commit", "-q", "-m", "Add a binary file")
    _commit_on_master(repo)

    (clash,) = changelog.mergeable_branches(str(repo))["branches"]

    assert clash["conflicts"] == ["notes.txt"]


def test_a_dirty_default_checkout_blocks_merging(repo):
    _branch(repo, "feature")
    (repo / "notes.txt").write_text("edited, not committed\n")

    listing = changelog.mergeable_branches(str(repo))

    assert "uncommitted changes" in listing["blocked"]


def test_untracked_files_do_not_block(repo):
    _branch(repo, "feature")
    (repo / "scratch.log").write_text("untracked\n")

    assert changelog.mergeable_branches(str(repo))["blocked"] is None


def test_not_a_repository_lists_nothing(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()

    assert changelog.mergeable_branches(str(folder)) == {"default_branch": None, "checkout": None,
                                                          "main_checkout": None, "blocked": None, "branches": []}


def test_the_command_shown_is_the_one_that_runs(repo):
    _branch(repo, "feature")

    (feature,) = changelog.mergeable_branches(str(repo))["branches"]

    assert feature["command"] == f"git -C {repo} merge --no-edit feature"


# --- Merging --------------------------------------------------------------------------


def test_merges_into_the_checkout_that_has_the_default_branch(repo):
    _branch(repo, "feature")
    _commit_on_master(repo)

    result = changelog.merge_branch(str(repo), "feature")

    assert result["ok"], result
    assert result["into"] == "master" and result["checkout"] == str(repo)
    assert (repo / "feature.txt").read_text() == "new\n"
    assert _git(repo, "merge-base", "--is-ancestor", "feature", "master") == ""
    assert changelog.mergeable_branches(str(repo))["branches"] == []


def test_a_fast_forward_needs_no_merge_commit(repo):
    _branch(repo, "feature")

    result = changelog.merge_branch(str(repo), "feature")

    assert result["ok"] and result["fast_forward"]
    assert _git(repo, "rev-parse", "master") == _git(repo, "rev-parse", "feature")


def test_a_conflict_is_aborted_and_nothing_changes(repo):
    _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    _commit_on_master(repo)
    before = _git(repo, "rev-parse", "master")

    result = changelog.merge_branch(str(repo), "clash")

    assert not result["ok"]
    assert result["conflicts"] == ["notes.txt"]
    assert "notes.txt" in result["error"]
    assert _git(repo, "rev-parse", "master") == before
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (repo / "notes.txt").read_text() == "one\nTWO\nthree\n"


def test_refuses_when_the_default_checkout_has_uncommitted_changes(repo):
    _branch(repo, "feature")
    (repo / "notes.txt").write_text("half-done edit\n")
    before = _git(repo, "rev-parse", "master")

    result = changelog.merge_branch(str(repo), "feature")

    assert not result["ok"] and "uncommitted changes" in result["error"]
    assert _git(repo, "rev-parse", "master") == before
    assert (repo / "notes.txt").read_text() == "half-done edit\n"


def test_default_branch_not_checked_out_fast_forwards_the_ref(repo):
    _branch(repo, "feature")
    _git(repo, "switch", "-q", "-c", "elsewhere")

    result = changelog.merge_branch(str(repo), "feature")

    assert result["ok"] and result["fast_forward"] and result["checkout"] is None
    assert _git(repo, "rev-parse", "master") == _git(repo, "rev-parse", "feature")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"


def test_default_branch_not_checked_out_merges_in_a_checkout_it_cleans_up(repo):
    _branch(repo, "feature")
    _commit_on_master(repo)
    _git(repo, "switch", "-q", "-c", "elsewhere")
    worktrees_before = _git(repo, "worktree", "list")

    result = changelog.merge_branch(str(repo), "feature")

    assert result["ok"] and not result["fast_forward"]
    assert _git(repo, "merge-base", "--is-ancestor", "feature", "master") == ""
    assert _git(repo, "show", "master:feature.txt") == "new"
    assert _git(repo, "worktree", "list") == worktrees_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"


def test_a_conflict_in_the_temporary_checkout_changes_nothing(repo):
    _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    _commit_on_master(repo)
    _git(repo, "switch", "-q", "-c", "elsewhere")
    before = _git(repo, "rev-parse", "master")
    worktrees_before = _git(repo, "worktree", "list")

    result = changelog.merge_branch(str(repo), "clash")

    assert not result["ok"] and result["conflicts"] == ["notes.txt"]
    assert _git(repo, "rev-parse", "master") == before
    assert _git(repo, "worktree", "list") == worktrees_before


@pytest.mark.parametrize("name", ["nope", "--upload-pack=touch /tmp/x", "", "master"])
def test_refuses_anything_but_another_local_branch(repo, name):
    before = _git(repo, "rev-parse", "master")

    result = changelog.merge_branch(str(repo), name)

    assert not result["ok"] and result["error"]
    assert _git(repo, "rev-parse", "master") == before


def test_an_already_merged_branch_is_left_alone(repo):
    _git(repo, "branch", "old")

    result = changelog.merge_branch(str(repo), "old")

    assert result["ok"] and result["already"]


# --- Dashboard ------------------------------------------------------------------------


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, repo):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repository = db.create_repository(conn, project["id"], "acme", str(repo))
    return project, repository, repo


def _finished_task(acme, branch, title="Fix the notes column"):
    project, repository, _ = acme
    conn = db.get_connection()
    task = db.create_task(conn, project_id=project["id"], title=title, repository_id=repository["id"])
    assert db.complete_task(conn, task, note="Fixed", branch=branch)
    return task


def _section(html):
    match = re.search(r'<section[^>]*id="ready-to-merge".*?</section>', html, re.S)
    return match.group(0) if match else ""


def test_the_project_page_lists_branches_ready_to_merge(client, acme):
    project, repository, repo = acme
    _branch(repo, "worktree-notes")
    task = _finished_task(acme, "worktree-notes")

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert "Ready to merge" in section
    assert "worktree-notes" in section
    assert f'href="/decisions/{task}"' in section
    assert f"git -C {repo} merge --no-edit worktree-notes" in section
    assert 'action="/projects/%d/branches/merge"' % project["id"] in section


def test_the_section_says_when_nothing_is_waiting(client, acme):
    project, _, _ = acme

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert "Nothing waiting to be merged" in section


def test_merge_from_the_project_page(client, acme):
    project, repository, repo = acme
    _branch(repo, "feature")

    response = client.post(f"/projects/{project['id']}/branches/merge",
                           data={"repository": repository["id"], "branch": "feature"}, follow_redirects=False)

    assert response.status_code == 303 and "#ready-to-merge" in response.headers["location"]
    assert (repo / "feature.txt").exists()
    page = client.get(response.headers["location"]).text
    assert "Merged <code>feature</code> into <code>master</code>" in page
    assert 'name="branch" value="feature"' not in page


def test_a_failed_merge_from_the_project_page_says_why(client, acme):
    project, repository, repo = acme
    _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    _commit_on_master(repo)

    response = client.post(f"/projects/{project['id']}/branches/merge",
                           data={"repository": repository["id"], "branch": "clash"})

    assert "Couldn't merge" in response.text and "notes.txt" in response.text


def test_merging_needs_a_folder_of_that_project(client, acme, tmp_path):
    project, _, _ = acme
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "other")
    other_repo = _repo(tmp_path / "other")
    _branch(other_repo, "feature")
    foreign = db.create_repository(conn, other["id"], "other", str(other_repo))

    response = client.post(f"/projects/{project['id']}/branches/merge",
                           data={"repository": foreign["id"], "branch": "feature"}, follow_redirects=False)

    assert response.status_code == 404
    assert not (other_repo / "feature.txt").exists()


def test_a_finished_task_offers_to_merge_its_branch(client, acme):
    _, _, repo = acme
    _branch(repo, "worktree-notes")
    task = _finished_task(acme, "worktree-notes")

    page = client.get(f"/decisions/{task}").text

    assert f'action="/decisions/{task}/merge"' in page
    assert "Merge into master</button>" in page
    assert f"git -C {repo} merge --no-edit worktree-notes" in page


def test_merging_from_the_item_page(client, acme):
    _, _, repo = acme
    _branch(repo, "worktree-notes")
    task = _finished_task(acme, "worktree-notes")

    response = client.post(f"/decisions/{task}/merge", follow_redirects=False)

    assert response.status_code == 303
    assert (repo / "feature.txt").exists()
    page = client.get(response.headers["location"]).text
    assert f'action="/decisions/{task}/merge"' not in page
    assert "merged into <code>master</code>" in page


def test_no_merge_button_once_merged_or_for_an_unknown_branch(client, acme):
    _, _, repo = acme
    _branch(repo, "done-already")
    _git(repo, "merge", "-q", "--no-edit", "done-already")
    merged = _finished_task(acme, "done-already")
    unknown = _finished_task(acme, "see the PR on GitHub", title="Something else")

    assert f'action="/decisions/{merged}/merge"' not in client.get(f"/decisions/{merged}").text
    assert f'action="/decisions/{unknown}/merge"' not in client.get(f"/decisions/{unknown}").text
    assert client.post(f"/decisions/{unknown}/merge", follow_redirects=False).status_code == 303


def test_a_merge_conflict_on_the_item_page_says_why(client, acme):
    _, _, repo = acme
    _branch(repo, "clash", filename="notes.txt", content="one\nzwei\nthree\n")
    _commit_on_master(repo)
    task = _finished_task(acme, "clash")

    page = client.post(f"/decisions/{task}/merge").text

    assert "Couldn't merge" in page and "notes.txt" in page
    assert f'action="/decisions/{task}/merge"' in page


# --- Who made a branch ----------------------------------------------------------------


def _session_row(claude_session_id):
    return db.get_session(db.get_connection(), claude_session_id)["id"]


def test_a_branch_links_the_session_that_worked_in_its_worktree_and_the_task_it_claimed(client, acme):
    project, repository, repo = acme
    worktree = _branch(repo, "worktree-notes")
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(worktree))
    task = db.create_task(conn, project_id=project["id"], title="Fix the notes column", repository_id=repository["id"])
    assert db.claim_task(conn, task, "s-1")

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert f'href="/decisions/{task}"' in section
    assert f'href="/sessions/{_session_row("s-1")}"' in section


def test_a_branch_links_the_item_its_background_session_was_started_for(client, acme):
    project, repository, repo = acme
    worktree = _branch(repo, "worktree-notes")
    conn = db.get_connection()
    db.register_session(conn, "s-2", str(worktree))
    conn.execute("UPDATE sessions SET agent_id = 'ab12cd34' WHERE claude_session_id = 's-2'")
    conn.commit()
    item = _finished_task(acme, "somewhere-else", title="Started from the dashboard")
    db.record_dispatch(conn, item, "ab12cd34", resumed=False)

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert f'href="/decisions/{item}"' in section
    assert f'href="/sessions/{_session_row("s-2")}"' in section


def test_a_branch_checked_out_in_the_main_checkout_names_no_session(client, acme):
    project, repository, repo = acme
    _git(repo, "switch", "-q", "-c", "local-work")
    _commit_on_master(repo)
    conn = db.get_connection()
    db.register_session(conn, "s-3", str(repo))
    task = db.create_task(conn, project_id=project["id"], title="Unrelated", repository_id=repository["id"])
    db.claim_task(conn, task, "s-3")

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert "local-work" in section
    assert 'href="/sessions/' not in section and f'href="/decisions/{task}"' not in section


def test_a_subagent_worktree_links_its_parent_session_and_the_item_it_names(client, acme, tmp_path, monkeypatch):
    import json
    from apo import claude_runtime

    project, repository, repo = acme
    worktree = repo / ".claude" / "worktrees" / "agent-a1b2c3"
    _git(repo, "worktree", "add", "-q", "-b", "worktree-agent-a1b2c3", str(worktree))
    (worktree / "feature.txt").write_text("new\n")
    _git(worktree, "add", "feature.txt")
    _git(worktree, "commit", "-q", "-m", "Subagent work")
    conn = db.get_connection()
    db.register_session(conn, "parent-1", str(repo))
    task = db.create_task(conn, project_id=project["id"], title="Fix the notes column", repository_id=repository["id"])
    subagents = tmp_path / "claude" / "projects" / "-acme" / "parent-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a1b2c3.meta.json").write_text(json.dumps({
        "worktreePath": str(worktree), "worktreeBranch": "worktree-agent-a1b2c3",
        "description": f"#{task} Fix the notes column"}))
    monkeypatch.setattr(claude_runtime, "claude_config_dirs", lambda: [tmp_path / "claude"])

    section = _section(client.get(f"/projects/{project['id']}").text)

    assert f'href="/decisions/{task}"' in section
    assert f'href="/sessions/{_session_row("parent-1")}"' in section
    assert "Fix the notes column" in section
