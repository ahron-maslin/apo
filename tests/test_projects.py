"""Projects as a place: one row each with its health (the worst state among its
items, never an average), counts that read like "2 you · 1 review · 1 working",
pinning, computed dormancy, and a page per project with everything in it."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _project(conn, name, path):
    path.mkdir(exist_ok=True)
    project = db.get_or_create_project(conn, name)
    db.ensure_repository_for_project(conn, project["id"], str(path))
    return project


def _decision(conn, project, title="Queue backend"):
    return db.create_decision_request(conn, project_id=project["id"], title=title, question="Postgres or SQLite?")


def _age_everything(conn, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE events SET occurred_at = ?", (old,))
    conn.execute("UPDATE sessions SET last_seen_at = ?, started_at = ?", (old, old))
    conn.execute("UPDATE projects SET created_at = ?", (old,))
    conn.commit()


def _row(overview, name):
    return next(p for p in overview if p["name"] == name)


def test_health_is_the_worst_state_among_its_items(db_path, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    _decision(conn, acme)
    db.register_session(conn, "s-acme", str(tmp_path / "acme"))
    db.session_prompt_submitted(conn, "s-acme", "go")
    quiet = _project(conn, "quiet", tmp_path / "quiet")
    db.register_session(conn, "s-quiet", str(tmp_path / "quiet"))
    db.session_prompt_submitted(conn, "s-quiet", "go")

    overview = db.project_overview(conn)

    assert _row(overview, "acme")["health"] == "your_move"
    assert _row(overview, "acme")["counts"]["you"] == 1
    assert _row(overview, "acme")["counts"]["working"] == 1
    assert _row(overview, "quiet")["health"] == "working"
    assert quiet["id"] == _row(overview, "quiet")["id"]


def test_counts_read_as_a_line(db_path, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    _decision(conn, acme, "One")
    _decision(conn, acme, "Two")

    assert _row(db.project_overview(conn), "acme")["counts_line"] == "2 you"


def test_a_project_with_nothing_open_and_no_activity_for_two_weeks_is_dormant(db_path, tmp_path):
    conn = db.get_connection()
    _project(conn, "old", tmp_path / "old")
    db.register_session(conn, "s-old", str(tmp_path / "old"))
    db.session_end(conn, "s-old", "prompt_input_exit")
    _age_everything(conn, 20)

    assert _row(db.project_overview(conn), "old")["health"] == "dormant"


def test_dormancy_wakes_on_anything_new(db_path, tmp_path):
    conn = db.get_connection()
    old = _project(conn, "old", tmp_path / "old")
    _age_everything(conn, 20)
    _decision(conn, old)

    assert _row(db.project_overview(conn), "old")["health"] == "your_move"


def test_pinned_projects_come_first(db_path, tmp_path):
    conn = db.get_connection()
    busy = _project(conn, "busy", tmp_path / "busy")
    _decision(conn, busy)
    calm = _project(conn, "calm", tmp_path / "calm")

    db.set_project_pinned(conn, calm["id"], True)

    names = [p["name"] for p in db.project_overview(conn)]
    assert names.index("calm") < names.index("busy")
    assert _row(db.project_overview(conn), "calm")["pinned"]


def test_archived_projects_are_left_out_unless_asked(db_path, tmp_path):
    conn = db.get_connection()
    gone = _project(conn, "gone", tmp_path / "gone")
    db.archive_project(conn, gone["id"])

    assert "gone" not in [p["name"] for p in db.project_overview(conn)]
    assert "gone" in [p["name"] for p in db.project_overview(conn, include_archived=True)]


def test_projects_page(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    _decision(conn, acme)

    text = client.get("/projects").text

    assert f'href="/projects/{acme["id"]}"' in text
    assert "1 you" in text
    assert f'action="/projects/{acme["id"]}/pin"' in text


def test_pin_and_unpin_from_the_page(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")

    client.post(f"/projects/{acme['id']}/pin", data={"pinned": "1"})
    assert _row(db.project_overview(conn), "acme")["pinned"]
    client.post(f"/projects/{acme['id']}/pin", data={"pinned": "0"})
    assert not _row(db.project_overview(conn), "acme")["pinned"]


def test_project_page_has_its_queue_sessions_outcomes_and_activity(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    other = _project(conn, "other", tmp_path / "other")
    _decision(conn, acme, "Pick the queue backend")
    _decision(conn, other, "Something elsewhere")
    db.register_session(conn, "s-acme", str(tmp_path / "acme"))
    db.create_outcome(conn, acme["id"], "Launch the site")

    overview = client.get(f"/projects/{acme['id']}").text
    agents = client.get(f"/projects/{acme['id']}/agents").text

    assert "Pick the queue backend" in overview
    assert "Something elsewhere" not in overview
    assert "Launch the site" in overview
    assert str(tmp_path / "acme") in overview
    assert 'id="session-card-' in agents
    assert "/start" not in agents


def test_archive_and_unarchive_from_the_project_page(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    decision_id = _decision(conn, acme)

    client.post(f"/projects/{acme['id']}/archive")
    assert db.get_decision_request(conn, decision_id)["status"] == "resolved"
    assert conn.execute("SELECT archived_at FROM projects WHERE id = ?", (acme["id"],)).fetchone()[0]

    client.post(f"/projects/{acme['id']}/unarchive")
    assert conn.execute("SELECT archived_at FROM projects WHERE id = ?", (acme["id"],)).fetchone()[0] is None


def test_merge_from_the_project_page(client, tmp_path):
    conn = db.get_connection()
    dup = _project(conn, "Widget Exporter", tmp_path / "wex")
    decision_id = _decision(conn, dup)
    outcome_id = db.create_outcome(conn, dup["id"], "Ship the exporter")
    # Duplicates like this predate name normalization; get_or_create would fold them.
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('widget-exporter', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    keep = conn.execute("SELECT * FROM projects WHERE name = 'widget-exporter'").fetchone()

    resp = client.post(f"/projects/{dup['id']}/merge", data={"into": "widget-exporter"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/projects/{keep['id']}"
    assert db.get_decision_request(conn, decision_id)["project_id"] == keep["id"]
    assert db.get_outcome(conn, outcome_id)["project_id"] == keep["id"]


def test_now_can_be_narrowed_to_one_project(client, tmp_path):
    conn = db.get_connection()
    _decision(conn, _project(conn, "acme", tmp_path / "acme"), "Acme thing")
    _decision(conn, _project(conn, "other", tmp_path / "other"), "Other thing")

    text = client.get("/?project=acme").text

    assert "Acme thing" in text
    assert "Other thing" not in text
    assert 'href="/"' in text


def test_project_page_shows_what_its_agents_are_working_through(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")
    db.register_session(conn, "s-acme", str(tmp_path / "acme"))
    db.record_checklist_item(conn, "s-acme", "t1", "Write the migration")
    db.record_checklist_item(conn, "s-acme", "t2", "Backfill old rows")

    text = client.get(f"/projects/{acme['id']}/agents").text

    assert "Write the migration" in text and "0/2" in text


def test_project_page_keeps_itself_current(client, tmp_path):
    conn = db.get_connection()
    acme = _project(conn, "acme", tmp_path / "acme")

    text = client.get(f"/projects/{acme['id']}").text

    assert "data-live-region" in text and "/static/live.js" in text


def test_renaming_a_project_keeps_the_old_name_as_an_alias(client, tmp_path):
    conn = db.get_connection()
    initech = _project(conn, "Initech", tmp_path / "sideProject2")

    client.post(f"/projects/{initech['id']}/rename", data={"name": "sideProject2"})

    assert db.get_project_name(conn, initech["id"]) == "sideProject2"
    assert db.find_project(conn, "Initech")["id"] == initech["id"]


def test_the_projects_list_shows_the_folder_when_it_differs_from_the_name(client, tmp_path):
    _project(db.get_connection(), "Initech", tmp_path / "sideProject2")

    assert "sideProject2/" in client.get("/projects").text
