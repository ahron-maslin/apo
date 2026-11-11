"""Pages show what's there to act on: controls you use rarely fold away, and sections
with nothing in them don't render at all."""
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
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return {**project, "repo_id": repo_id}


def test_the_bulk_bar_waits_folded_below_the_rows(client, acme):
    item = db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain")

    for page in (f"/projects/{acme['id']}/work", "/projects/work"):
        html = client.get(page).text
        fold = html.index('<details class="bulk-wrap"')
        assert "Select several" in html[fold:html.index('id="bulk-form"')], page
        assert html.index(f'name="ids" value="{item}"') < fold, page


def test_saved_views_are_not_on_the_pages(client, acme):
    db.save_view(db.get_connection(), "Mine", "/projects/work", "owner=me")

    for page in (f"/projects/{acme['id']}/work", "/projects/work", f"/projects/{acme['id']}/decisions",
                 "/projects/decisions", "/search"):
        assert 'class="saved-views"' not in client.get(page).text, page
    assert client.get("/api/palette").json()["views"][0]["name"] == "Mine"


def test_the_overview_leaves_out_linear_goals_and_merging_when_there_is_nothing_to_show(client, acme):
    html = client.get(f"/projects/{acme['id']}").text

    assert 'id="linear-goals"' not in html
    assert 'id="ready-to-merge"' not in html and "Nothing waiting to be merged" not in html


def test_the_goals_hint_shows_only_until_there_is_a_goal(client, acme):
    pages = (f"/projects/{acme['id']}", "/projects")
    assert all("goals-hint" in client.get(p).text for p in pages)

    db.create_outcome(db.get_connection(), acme["id"], "Launch the site")

    assert not any("goals-hint" in client.get(p).text for p in pages)


def test_status_export_lives_on_the_weekly_report_not_the_overview(client, acme):
    assert "Copy status" not in client.get(f"/projects/{acme['id']}").text
    assert "Copy status" in client.get(f"/projects/{acme['id']}/report").text


def test_answers_on_their_way_fold_into_one_line_on_now(client, acme):
    conn = db.get_connection()
    first = db.create_decision_request(conn, project_id=acme["id"], repository_id=acme["repo_id"], title="Pick a queue", question="Q?")
    second = db.create_decision_request(conn, project_id=acme["id"], repository_id=acme["repo_id"], title="Pick a host", question="Q?")
    db.answer_decision_request(conn, first, "SQS")
    db.answer_decision_request(conn, second, "Fly")

    html = client.get("/").text

    fold = html[html.index('now-transit'):]
    assert "2 answers on their way to agents" in fold[:fold.index("</summary>")]
    assert "Pick a queue" in fold and "now-quiet" not in html
