"""Any open row can be closed where it's listed, without opening it: Cancel under the
row's …, back on the same page. Goals close with Achieved or Drop."""
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
    item = db.create_decision_request(conn, project_id=project["id"], title="Pick a webhook queue", question="Which?")
    return {**project, "item": item}


def _cancel_next(html, item_id):
    """The next= of the Cancel form for item_id inside a row's … menu, or None."""
    for menu in re.findall(r'<details class="[^"]*row-more[^"]*".*?</details>', html, re.S):
        form = re.search(rf'action="/items/{item_id}/cancel".*?name="next" value="([^"]*)"', menu, re.S)
        if form:
            return form.group(1).replace("&amp;", "&")
    return None


@pytest.mark.parametrize("page", ["/", "/projects/{pid}", "/projects/{pid}/decisions", "/projects/decisions",
                                  "/search?q=webhook"])
def test_every_list_can_close_an_open_row_and_come_back(client, acme, page):
    page = page.format(pid=acme["id"])
    back = _cancel_next(client.get(page).text, acme["item"])
    assert back == page

    resp = client.post(f"/items/{acme['item']}/cancel", data={"next": back}, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == page
    assert db.get_decision_request(db.get_connection(), acme["item"])["status"] == "cancelled"


def test_a_claimed_task_can_be_cancelled_from_work(client, acme):
    conn = db.get_connection()
    task = db.create_task(conn, project_id=acme["id"], title="Write the migration")
    db.claim_task(conn, task, "s-1")

    assert _cancel_next(client.get(f"/projects/{acme['id']}/work").text, task) == f"/projects/{acme['id']}/work"


def test_a_goal_card_drops_its_goal_after_asking(client, acme):
    outcome = db.create_outcome(db.get_connection(), acme["id"], "Launch the site")

    for page in (f"/projects/{acme['id']}", "/projects"):
        html = client.get(page).text
        card = html[html.index('class="outcome-card'):]
        menu = re.search(r'<details class="[^"]*row-more[^"]*".*?</details>', card, re.S).group(0)
        form = re.search(rf'<form[^>]*action="/outcomes/{outcome}/status"([^>]*)>(.*?)</form>', menu, re.S)
        assert "data-confirm" in form.group(1) and 'value="dropped"' in form.group(2), page
