"""Adding or ticking a step on an agent's checklist stays on the page you're on. The
form swaps the session card in from the page it redirects to, so redirecting to Now,
which has no session cards, navigated you away."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def session(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    db.register_session(conn, "s-1", str(tmp_path))
    row = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-1'").fetchone()
    db.add_checklist_item(conn, row["id"], "Write the tests")
    return project, row["id"]


def _card_forms(html, sid):
    card = re.search(rf'<article[^>]*id="session-card-{sid}".*?</article>', html, re.S).group(0)
    return re.findall(r'<form[^>]*action="/sessions/[^"]*".*?</form>', card, re.S)


@pytest.mark.parametrize("page", ["/projects/agents", "/projects/{pid}/agents"])
def test_the_card_forms_say_which_page_to_come_back_to(client, session, page):
    project, sid = session
    path = page.format(pid=project["id"])

    forms = _card_forms(client.get(path).text, sid)

    assert len(forms) >= 2
    for form in forms:
        assert f'name="next" value="{path}"' in form


def test_adding_and_ticking_come_back_to_that_page(client, session):
    project, sid = session
    back = f"/projects/{project['id']}/agents"
    item = db.get_connection().execute("SELECT id FROM checklist_items WHERE session_id = ?", (sid,)).fetchone()["id"]

    added = client.post(f"/sessions/{sid}/checklist", data={"title": "Ship it", "next": back}, follow_redirects=False)
    ticked = client.post(f"/sessions/{sid}/checklist/{item}/complete", data={"next": back}, follow_redirects=False)

    assert added.headers["location"] == back and ticked.headers["location"] == back
    assert 'id="session-card-' in client.get(back).text


def test_without_a_page_it_goes_to_the_sessions_project_not_now(client, session):
    project, sid = session

    resp = client.post(f"/sessions/{sid}/checklist", data={"title": "Ship it"}, follow_redirects=False)

    assert resp.headers["location"] == f"/projects/{project['id']}/agents"
