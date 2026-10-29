"""A status you can paste: done, in progress, waiting on you, stuck and next, for one
project or all, over a date range, as Markdown or JSON, from the dashboard and the CLI."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db
from test_cli import run_cli


def _at(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def world(db_path):
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    other = db.get_or_create_project(conn, "other")
    pid = acme["id"]
    done = db.create_task(conn, project_id=pid, title="Add rate limiting")
    db.claim_task(conn, done, "s1")
    db.complete_task(conn, done, note="Shipped")
    conn.execute("UPDATE decision_requests SET resolved_at = ? WHERE id = ?", (_at(2), done))
    old = db.create_task(conn, project_id=pid, title="Ancient chore")
    db.complete_task(conn, old)
    conn.execute("UPDATE decision_requests SET resolved_at = ? WHERE id = ?", (_at(40), old))
    working = db.create_task(conn, project_id=pid, title="Build search")
    db.claim_task(conn, working, "s2")
    ready = db.create_task(conn, project_id=pid, title="Write docs")
    stuck = db.create_task(conn, project_id=pid, title="Lost work")
    db.claim_task(conn, stuck, "s3")
    conn.execute("INSERT INTO sessions (claude_session_id, started_at, status, last_seen_at) VALUES ('s3', ?, 'ended', ?)",
                 (_at(1), _at(1)))
    foreign = db.create_task(conn, project_id=other["id"], title="Other thing")
    conn.commit()
    return dict(conn=conn, pid=pid, other=other["id"], done=done, old=old, working=working, ready=ready, stuck=stuck,
                foreign=foreign)


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def test_status_report_sections(world):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    report = db.status_report(world["conn"], world["pid"], since)
    (project,) = report["projects"]
    assert project["name"] == "acme"
    assert [i["id"] for i in project["done"]] == [world["done"]]
    assert world["working"] in [i["id"] for i in project["in_progress"]]
    assert world["stuck"] in [i["id"] for i in project["stuck"]]
    assert world["stuck"] not in [i["id"] for i in project["in_progress"]]
    assert world["ready"] in [i["id"] for i in project["next"]]
    assert report["since"] and report["until"]


def test_status_report_all_projects(world):
    report = db.status_report(world["conn"], None, datetime.now(timezone.utc) - timedelta(days=7))
    assert {p["name"] for p in report["projects"]} == {"acme", "other"}


def test_status_markdown_reads_as_a_status(world):
    report = db.status_report(world["conn"], world["pid"], datetime.now(timezone.utc) - timedelta(days=7))
    md = db.status_markdown(report)
    assert md.startswith("# Status")
    assert "## acme" in md
    assert "**Done (1)**" in md and "#%d Add rate limiting" % world["done"] in md
    assert "**In progress" in md and "**Stuck" in md and "**Next" in md
    assert "Ancient chore" not in md


def test_status_route_markdown_and_json(world, client):
    pid = world["pid"]
    r = client.get(f"/projects/{pid}/status.md?since=7d")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "Add rate limiting" in r.text and "Other thing" not in r.text
    r = client.get(f"/projects/{pid}/status.json")
    assert r.json()["projects"][0]["name"] == "acme"
    r = client.get("/projects/status.md")
    assert "## other" in r.text
    assert client.get("/projects/status.md?since=garbage").status_code == 400
    assert client.get("/projects/9999/status.md").status_code == 404


def test_status_download_header(world, client):
    r = client.get(f"/projects/{world['pid']}/status.md?download=1")
    assert "attachment" in r.headers["content-disposition"]


def test_report_and_overview_pages_offer_copy_and_download(world, client):
    pid = world["pid"]
    for url in (f"/projects/{pid}", f"/projects/{pid}/report", "/projects/report"):
        html = client.get(url).text
        assert "Copy status" in html and "status.md" in html


def test_cli_status(world, db_path):
    env = dict(os.environ, APO_DB=str(db_path))
    out = run_cli("status", "--project", "acme", "--since", "7d", env=env)
    assert out.returncode == 0, out.stderr
    assert "Add rate limiting" in out.stdout and "Other thing" not in out.stdout
    js = run_cli("status", "--format", "json", env=env)
    assert {p["name"] for p in json.loads(js.stdout)["projects"]} == {"acme", "other"}
    assert run_cli("status", "--since", "nope", env=env).returncode != 0
    assert run_cli("status", "--project", "ghost", env=env).returncode != 0
