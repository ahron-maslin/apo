"""A project as the place you work from: a brief (goal, done when, links), a status
(suggested from what's going on, which you can override), one backlog with every
owner's work in it, a log of what was decided, and tabs for its agents and history."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return project


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _age_project(conn, project_id, **delta):
    old = _ago(**delta)
    conn.execute("UPDATE events SET occurred_at = ? WHERE project_id = ?", (old, project_id))
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE project_id = ?", (old, project_id))
    conn.execute("UPDATE projects SET created_at = ? WHERE id = ?", (old, project_id))
    conn.commit()


# --- brief ---

def test_the_brief_holds_the_goal_done_when_and_links(acme):
    conn = db.get_connection()

    db.set_project_brief(conn, acme["id"], goal="Sell the app on Etsy", done_when="First real sale",
                         links="Repo https://github.com/me/acme\nnot a link\nSite https://acme.dev")

    brief = db.get_project_brief(conn, acme["id"])
    assert brief["goal"] == "Sell the app on Etsy" and brief["done_when"] == "First real sale"
    assert brief["links"] == [{"label": "Repo", "url": "https://github.com/me/acme"},
                              {"label": "Site", "url": "https://acme.dev"}]
    assert any(e["verb"] == "project.brief_updated" for e in db.list_events(conn, project_id=acme["id"]))


# --- status ---

def test_status_is_blocked_when_something_has_waited_on_you_for_days(acme):
    conn = db.get_connection()
    db.create_decision_request(conn, project_id=acme["id"], title="Pick a queue", question="?")
    _age_project(conn, acme["id"], days=3)

    status = db.project_status(conn, acme["id"])

    assert status["value"] == "blocked" and "waited on you" in status["reason"]
    assert not status["overridden"]


def test_status_is_at_risk_when_open_work_has_not_moved(acme):
    conn = db.get_connection()
    db.create_task(conn, project_id=acme["id"], title="Fix the scroll bar")
    _age_project(conn, acme["id"], days=6)

    assert db.project_status(conn, acme["id"])["value"] == "at_risk"


def test_status_is_on_track_when_work_is_moving(acme):
    conn = db.get_connection()
    db.create_task(conn, project_id=acme["id"], title="Fix the scroll bar")

    assert db.project_status(conn, acme["id"])["value"] == "on_track"


def test_status_is_quiet_with_nothing_open(acme):
    assert db.project_status(db.get_connection(), acme["id"])["value"] == "quiet"


def test_your_status_overrides_the_suggestion_until_you_clear_it(acme):
    conn = db.get_connection()

    db.set_project_status(conn, acme["id"], "paused", note="Waiting for the client")
    status = db.project_status(conn, acme["id"])
    assert status["value"] == "paused" and status["overridden"] and status["note"] == "Waiting for the client"

    db.set_project_status(conn, acme["id"], None)
    assert db.project_status(conn, acme["id"])["value"] == "quiet"


# --- work ---

def test_work_is_one_list_grouped_by_where_each_thing_stands(acme):
    conn = db.get_connection()
    pid = acme["id"]
    mine = db.create_decision_request(conn, project_id=pid, title="Pick a queue", question="?")
    ready = db.create_task(conn, project_id=pid, title="Write the migration")
    blocker = db.create_task(conn, project_id=pid, title="Design the schema")
    blocked = db.create_task(conn, project_id=pid, title="Backfill rows", blocked_by=[blocker])
    db.claim_task(conn, blocker, "sess-1")
    finished = db.create_task(conn, project_id=pid, title="Add the index")
    db.complete_task(conn, finished, note="Added")

    work = db.project_work(conn, pid)

    def ids(group):
        return [i["id"] for i in work[group]]

    assert ids("needs_you") == [mine]
    assert ids("ready") == [ready]
    assert ids("in_progress") == [blocker]
    assert ids("waiting") == [blocked]
    assert ids("done") == [finished]
    assert work["ready"][0]["owner"] == "agent" and work["needs_you"][0]["owner"] == "you"


def test_adding_work_for_an_agent_or_for_yourself(client, acme):
    client.post(f"/projects/{acme['id']}/items", data={"title": "Add rate limiting", "kind": "task"})
    client.post(f"/projects/{acme['id']}/items", data={"title": "Renew the domain", "kind": "me"})

    work = db.project_work(db.get_connection(), acme["id"])
    assert [i["title"] for i in work["ready"]] == ["Add rate limiting"]
    assert [i["title"] for i in work["needs_you"]] == ["Renew the domain"]


def _done_form(html, item_id):
    """The Done form on item_id's Work row, as (action, hidden fields), or None."""
    form = re.search(rf'<form[^>]*action="(/decisions/{item_id}/answer)"[^>]*>(.*?)</form>', html, re.S)
    if not form:
        return None
    return form.group(1), dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(2)))


def test_only_a_do_it_row_has_its_own_done_button(client, acme):
    conn = db.get_connection()
    pid = acme["id"]
    action = db.add_work(conn, pid, "me", "Renew the domain")
    snoozed = db.add_work(conn, pid, "me", "Call the bank")
    db.snooze_request(conn, snoozed, condition="after lunch")
    decision = db.create_decision_request(conn, project_id=pid, title="Pick a queue", question="?")
    task = db.create_task(conn, project_id=pid, title="Write the migration")

    html = client.get(f"/projects/{pid}/work").text

    assert _done_form(html, action) == (f"/decisions/{action}/answer", {"choice": "done", "next": f"/projects/{pid}/work"})
    assert [other for other in (snoozed, decision, task) if _done_form(html, other)] == []
    assert html.count("data-key-done") == 1


