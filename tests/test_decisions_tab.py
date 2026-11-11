"""The Decisions tab: what's waiting on you as compact rows you can answer from, then
the log of what was decided."""
import re

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
    decision = db.create_decision_request(conn, project_id=project["id"], title="Pick a queue", question="Which?",
                                          recommendation="SQS", confidence=90,
                                          options=[{"label": "SQS", "reversible": True}], recommended_option=0)
    action = db.add_work(conn, project["id"], "me", "Renew the domain")
    return {**project, "decision": decision, "action": action}


def _form(html, item_id):
    form = re.search(rf'<form[^>]*action="/decisions/{item_id}/answer"[^>]*>(.*?)</form>', html, re.S)
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(1))) if form else None


def test_waiting_rows_answer_in_place_without_bulk_controls(client, acme):
    for page in (f"/projects/{acme['id']}/decisions", "/projects/decisions"):
        html = client.get(page).text
        assert 'id="bulk-form"' not in html and 'name="ids"' not in html, page
        assert _form(html, acme["decision"]) == {"choice": "recommendation", "next": page}, page
        assert _form(html, acme["action"]) == {"choice": "done", "next": page}, page
        assert "Accept: SQS" in html, page


def test_accepting_from_the_tab_comes_back_to_it(client, acme):
    page = f"/projects/{acme['id']}/decisions"

    resp = client.post(f"/decisions/{acme['decision']}/answer", data=_form(client.get(page).text, acme["decision"]),
                       follow_redirects=False)

    assert resp.headers["location"] == page
    assert db.get_decision_request(db.get_connection(), acme["decision"])["status"] == "answered"


def test_the_tab_counts_what_its_heading_counts(client, acme):
    for page in (f"/projects/{acme['id']}/decisions", "/projects/decisions"):
        html = client.get(page).text
        tab = re.search(r'href="[^"]*/decisions"[^>]*>\s*Decisions\s*<span class="count-chip">(\d+)</span>', html)
        heading = re.search(r'id="open-decisions">Waiting on you <span class="count-chip">(\d+)</span>', html)
        assert tab and heading and tab.group(1) == heading.group(1) == "2", page
