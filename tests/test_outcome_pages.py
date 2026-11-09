"""The outcome pages: list, create, the checklist with owners, and the moves on it."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path, tmp_path):
    from apo.web import app

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return TestClient(app.app)


def _outcome(**steps):
    conn = db.get_connection()
    project = db.find_project(conn, "acme")
    outcome_id = db.create_outcome(conn, project["id"], "Launch the site", done_when="Live with a payment")
    ids = {name: db.add_step(conn, outcome_id, name, owner=owner) for name, owner in steps.items()}
    return outcome_id, ids


def test_outcomes_page_lists_progress_and_whose_move_is_next(client):
    _outcome(**{"Configure the build": "agent", "Add DNS record": "me"})

    text = client.get("/outcomes").text

    assert "Launch the site" in text and "acme" in text
    assert "0/2" in text
    assert "Configure the build" in text  # the next step
    assert 'action="/outcomes"' in text  # start a new one


def test_creating_an_outcome_from_the_dashboard(client):
    resp = client.post("/outcomes", data={"project": "acme", "title": "Ship v2", "done_when": "Tagged"},
                       follow_redirects=False)

    assert resp.status_code == 303
    outcome = db.get_outcome(db.get_connection(), int(resp.headers["location"].rsplit("/", 1)[1]))
    assert outcome["title"] == "Ship v2" and outcome["created_by"] == "human"


def test_outcome_page_shows_the_checklist_with_owners_and_moves(client):
    outcome_id, ids = _outcome(**{"Configure the build": "agent", "Add DNS record": "me"})

    text = client.get(f"/outcomes/{outcome_id}").text

    assert "Live with a payment" in text
    assert "Configure the build" in text and "Add DNS record" in text
    assert f'action="/items/{ids["Add DNS record"]}/done"' in text
    assert f'action="/items/{ids["Add DNS record"]}/assign"' in text  # give to an agent
    assert f'action="/items/{ids["Configure the build"]}/assign"' in text  # take it
    assert f'action="/outcomes/{outcome_id}/steps"' in text


def test_adding_a_step_with_a_dependency_from_the_page(client):
    outcome_id, ids = _outcome(**{"First": "me"})

    client.post(f"/outcomes/{outcome_id}/steps",
                data={"title": "Second", "owner": "external", "waiting_on": "Apple", "blocked_by": str(ids["First"])})

    steps = db.outcome_progress(db.get_connection(), outcome_id)["steps"]
    assert steps[1]["title"] == "Second" and steps[1]["owner"] == "external"
    assert steps[1]["blocked_by"] == [ids["First"]]


def test_step_moves_done_assign_and_cancel(client):
    outcome_id, ids = _outcome(**{"Mine": "me", "Theirs": "agent", "Drop me": "me"})
    conn = db.get_connection()

    client.post(f"/items/{ids['Mine']}/done", data={"note": "checked", "next": f"/outcomes/{outcome_id}"})
    client.post(f"/items/{ids['Theirs']}/assign", data={"owner": "me"})
    client.post(f"/items/{ids['Drop me']}/cancel", data={"reason": "Not needed"})

    assert db.get_decision_request(conn, ids["Mine"])["status"] == "resolved"
    assert db.get_decision_request(conn, ids["Theirs"])["kind"] == "action"
    assert db.get_decision_request(conn, ids["Drop me"])["status"] == "cancelled"


def test_outcome_status_buttons_and_achieving_from_now(client):
    outcome_id, ids = _outcome(**{"Only step": "agent"})
    conn = db.get_connection()
    db.complete_step(conn, ids["Only step"], by="agent")

    now = client.get("/").text
    assert "Every step is done" in now and f'action="/outcomes/{outcome_id}/status"' in now

    client.post(f"/outcomes/{outcome_id}/status", data={"status": "achieved", "next": "/"})
    assert db.get_outcome(conn, outcome_id)["status"] == "achieved"
    assert client.post(f"/outcomes/{outcome_id}/status", data={"status": "bogus"}).status_code == 400


def _achieve_form(html, outcome_id):
    """The Achieved form on outcome_id's goal card: (hidden fields, its confirm question), or None."""
    form = re.search(rf'<form([^>]*action="/outcomes/{outcome_id}/status"[^>]*)>(.*?)</form>', html, re.S)
    if not form:
        return None
    confirm = re.search(r'data-confirm="([^"]*)"', form.group(1))
    return (dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(2))),
            confirm.group(1) if confirm else None)


def _ready_goal():
    outcome_id, ids = _outcome(**{"Only step": "agent"})
    db.complete_step(db.get_connection(), ids["Only step"], by="agent")
    return outcome_id


def test_a_goal_card_marks_the_goal_achieved_without_opening_it(client):
    outcome_id = _ready_goal()
    project = db.find_project(db.get_connection(), "acme")

    for page in (f"/projects/{project['id']}", "/projects"):
        assert _achieve_form(client.get(page).text, outcome_id) == ({"status": "achieved", "next": page}, None), page


def test_achieving_a_goal_with_open_steps_from_its_card_asks_first(client):
    two_open, _ = _outcome(**{"Configure the build": "agent", "Add DNS record": "me"})
    no_steps = db.create_outcome(db.get_connection(), db.find_project(db.get_connection(), "acme")["id"], "Pick a name")
    page = client.get("/projects").text

    assert "2 steps are still open" in _achieve_form(page, two_open)[1]
    assert _achieve_form(page, no_steps)[1] is None


def test_achieved_from_a_card_comes_back_to_the_page_without_that_card(client):
    outcome_id = _ready_goal()
    page = f"/projects/{db.find_project(db.get_connection(), 'acme')['id']}"
    fields, _ = _achieve_form(client.get(page).text, outcome_id)

    resp = client.post(f"/outcomes/{outcome_id}/status", data=fields, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == page
    assert db.get_outcome(db.get_connection(), outcome_id)["status"] == "achieved"
    assert _achieve_form(client.get(page).text, outcome_id) is None
