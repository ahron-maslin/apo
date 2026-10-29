"""Cancel from an item's own page and from its card on Now, not only from bulk lists."""
import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _file(conn, tmp_path, **kw):
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    kw.setdefault("title", "Pick a queue")
    kw.setdefault("question", "Which queue?")
    return db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, **kw)


def test_an_open_items_page_offers_cancel_with_an_optional_reason(client, tmp_path):
    item = _file(db.get_connection(), tmp_path)

    html = client.get(f"/decisions/{item}").text

    assert f'action="/items/{item}/cancel"' in html
    assert 'name="reason"' in html


def test_a_closed_items_page_offers_no_cancel(client, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    db.cancel_request(conn, item, "Not needed")

    assert f'action="/items/{item}/cancel"' not in client.get(f"/decisions/{item}").text


def test_cancelling_with_a_reason_closes_it_writes_it_to_the_repo_and_offers_undo(client, tmp_path):
    item = _file(db.get_connection(), tmp_path)

    response = client.post(f"/items/{item}/cancel", data={"reason": "Filed twice", "next": f"/decisions/{item}"},
                           follow_redirects=False)

    assert response.status_code == 303 and "ado_undo" in response.headers.get("set-cookie", "")
    d = db.get_decision_request(db.get_connection(), item)
    assert d["status"] == "cancelled" and d["cancel_reason"] == "Filed twice"
    assert "| CANCELLED |" in (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def test_now_cards_offer_cancel_and_come_back_to_now(client, tmp_path):
    conn = db.get_connection()
    decision = _file(conn, tmp_path)
    action = _file(conn, tmp_path, kind="action", title="Add the DNS record", question="Add it")

    html = client.get("/").text

    for item in (decision, action):
        form = html.split(f'action="/items/{item}/cancel"', 1)
        assert len(form) == 2, f"no cancel form for #{item}"
        assert 'name="next" value="/"' in form[1].split("</form>", 1)[0]
