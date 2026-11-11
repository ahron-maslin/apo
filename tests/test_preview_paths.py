"""A preview runs on stand-in folders inside its scratch directory, but shows each
project's real folder, so it reads like the dashboard it previews."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from apo import db


def test_a_preview_shows_the_real_folders_not_its_stand_ins(db_path, tmp_path, monkeypatch):
    real = Path.home() / "projects" / "acme"
    stand_in = Path(db_path).parent / "projects" / "acme"
    stand_in.mkdir(parents=True)
    (Path(db_path).parent / "folders.json").write_text(json.dumps([[str(real), str(stand_in)]]))
    monkeypatch.setenv("APO_PREVIEW", "1")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(stand_in))
    from apo.web import app

    html = TestClient(app.app).get(f"/projects/{project['id']}").text
    header = html[html.index('class="page-desc"'):]
    header = header[:header.index("</p>")]

    assert "~/projects/acme" in header and str(stand_in) not in header


def test_outside_a_preview_paths_are_shown_as_they_are(db_path, tmp_path, monkeypatch):
    stand_in = Path(db_path).parent / "projects" / "acme"
    stand_in.mkdir(parents=True)
    (Path(db_path).parent / "folders.json").write_text(json.dumps([["/somewhere/else", str(stand_in)]]))
    monkeypatch.delenv("APO_PREVIEW", raising=False)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(stand_in))
    from apo.web import app

    assert "/somewhere/else" not in TestClient(app.app).get(f"/projects/{project['id']}").text


def test_a_preview_copies_resume_commands_for_the_real_folders(db_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    real = Path.home() / "projects" / "acme"
    stand_in = Path(db_path).parent / "projects" / "acme"
    stand_in.mkdir(parents=True)
    (Path(db_path).parent / "folders.json").write_text(json.dumps([[str(real), str(stand_in)]]))
    monkeypatch.setenv("APO_PREVIEW", "1")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.ensure_repository_for_project(conn, project["id"], str(stand_in))
    item = db.create_decision_request(conn, project_id=project["id"], repository_id=repo, title="Pick a queue", question="?")
    db.answer_decision_request(conn, item, "SQS")
    five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (five_hours_ago, item))
    conn.commit()
    from apo.web import app

    html = TestClient(app.app).get("/").text

    assert str(stand_in) not in html and f'data-copy="cd {real} &amp;&amp; ' in html
