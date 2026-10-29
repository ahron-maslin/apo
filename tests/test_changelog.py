"""What changed in a project, from its folders' git history and GitHub pull requests:
local, read-only, no tokens. It tells "agents were busy" apart from "the project moved"."""
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


def _git(path, *args, at=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Claude", GIT_AUTHOR_EMAIL="c@x", GIT_COMMITTER_NAME="Claude",
               GIT_COMMITTER_EMAIL="c@x")
    if at:
        env.update(GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)


def _repo(path, commits):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    for subject, at in commits:
        _git(path, "commit", "-q", "--allow-empty", "-m", subject, at=at)
    return path


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    repo = _repo(tmp_path / "acme", [("Initial import", _ago(days=30)), ("Add the ELK layout", _ago(days=2)),
                                     ("Fix the export button", _ago(hours=1))])
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(repo))
    return project, repo


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    prs = []
    script = tmp_path / "fake-gh"
    data = tmp_path / "prs.json"
    script.write_text(f"#!/bin/sh\ncat {data}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_GH_BIN", str(script))

    def set_prs(items):
        prs[:] = items
        data.write_text(json.dumps(items))

    set_prs([])
    return set_prs


def test_commits_since_a_moment_newest_first(acme):
    from apo import changelog

    _, repo = acme

    found = changelog.commits(str(repo), since=_ago(days=7))

    assert [c["subject"] for c in found] == ["Fix the export button", "Add the ELK layout"]
    assert all(len(c["short"]) >= 7 and c["at"] for c in found)


def test_a_folder_that_isnt_a_git_repo_has_no_commits(tmp_path):
    from apo import changelog

    (tmp_path / "plain").mkdir()

    assert changelog.commits(str(tmp_path / "plain"), since=_ago(days=7)) == []
    assert changelog.commits(str(tmp_path / "missing"), since=_ago(days=7)) == []


def test_commits_on_any_branch_count(acme):
    from apo import changelog

    _, repo = acme
    _git(repo, "checkout", "-q", "-b", "feature")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Work on a branch", at=_ago(minutes=5))
    _git(repo, "checkout", "-q", "main")

    assert "Work on a branch" in [c["subject"] for c in changelog.commits(str(repo), since=_ago(days=1))]


def test_pull_requests_come_from_a_background_refresh(acme, fake_gh):
    from apo import changelog

    _, repo = acme
    fake_gh([{"number": 12, "title": "ELK layout for large diagrams", "state": "MERGED", "mergedAt": _ago(hours=3),
              "createdAt": _ago(days=1), "url": "https://github.com/me/acme/pull/12", "isDraft": False}])

    assert changelog.pull_requests(str(repo)) == []
    changelog.refresh_pull_requests(str(repo))

    (pr,) = changelog.pull_requests(str(repo))
    assert pr["number"] == 12 and pr["state"] == "MERGED"


def test_a_folder_without_github_is_remembered_and_not_asked_every_time(acme, tmp_path, monkeypatch):
    from apo import changelog

    _, repo = acme
    calls = tmp_path / "calls"
    script = tmp_path / "failing-gh"
    script.write_text(f"#!/bin/sh\necho x >> {calls}\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_GH_BIN", str(script))

    changelog.refresh_pull_requests(str(repo))
    changelog.refresh_pull_requests(str(repo))

    assert changelog.pull_requests(str(repo)) == []
    assert calls.read_text().count("x") == 1


def test_one_commit_seen_from_two_folders_counts_once(db_path, tmp_path):
    from apo import changelog

    repo = _repo(tmp_path / "acme-shop", [("Wire the map", _ago(hours=2))])
    (repo / "dashboard").mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "Globex")
    db.create_repository(conn, project["id"], "acme-shop", str(repo))
    db.create_repository(conn, project["id"], "dashboard", str(repo / "dashboard"))

    changes = changelog.project_changes(db.project_repositories(conn, project["id"]), since=_ago(days=7))

    assert [c["subject"] for c in changes["commits"]] == ["Wire the map"]


def test_the_overview_says_what_changed_by_day(client, acme, fake_gh):
    from apo import changelog

    project, repo = acme
    fake_gh([{"number": 12, "title": "ELK layout for large diagrams", "state": "MERGED", "mergedAt": _ago(hours=3),
              "createdAt": _ago(days=1), "url": "https://github.com/me/acme/pull/12", "isDraft": False}])
    changelog.refresh_pull_requests(str(repo))

    html = client.get(f"/projects/{project['id']}").text
    card = re.search(r'<section[^>]*id="what-changed".*?</section>', html, re.S).group(0)

    assert "2 commits" in card and "1 PR merged" in card
    assert "Fix the export button" in card and "Add the ELK layout" in card and "Initial import" not in card
    assert "ELK layout for large diagrams" in card and 'href="https://github.com/me/acme/pull/12"' in card
    assert "Today" in card or "Yesterday" in card


def test_a_project_with_no_code_changes_says_so(client, db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "quiet")
    db.ensure_repository_for_project(conn, project["id"], str(_repo(tmp_path / "quiet", [("Old", _ago(days=60))])))

    card = re.search(r'<section[^>]*id="what-changed".*?</section>', client.get(f"/projects/{project['id']}").text, re.S)

    assert card and "No commits in the last 7 days" in card.group(0)


def test_whats_new_since_your_last_look_is_marked(client, acme):
    project, repo = acme
    conn = db.get_connection()
    db.set_ui_state(conn, f"project_seen:{project['id']}", _ago(hours=5))

    card = re.search(r'<section[^>]*id="what-changed".*?</section>', client.get(f"/projects/{project['id']}").text,
                     re.S).group(0)

    fresh = re.search(r'<li[^>]*class="[^"]*is-new[^"]*".*?</li>', card, re.S).group(0)
    assert "Fix the export button" in fresh
    assert len(re.findall(r'class="[^"]*is-new', card)) == 1
    assert "1 new since you last looked" in card


def test_the_marker_holds_while_you_browse_and_moves_after_a_break(acme):
    project, _ = acme
    conn = db.get_connection()

    assert db.project_look(conn, project["id"]) is None
    first = db.get_ui_state(conn, f"project_seen:{project['id']}")
    assert db.project_look(conn, project["id"]) is None

    db.set_ui_state(conn, f"project_seen:{project['id']}", _ago(hours=2))
    baseline = db.project_look(conn, project["id"])
    assert baseline is not None and baseline < first
    assert db.project_look(conn, project["id"]) == baseline


def test_activity_lists_commits_among_the_days_events(client, acme):
    project, _ = acme

    html = client.get(f"/projects/{project['id']}/activity").text

    assert "Fix the export button" in html and "Add the ELK layout" in html


def test_a_pull_request_with_githubs_own_timestamp_shows_on_the_overview(client, acme, fake_gh):
    from apo import changelog

    project, repo = acme
    merged = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_gh([{"number": 3, "title": "Show more opens a modal", "state": "MERGED", "mergedAt": merged,
              "createdAt": merged, "url": "https://github.com/me/acme/pull/3", "isDraft": False}])
    changelog.refresh_pull_requests(str(repo))

    response = client.get(f"/projects/{project['id']}")

    assert response.status_code == 200
    assert "Show more opens a modal" in response.text