def test_the_work_rows_are_a_key_list(client, acme):
    conn = db.get_connection()
    db.add_work(conn, acme["id"], "me", "Renew the domain")
    db.create_task(conn, project_id=acme["id"], title="Write the migration")

    for page in (f"/projects/{acme['id']}/work", "/projects/work"):
        html = client.get(page).text
        assert html.count("data-keylist") == 1 and html.count("data-keyitem") == 2, page


def test_done_on_a_work_row_closes_the_action_and_comes_back_to_work(client, acme):
    action = db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain")
    url, fields = _done_form(client.get(f"/projects/{acme['id']}/work").text, action)

    resp = client.post(url, data=fields, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == f"/projects/{acme['id']}/work"
    work = db.project_work(db.get_connection(), acme["id"])
    assert [i["id"] for i in work["done"]] == [action] and not work["needs_you"]


def test_done_on_the_all_projects_work_list_comes_back_to_it(client, acme):
    action = db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain")
    url, fields = _done_form(client.get("/projects/work").text, action)

    resp = client.post(url, data=fields, follow_redirects=False)

    assert resp.headers["location"] == "/projects/work"
    assert db.get_decision_request(db.get_connection(), action)["status"] == "resolved"


# --- decisions ---

def test_the_decision_log_is_what_was_decided_newest_first(acme):
    conn = db.get_connection()
    first = db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Postgres or SQLite?")
    second = db.create_decision_request(conn, project_id=acme["id"], title="Cache", question="Redis?")
    db.create_decision_request(conn, project_id=acme["id"], title="Still open", question="?")
    db.answer_decision_request(conn, first, "Postgres")
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(days=2), first))
    conn.commit()
    db.answer_decision_request(conn, second, "No cache yet", source="cli")

    log = db.decision_log(conn, acme["id"])

    assert [d["id"] for d in log] == [second, first]
    assert log[1]["answer_text"] == "Postgres" and log[0]["answer_source"] == "cli"


# --- the tabs ---

def test_overview_tab(client, acme):
    conn = db.get_connection()
    db.set_project_brief(conn, acme["id"], goal="Sell the app on Etsy", links="")
    db.create_outcome(conn, acme["id"], "Launch the listing")
    db.create_decision_request(conn, project_id=acme["id"], title="Pick a queue", question="?")

    text = client.get(f"/projects/{acme['id']}").text

    assert "Sell the app on Etsy" in text
    assert f'href="/projects/{acme["id"]}/settings"' in text
    assert "Launch the listing" in text and "Pick a queue" in text
    for tab in ("work", "decisions", "agents", "activity"):
        assert f'href="/projects/{acme["id"]}/{tab}"' in text


def test_overview_asks_for_a_purpose_when_there_is_none(client, acme):
    assert "No purpose yet" in client.get(f"/projects/{acme['id']}").text


def test_work_tab(client, acme):
    db.report_bug(db.get_connection(), acme["id"], "Crash on save")

    text = client.get(f"/projects/{acme['id']}/work").text

    assert "Crash on save" in text
    assert f'action="/projects/{acme["id"]}/items"' in text


def test_decisions_tab(client, acme):
    conn = db.get_connection()
    decided = db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Postgres or SQLite?")
    db.answer_decision_request(conn, decided, "Postgres")

    text = client.get(f"/projects/{acme['id']}/decisions").text

    assert "Postgres or SQLite?" in text and "Postgres" in text


def test_agents_tab(client, acme, tmp_path):
    db.register_session(db.get_connection(), "s-acme", str(tmp_path))

    text = client.get(f"/projects/{acme['id']}/agents").text

    assert 'id="session-card-' in text and "/start" not in text
    assert f'href="/projects/{acme["id"]}/work#new-work"' in text


def test_activity_tab(client, acme):
    db.create_decision_request(db.get_connection(), project_id=acme["id"], title="Pick a queue", question="?")

    assert "Pick a queue" in client.get(f"/projects/{acme['id']}/activity").text


def test_setting_brief_and_status_from_the_page(client, acme):
    client.post(f"/projects/{acme['id']}/brief", data={"goal": "Ship v2", "links": ""})
    client.post(f"/projects/{acme['id']}/status", data={"status": "at_risk", "note": "Client is slow"})

    conn = db.get_connection()
    assert db.get_project_brief(conn, acme["id"])["goal"] == "Ship v2"
    assert db.project_status(conn, acme["id"])["value"] == "at_risk"

    client.post(f"/projects/{acme['id']}/status", data={"status": ""})
    assert not db.project_status(conn, acme["id"])["overridden"]


def test_a_goal_started_from_the_project_comes_back_to_it(client, acme):
    resp = client.post("/outcomes", data={"project": "acme", "title": "Launch", "next": f"/projects/{acme['id']}"},
                       follow_redirects=False)

    assert resp.headers["location"] == f"/projects/{acme['id']}"
