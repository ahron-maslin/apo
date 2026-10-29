"""Bulk actions over several rows, saved views, and the Search filter bar."""
import pytest
from fastapi.testclient import TestClient

from apo import db


def _file(conn, tmp_path, project="acme", **kw):
    p = db.get_or_create_project(conn, project)
    repo_id = db.ensure_repository_for_project(conn, p["id"], str(tmp_path))
    kw.setdefault("title", "Pick a queue")
    kw.setdefault("question", "Which queue?")
    return db.create_decision_request(conn, project_id=p["id"], repository_id=repo_id, **kw)


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _steps(conn, tmp_path, n, kind="action"):
    return [_file(conn, tmp_path, title=f"Step {i}", kind=kind, requested_by="human") for i in range(n)]


# --- bulk ---

def test_bulk_done_counts_and_skips_invalid(db_path, tmp_path):
    conn = db.get_connection()
    a, b = _steps(conn, tmp_path, 2)
    decision = _file(conn, tmp_path, title="A real decision")
    db.cancel_request(conn, b, "no")
    result = db.bulk_apply(conn, "done", [a, b, decision, 9999])
    assert (result["applied"], result["skipped"]) == (1, 3)
    assert result["summary"] == "1 done, 3 skipped"
    assert db.get_decision_request(conn, a)["status"] in ("answered", "resolved")
    assert db.get_decision_request(conn, decision)["status"] == "open"


def test_bulk_done_records_undo_snapshot(db_path, tmp_path):
    conn = db.get_connection()
    a, = _steps(conn, tmp_path, 1)
    db.bulk_apply(conn, "done", [a])
    assert db.can_undo(db.get_decision_request(conn, a))
    db.undo_last(conn, a)
    assert db.get_decision_request(conn, a)["status"] == "open"


def test_bulk_cancel_and_snooze_record_undo(db_path, tmp_path):
    conn = db.get_connection()
    a, b = _steps(conn, tmp_path, 2)
    db.bulk_apply(conn, "cancel", [a])
    assert db.get_decision_request(conn, a)["status"] == "cancelled"
    assert db.can_undo(db.get_decision_request(conn, a))
    result = db.bulk_apply(conn, "snooze", [b], preset="tomorrow")
    assert result["applied"] == 1
    assert db.get_decision_request(conn, b)["snoozed_until"]
    assert db.get_decision_request(conn, b)["undo_snapshot"]


def test_bulk_reassign_and_due(db_path, tmp_path):
    conn = db.get_connection()
    a, = _steps(conn, tmp_path, 1)
    decision = _file(conn, tmp_path, title="Decide")
    result = db.bulk_apply(conn, "reassign", [a, decision], owner="agent")
    assert (result["applied"], result["skipped"]) == (1, 1)
    assert db.get_decision_request(conn, a)["kind"] == "task"
    result = db.bulk_apply(conn, "due", [a, decision], due_on="2030-01-02")
    assert result["applied"] == 2
    assert db.get_decision_request(conn, decision)["due_on"] == "2030-01-02"


def test_bulk_rejects_bad_arguments(db_path, tmp_path):
    conn = db.get_connection()
    a, = _steps(conn, tmp_path, 1)
    for action, kw in (("explode", {}), ("snooze", {"preset": "never"}), ("reassign", {"owner": "bob"}),
                       ("due", {"due_on": "soon"})):
        with pytest.raises(ValueError):
            db.bulk_apply(conn, action, [a], **kw)


