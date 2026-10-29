"""'While you were away': what changed since the human last looked, per project, from
the event log — free, no LLM. Shown on Now after an absence; 'Mark seen' resets it."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from apo import db


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _activity(conn, tmp_path):
    acme = db.get_or_create_project(conn, "acme")
    repo = db.ensure_repository_for_project(conn, acme["id"], str(tmp_path / "acme"))
    db.register_session(conn, "worker", str(tmp_path / "acme"))
    first = db.create_decision_request(conn, project_id=acme["id"], repository_id=repo, title="Pick a queue", question="Q?")
    db.create_decision_request(conn, project_id=acme["id"], repository_id=repo, title="Rotate key", question="Q?")
    db.answer_decision_request(conn, first, "Postgres", source="linear")
    for _ in range(3):
        db.session_heartbeat(conn, "worker")
        db.session_stop(conn, "worker")
    db.record_session_summary(conn, "worker", "Wired the webhook; tests pass.")
    db.register_session(conn, "crasher", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "crasher")
    db.session_end(conn, "crasher", "process_gone_mid_turn")


def test_the_digest_counts_what_happened_and_tells_each_projects_story(db_path, tmp_path):
    conn = db.get_connection()
    since = db.latest_event_seq(conn)
    _activity(conn, tmp_path)

    digest = db.away_digest(conn, since)

    assert digest["counts"]["filed"] == 2
    assert digest["counts"]["answered"] == 1
    assert digest["counts"]["turns"] == 3
    assert digest["counts"]["crashed"] == 1
    (acme,) = digest["projects"]
    assert acme["name"] == "acme"
    assert any("Rotate key" in e["summary"] for e in acme["events"])
    assert acme["recap"] == "Wired the webhook; tests pass."


def test_an_empty_stretch_has_an_empty_digest(db_path):
    conn = db.get_connection()
    digest = db.away_digest(conn, db.latest_event_seq(conn))
    assert digest["projects"] == [] and sum(digest["counts"].values()) == 0


def test_first_visit_sets_the_marker_without_dumping_history(db_path, tmp_path):
    conn = db.get_connection()
    _activity(conn, tmp_path)
    client = TestClient(__import__("apo.web.app", fromlist=["app"]).app)

    text = client.get("/").text

    assert "Since you were away" not in text
    assert db.get_ui_state(db.get_connection(), "last_visit_seq") == str(db.latest_event_seq(conn))


def test_returning_after_a_while_shows_the_digest_until_marked_seen(db_path, tmp_path):
    conn = db.get_connection()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", _ago(hours=3))
    _activity(conn, tmp_path)
    client = TestClient(__import__("apo.web.app", fromlist=["app"]).app)

    text = client.get("/").text
    assert "Since you were away" in text and "Rotate key" in text and "Wired the webhook" in text
    assert "Since you were away" in client.get("/").text  # a refresh doesn't lose it

    resp = client.post("/seen", follow_redirects=False)
    assert resp.status_code == 303
    assert "Since you were away" not in client.get("/").text


def test_a_visible_now_tab_polling_counts_as_watching(db_path, tmp_path):
    conn = db.get_connection()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", _ago(hours=3))
    client = TestClient(__import__("apo.web.app", fromlist=["app"]).app)
    _activity(conn, tmp_path)

    client.get("/api/now")

    assert "Since you were away" not in client.get("/").text


def test_a_quick_return_doesnt_show_the_digest(db_path, tmp_path):
    conn = db.get_connection()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", _ago(minutes=5))
    _activity(conn, tmp_path)

    assert "Since you were away" not in TestClient(__import__("apo.web.app", fromlist=["app"]).app).get("/").text


def _away_page(conn, tmp_path, then):
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", _ago(hours=3))
    then(conn, tmp_path)
    html = TestClient(__import__("apo.web.app", fromlist=["app"]).app).get("/").text
    import re
    return re.search(r'<section class="away surface".*?</section>', html, re.S).group(0)


def _noisy(conn, tmp_path):
    _activity(conn, tmp_path)
    for n in range(6):
        db.session_waiting(conn, "worker", "permission", f"Bash: aws cloudfront update-distribution --id E{n}")
    db.register_session(conn, "idle", str(tmp_path / "acme"))
    db.session_end(conn, "idle", "logout")
    beta = db.get_or_create_project(conn, "beta")
    db.ensure_repository_for_project(conn, beta["id"], str(tmp_path / "beta"))
    db.register_session(conn, "quiet", str(tmp_path / "beta"))
    db.session_heartbeat(conn, "quiet")
    db.session_stop(conn, "quiet")
    db.record_session_summary(conn, "quiet", "Both scans are done. The build passes; nothing committed yet.")
    db.register_session(conn, "homeless", "/")
    db.session_end(conn, "homeless", "logout")


def test_the_digest_leaves_out_what_no_longer_matters(db_path, tmp_path):
    conn = db.get_connection()
    since = db.latest_event_seq(conn)
    _noisy(conn, tmp_path)

    digest = db.away_digest(conn, since)

    listed = [e["verb"] for p in digest["projects"] for e in p["events"]]
    assert not [v for v in listed if v in ("session.waiting", "session.started", "session.ended")]
    assert [p["name"] for p in digest["projects"]] == ["acme", "beta"]


def test_a_project_where_agents_only_worked_shows_its_recap(db_path, tmp_path):
    conn = db.get_connection()
    since = db.latest_event_seq(conn)
    _noisy(conn, tmp_path)

    beta = next(p for p in db.away_digest(conn, since)["projects"] if p["name"] == "beta")

    assert beta["events"] == [] and beta["gist"] == "Both scans are done."


def test_the_headline_counts_work_not_prompts(db_path, tmp_path):
    section = _away_page(db.get_connection(), tmp_path, _noisy)

    head = section.split('class="away-counts"')[1].split("</p>")[0]
    assert "2 filed" in head and "1 answered" in head and "1 crashed" in head
    assert "stopped on you" not in head and "turn" not in head


def test_each_project_is_one_line_until_opened(db_path, tmp_path):
    section = _away_page(db.get_connection(), tmp_path, _noisy)

    import re
    projects = re.findall(r'<details class="away-project".*?</details>', section, re.S)
    assert len(projects) == 2 and all(" open" not in p.split(">")[0] for p in projects)
    summary = re.search(r"<summary>(.*?)</summary>", projects[0], re.S).group(1)
    assert "acme" in summary and "2 filed" in summary and "1 crashed" in summary
    assert "aws cloudfront" not in section


def test_event_times_from_another_day_say_which_day(db_path):
    from apo.web import app

    assert app._when(_ago()).count(":") == 1 and len(app._when(_ago())) == 5
    assert app._when(_ago(days=2))[:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert app._when(_ago(days=20))[:3] in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def test_a_gist_stops_before_a_list_starts():
    assert db._first_sentence("Breaking it down: - aws cloudfront update - python3 script") == "Breaking it down:"
    assert db._first_sentence("Both scans are done. The build passes.") == "Both scans are done."
