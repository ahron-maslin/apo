"""An item an agent closes from a git checkout records where the work is: the branch,
the commit, and whether that commit is in the default branch. Work finished on a side
branch or in a worktree is then findable, instead of looking like it vanished (#103)."""
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from apo import db
from test_cli import run_cli


def _git(path, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Claude", GIT_AUTHOR_EMAIL="c@x", GIT_COMMITTER_NAME="Claude",
               GIT_COMMITTER_EMAIL="c@x")
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _repo(path, default="master"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", default)
    _git(path, "commit", "-q", "--allow-empty", "-m", "Initial")
    return path


def _commit_on(path, branch):
    _git(path, "switch", "-q", "-c", branch)
    _git(path, "commit", "-q", "--allow-empty", "-m", f"Work on {branch}")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    repo = _repo(tmp_path / "acme")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repository = db.create_repository(conn, project["id"], "acme", str(repo))
    return project, repository, repo


def _task(acme, title="Write the Stripe checklist"):
    project, repository, _ = acme
    return db.create_task(db.get_connection(), project_id=project["id"], title=title,
                          repository_id=repository["id"])


def _answered_decision(acme, title="Which payment provider?"):
    project, repository, _ = acme
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=project["id"], repository_id=repository["id"],
                                             title=title, question="Stripe or Paddle?")
    db.answer_decision_request(conn, decision_id, "Stripe.")
    return decision_id


def _location(item_id):
    return db.get_decision_request(db.get_connection(), item_id)["work_location"]


def test_a_task_completed_on_a_side_branch_records_that_it_is_not_merged(acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    task = _task(acme)

    assert db.complete_task(db.get_connection(), task, note="Wrote it", cwd=str(repo))

    assert _location(task) == {"branch": "feature", "commit": head, "default_branch": "master", "merged": False}


def test_work_already_in_the_default_branch_counts_as_merged(acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    _git(repo, "switch", "-q", "master")
    _git(repo, "merge", "-q", "--ff-only", "feature")
    _git(repo, "switch", "-q", "feature")
    task = _task(acme)

    db.complete_task(db.get_connection(), task, cwd=str(repo))

    assert _location(task) == {"branch": "feature", "commit": head, "default_branch": "master", "merged": True}


def test_work_on_the_default_branch_itself(acme):
    _, _, repo = acme
    task = _task(acme)

    db.complete_task(db.get_connection(), task, cwd=str(repo))

    assert _location(task)["branch"] == "master" and _location(task)["merged"] is True


def test_a_worktree_records_its_own_branch(acme, tmp_path):
    _, _, repo = acme
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "worktree-stripe-live-checklist", str(worktree))
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "Checklist")
    task = _task(acme)

    db.complete_task(db.get_connection(), task, note="Wrote the checklist", cwd=str(worktree))

    assert _location(task) == {"branch": "worktree-stripe-live-checklist", "commit": _git(worktree, "rev-parse", "HEAD"),
                               "default_branch": "master", "merged": False}


def test_the_default_branch_comes_from_origin_head_first(db_path, tmp_path):
    origin = _repo(tmp_path / "origin", default="trunk")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "branch", "main")
    head = _commit_on(clone, "work")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "clone")
    repository = db.create_repository(conn, project["id"], "clone", str(clone))
    task = db.create_task(conn, project_id=project["id"], title="T", repository_id=repository["id"])

    db.complete_task(conn, task, cwd=str(clone))

    assert _location(task) == {"branch": "work", "commit": head, "default_branch": "trunk", "merged": False}


def test_without_a_default_branch_whether_it_is_merged_is_unknown(db_path, tmp_path):
    repo = _repo(tmp_path / "dev-only", default="dev")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "dev-only")
    repository = db.create_repository(conn, project["id"], "dev-only", str(repo))
    task = db.create_task(conn, project_id=project["id"], title="T", repository_id=repository["id"])

    db.complete_task(conn, task, cwd=str(repo))

    assert _location(task) == {"branch": "dev", "commit": _git(repo, "rev-parse", "HEAD"), "default_branch": None,
                               "merged": None}