def test_bulk_route_redirects_with_counts(client, db_path, tmp_path):
    conn = db.get_connection()
    a, b = _steps(conn, tmp_path, 2)
    db.cancel_request(conn, b, "no")
    r = client.post("/bulk", data={"action": "done", "ids": [a, b], "next": "/projects/work"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/projects/work?bulk=")
    assert "1%20done%2C%201%20skipped" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "1 done, 1 skipped" in page.text


def test_bulk_route_bad_action_is_400_and_next_is_safe(client, db_path, tmp_path):
    conn = db.get_connection()
    a, = _steps(conn, tmp_path, 1)
    assert client.post("/bulk", data={"action": "nope", "ids": [a]}).status_code == 400
    r = client.post("/bulk", data={"action": "cancel", "ids": [a], "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"].startswith("/projects/work")


def test_work_page_has_checkboxes_and_confirm_cancel(client, db_path, tmp_path):
    conn = db.get_connection()
    a, = _steps(conn, tmp_path, 1)
    html = client.get("/projects/work").text
    assert f'name="ids" value="{a}"' in html
    assert 'id="bulk-form"' in html
    assert 'value="cancel"' in html and "data-confirm" in html


# --- saved views ---

def test_saved_views_crud(db_path):
    conn = db.get_connection()
    vid = db.save_view(conn, "Overdue first", "/projects/work", "sort=due")
    assert [v["name"] for v in db.list_saved_views(conn)] == ["Overdue first"]
    assert db.save_view(conn, "Overdue first", "/projects/work", "") == vid
    assert db.list_saved_views(conn)[0]["query"] == ""
    assert db.delete_saved_view(conn, vid)
    assert db.list_saved_views(conn) == []


def test_saved_view_rejects_foreign_paths(db_path):
    conn = db.get_connection()
    for bad in ("https://evil.com", "//evil.com", "/settings", "/projects/x/work"):
        with pytest.raises(ValueError):
            db.save_view(conn, "x", bad, "")
    with pytest.raises(ValueError):
        db.save_view(conn, "  ", "/search", "")


def test_view_routes_and_palette(client, db_path):
    r = client.post("/views", data={"name": "Open bugs", "path": "/search", "query": "q=bug&status=open"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/search?q=bug&status=open"
    html = client.get("/search").text
    assert "Open bugs" in html and "/search?q=bug&amp;status=open" in html
    views = client.get("/api/palette").json()["views"]
    assert views == [{"id": 1, "name": "Open bugs", "href": "/search?q=bug&status=open"}]
    assert client.post("/views/1/delete", follow_redirects=False).status_code == 303
    assert client.get("/api/palette").json()["views"] == []


def test_view_route_rejects_bad_path(client, db_path):
    assert client.post("/views", data={"name": "x", "path": "//evil.com", "query": ""}).status_code == 400


# --- search filters ---

def test_search_without_filters_is_unchanged(db_path, tmp_path):
    conn = db.get_connection()
    _file(conn, tmp_path, title="Stripe webhook retries")
    assert db.search_filtered(conn, "stripe") == db.search(conn, "stripe")


def test_search_filters(db_path, tmp_path):
    conn = db.get_connection()
    _file(conn, tmp_path, title="Stripe webhook alpha", project="acme")
    b = _file(conn, tmp_path, title="Stripe webhook beta", project="beta", kind="task", requested_by="human")
    db.cancel_request(conn, b, "x")

    def titles(**kw):
        return sorted(h["title"] for h in db.search_filtered(conn, "stripe", **kw) if h["kind"] == "item")

    assert titles(status="open") == ["Stripe webhook alpha"]
    assert titles(project="BETA") == ["Stripe webhook beta"]
    assert titles(owner="agent") == ["Stripe webhook beta"]
    assert titles(owner="me") == ["Stripe webhook alpha"]
    assert titles(since="2999-01-01") == []
    assert titles(until="2999-01-01") == ["Stripe webhook alpha", "Stripe webhook beta"]
    with pytest.raises(ValueError):
        titles(since="yesterday")


def test_search_filters_without_query_list_recent_items(db_path, tmp_path):
    conn = db.get_connection()
    _file(conn, tmp_path, title="One")
    hits = db.search_filtered(conn, "", status="open")
    assert [h["title"] for h in hits] == ["One"]


def test_search_page_filter_bar(client, db_path, tmp_path):
    conn = db.get_connection()
    _file(conn, tmp_path, title="Stripe webhook alpha")
    html = client.get("/search?q=stripe").text
    assert 'name="status"' in html and 'name="project"' in html and 'name="owner"' in html
    assert 'name="since"' in html and 'name="until"' in html
    assert "webhook alpha" in html
    assert "Nothing matches" in client.get("/search?q=stripe&status=resolved").text
    assert "YYYY-MM-DD" in client.get("/search?q=stripe&since=bad").text
