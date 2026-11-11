"""Due dates act (overdue / due soon buckets, sort by due, a line in the away digest) and
stuck work (open or claimed for STUCK_ITEM_DAYS with the reason) — all derived, no LLM."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project, repo_id


def _days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def _age(conn, item_id, days, column="created_at"):
    at = (datetime.now(timezone.utc) - timedelta(days=days, hours=1)).isoformat()
    conn.execute(f"UPDATE decision_requests SET {column} = ? WHERE id = ?", (at, item_id))
    conn.commit()


def _all(work):
    return [i for key in ("needs_you", "ready", "in_progress", "waiting") for i in work[key]]


def test_due_buckets(acme):
    assert db.due_bucket(_days(-1)) == "overdue"
    assert db.due_bucket(_days(0)) == "soon"
    assert db.due_bucket(_days(db.DUE_SOON_DAYS)) == "soon"
    assert db.due_bucket(_days(db.DUE_SOON_DAYS + 1)) is None
    assert db.due_bucket(None) is None


def test_work_collects_overdue_and_due_soon_buckets(acme):
    conn, project, repo_id = acme
    late = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Late")
    soon = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Soon")
    far = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Far")
    done = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Done late")
    for item, offset in ((late, -3), (soon, 1), (far, 30), (done, -3)):
        db.set_due(conn, item, _days(offset))
    db.claim_task(conn, done, "s")
    db.complete_task(conn, done)

    work = db.project_work(conn, project["id"])

    assert [i["id"] for i in work["overdue"]] == [late]
    assert [i["id"] for i in work["due_soon"]] == [soon]
    assert {i["id"]: i["due_bucket"] for i in _all(work)}[far] is None


def test_work_can_sort_by_due_with_undated_last(acme):
    conn, project, repo_id = acme
    undated = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Undated")
    later = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Later")
    sooner = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Sooner")
    db.set_due(conn, later, _days(9))
    db.set_due(conn, sooner, _days(2))

    work = db.sort_work_by_due(db.project_work(conn, project["id"]))

    assert [i["id"] for i in work["ready"]] == [sooner, later, undated]


def test_an_item_waiting_on_you_for_days_is_stuck_with_that_reason(acme):
    conn, project, repo_id = acme
    old = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Old", question="?")
    fresh = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Fresh", question="?")
    _age(conn, old, db.STUCK_ITEM_DAYS + 2)

    work = db.project_work(conn, project["id"])
    by_id = {i["id"]: i for i in work["needs_you"]}

    assert by_id[old]["stuck"]["days"] == db.STUCK_ITEM_DAYS + 2
    assert by_id[old]["stuck"]["reason"] == "waiting on you"
    assert by_id[fresh]["stuck"] is None
    assert [i["id"] for i in work["stuck"]] == [old]


def test_a_claimed_task_with_no_activity_is_stuck_and_an_unclaimed_one_is_not_picked_up(acme):
    conn, project, repo_id = acme
    claimed = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Claimed")
    idle = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Idle")
    db.claim_task(conn, claimed, "s")
    _age(conn, claimed, db.STUCK_ITEM_DAYS + 1, "claimed_at")
    _age(conn, idle, db.STUCK_ITEM_DAYS + 1)

    work = db.project_work(conn, project["id"])

    assert work["in_progress"][0]["stuck"]["reason"] == "claimed with no activity"
    assert work["ready"][0]["stuck"]["reason"] == "ready, but no agent has picked it up"


def test_now_items_carry_due_and_stuck(acme):
    conn, project, repo_id = acme
    item = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Old", question="?")
    db.set_due(conn, item, _days(-2))
    _age(conn, item, db.STUCK_ITEM_DAYS)

    (row,) = db.now_queue(conn)["your_move"]

    assert row["due_bucket"] == "overdue" and row["stuck"]["reason"] == "waiting on you"


def test_the_away_digest_has_a_due_line(acme):
    conn, project, repo_id = acme
    a = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="A", question="?")
    b = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="B", question="?")
    db.set_due(conn, a, _days(-1))
    db.set_due(conn, b, _days(1))

    digest = db.away_digest(conn, db.latest_event_seq(conn))

    assert digest["due"] == {"overdue": 1, "due_soon": 1, "stuck": 0}


def test_pages_show_the_badges_and_sort(acme):
    conn, project, repo_id = acme
    item = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Old one", question="?")
    db.set_due(conn, item, _days(-2))
    _age(conn, item, db.STUCK_ITEM_DAYS + 1)
    client = TestClient(__import__("apo.web.app", fromlist=["app"]).app)

    for url, stuck in ((f"/projects/{project['id']}/work", f"Stuck for {db.STUCK_ITEM_DAYS + 1} days"),
                       ("/projects/work", f"Stuck for {db.STUCK_ITEM_DAYS + 1} days"),
                       ("/", f"Stuck {db.STUCK_ITEM_DAYS + 1}d")):
        text = client.get(url).text
        assert "Overdue" in text, url
        assert stuck in text, url
    overview = client.get(f"/projects/{project['id']}").text
    assert "1 overdue" in overview and "1 stuck" in overview
    assert client.get(f"/projects/{project['id']}/work?sort=due").status_code == 200
    assert client.get("/projects/work?sort=due").status_code == 200