def test_a_detached_head_records_no_branch(acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    _git(repo, "switch", "-q", "--detach")
    task = _task(acme)

    db.complete_task(db.get_connection(), task, cwd=str(repo))

    assert _location(task) == {"branch": None, "commit": head, "default_branch": "master", "merged": False}


def test_outside_a_git_checkout_nothing_is_recorded_and_the_task_still_completes(db_path, tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "plain")
    repository = db.create_repository(conn, project["id"], "plain", str(folder))
    task = db.create_task(conn, project_id=project["id"], title="T", repository_id=repository["id"])

    assert db.complete_task(conn, task, note="Done", cwd=str(folder))

    assert db.get_decision_request(conn, task)["status"] == "resolved" and _location(task) is None


def test_a_repository_with_no_commits_records_nothing(db_path, tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "empty")
    repository = db.create_repository(conn, project["id"], "empty", str(repo))
    task = db.create_task(conn, project_id=project["id"], title="T", repository_id=repository["id"])

    assert db.complete_task(conn, task, cwd=str(repo))
    assert _location(task) is None


def test_git_failing_outright_records_nothing(acme, monkeypatch):
    _, _, repo = acme
    _commit_on(repo, "feature")
    monkeypatch.setenv("PATH", "/nonexistent")
    task = _task(acme)

    assert db.complete_task(db.get_connection(), task, cwd=str(repo))
    assert _location(task) is None


def test_closing_from_another_repository_records_nothing(acme, tmp_path):
    other = _repo(tmp_path / "other")
    _commit_on(other, "unrelated")
    task = _task(acme)

    assert db.complete_task(db.get_connection(), task, cwd=str(other))
    assert _location(task) is None


def test_a_completion_with_no_folder_records_nothing(acme, monkeypatch):
    """The dashboard closes items too, and its folder is the live install's, not the work's."""
    _, _, repo = acme
    _commit_on(repo, "feature")
    monkeypatch.chdir(repo)
    first, second = _task(acme, "First"), _task(acme, "Second")
    conn = db.get_connection()

    db.complete_task(conn, first)
    db.complete_step(conn, second, by="human")

    assert _location(first) is None and _location(second) is None


def test_resolving_a_decision_records_where_the_work_is(acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    decision_id = _answered_decision(acme)

    assert db.resolve_decision_request(db.get_connection(), decision_id, cwd=str(repo))

    assert _location(decision_id) == {"branch": "feature", "commit": head, "default_branch": "master",
                                      "merged": False}


def test_the_mcp_tools_record_from_the_folder_the_session_runs_in(acme, monkeypatch):
    from apo import mcp_server

    _, _, repo = acme
    _commit_on(repo, "feature")
    monkeypatch.chdir(repo)
    task, decision_id = _task(acme), _answered_decision(acme)

    assert mcp_server.complete_task(task_id=task, note="Wrote it") == {"completed": True}
    assert mcp_server.mark_decision_resolved(decision_id) == {"resolved": True}

    assert _location(task)["branch"] == "feature" and _location(decision_id)["branch"] == "feature"


def test_the_cli_records_from_the_folder_it_runs_in(acme, db_path):
    _, _, repo = acme
    _commit_on(repo, "feature")
    task, decision_id = _task(acme), _answered_decision(acme)
    env = dict(os.environ, APO_DB=str(db_path))

    assert run_cli("complete", str(task), "--note", "Wrote it", env=env, cwd=str(repo)).returncode == 0
    assert run_cli("resolve", str(decision_id), env=env, cwd=str(repo)).returncode == 0

    assert _location(task)["branch"] == "feature" and _location(decision_id)["branch"] == "feature"


@pytest.mark.parametrize("location, phrase", [
    ({"branch": "worktree-stripe-live-checklist", "commit": "abc1234def5678", "default_branch": "master",
      "merged": False},
     "on branch `worktree-stripe-live-checklist` (commit abc1234) — not merged into `master`"),
    ({"branch": "feature", "commit": "abc1234def5678", "default_branch": "main", "merged": True},
     "on branch `feature` (commit abc1234) — merged into `main`"),
    ({"branch": "master", "commit": "abc1234def5678", "default_branch": "master", "merged": True},
     "on branch `master` (commit abc1234)"),
    ({"branch": None, "commit": "abc1234def5678", "default_branch": "master", "merged": False},
     "at commit abc1234, not on any branch — not merged into `master`"),
    ({"branch": "dev", "commit": "abc1234def5678", "default_branch": None, "merged": None},
     "on branch `dev` (commit abc1234)"),
])
def test_the_wording_says_where_the_work_is(location, phrase):
    assert db.work_location_phrase(location) == phrase


def test_the_closed_file_says_where_the_work_is(acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    task = _task(acme)

    db.complete_task(db.get_connection(), task, note="Wrote it", cwd=str(repo))

    text = (repo / ".claude" / "decisions" / f"{task:04d}-write-the-stripe-checklist.md").read_text()
    closed = re.search(r"^- \*\*Closed:\*\* .*\n(.*)$", text, re.M).group(1)
    assert closed == (f"- **Where the work is:** on branch `feature` (commit {head[:7]}) — not merged into "
                      "`master` when it closed")


def test_a_file_closed_without_a_location_says_nothing_about_it(acme):
    task = _task(acme)

    db.complete_task(db.get_connection(), task, note="Wrote it")

    _, _, repo = acme
    assert "Where the work is" not in (repo / ".claude" / "decisions" / f"{task:04d}-write-the-stripe-checklist.md").read_text()


def test_a_done_task_page_says_where_the_work_is(client, acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    task = _task(acme)
    db.complete_task(db.get_connection(), task, note="Wrote it", cwd=str(repo))

    html = client.get(f"/decisions/{task}").text

    assert (f"Work is on branch <code>feature</code> (commit {head[:7]}) — not merged into "
            "<code>master</code>.") in html


def test_a_resolved_decision_page_says_where_the_work_is(client, acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    decision_id = _answered_decision(acme)
    db.resolve_decision_request(db.get_connection(), decision_id, cwd=str(repo))

    html = client.get(f"/decisions/{decision_id}").text

    assert (f"Work is on branch <code>feature</code> (commit {head[:7]}) — not merged into "
            "<code>master</code>.") in html


def test_the_page_notices_once_the_work_is_merged(client, acme):
    _, _, repo = acme
    head = _commit_on(repo, "feature")
    task = _task(acme)
    db.complete_task(db.get_connection(), task, cwd=str(repo))
    _git(repo, "switch", "-q", "master")
    _git(repo, "merge", "-q", "--ff-only", "feature")

    html = client.get(f"/decisions/{task}").text

    assert f"Work is on branch <code>feature</code> (commit {head[:7]}) — merged into <code>master</code>." in html


def test_a_goal_step_done_on_an_unmerged_branch_says_so(client, acme):
    project, repository, repo = acme
    head = _commit_on(repo, "feature")
    conn = db.get_connection()
    outcome_id = db.create_outcome(conn, project["id"], "Go live")
    step = db.add_step(conn, outcome_id, "Write the Stripe checklist", owner="agent", repository_id=repository["id"])
    db.complete_task(conn, step, note="Wrote it", cwd=str(repo))

    html = client.get(f"/outcomes/{outcome_id}").text

    assert f"Work is on branch <code>feature</code> (commit {head[:7]}) — not merged into <code>master</code>." in html


def test_an_older_database_gains_the_column(db_path):
    conn = db.get_connection()
    conn.execute("ALTER TABLE decision_requests DROP COLUMN work_location")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()

    conn = db.get_connection()

    assert "work_location" in [r[1] for r in conn.execute("PRAGMA table_info(decision_requests)")]
