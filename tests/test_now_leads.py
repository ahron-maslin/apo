"""Now leads with the work: one summary line, then the rows. What you rarely use
folds away but keeps its key."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


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


def _decision(acme, title, **kw):
    return db.create_decision_request(db.get_connection(), project_id=acme["id"], repository_id=acme["repo_id"],
                                      title=title, question="Which?", **kw)


def _row(html, title):
    start = html.index(title)
    return html[html.rindex("<article", 0, start):html.index("</article>", start)]


def test_the_top_of_now_is_one_line(client, acme):
    _decision(acme, "Pick a queue", recommendation="SQS", confidence=92, options=[{"label": "SQS", "reversible": True}],
              recommended_option=0)

    html = client.get("/").text

    summary = re.search(r'<p class="page-desc now-summary"[^>]*>(.*?)</p>', html, re.S).group(1)
    assert re.sub(r"\s+", " ", summary).strip().startswith("1 waiting on you")
    assert "working" not in summary and "plan-bar" not in html


def test_the_round_and_bulk_accept_keep_their_keys_in_a_menu(client, acme):
    _decision(acme, "Pick a queue", recommendation="SQS", confidence=92, options=[{"label": "SQS", "reversible": True}],
              recommended_option=0)

    html = client.get("/").text
    menu = html[html.index('class="now-more-menu'):]
    menu = menu[:menu.index("</details>")]

    assert 'href="/round?minutes=5"' in menu and 'data-hotkey="t"' in menu
    assert 'href="/accept"' in menu and 'data-hotkey="b"' in menu


def test_since_you_were_away_is_one_line_that_opens(client, acme):
    conn = db.get_connection()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", _ago(hours=3))
    _decision(acme, "Pick a queue")

    html = client.get("/").text
    away = html[html.index('class="away'):]
    away = away[:away.index("</section>")]

    summary = away[away.index("<summary"):away.index("</summary>")]
    assert "Since you were away" in summary and "1 filed" in summary
    assert "<details" in away and 'action="/seen"' in away


def test_a_move_carries_its_suggestion_on_the_accept_button(client, acme):
    item = _decision(acme, "Pick a queue", recommendation="SQS, with a dead-letter queue", confidence=92,
                     options=[{"label": "SQS", "reversible": True}], recommended_option=0)

    row = _row(client.get("/").text, "Pick a queue")

    accept = re.search(r'<button[^>]*data-now-primary[^>]*>(.*?)</button>', row, re.S).group(1)
    assert "SQS, with a dead-letter queue" in accept and "92%" in accept
    assert "now-suggestion" not in row
    more = row[row.index('class="now-more'):]
    assert f'action="/items/{item}/cancel"' in more[:more.index("</details>")]


def test_due_stuck_and_notes_sit_on_the_meta_line(client, acme):
    conn = db.get_connection()
    item = db.add_work(conn, acme["id"], "me", "Renew the domain",
                       due_on=(datetime.now().date() - timedelta(days=1)).isoformat())
    db.add_note(conn, item, "Registrar emailed")

    row = _row(client.get("/").text, "Renew the domain")

    meta = row[row.index('class="now-meta"'):]
    meta = meta[:meta.index("</span>\n")]
    assert "Overdue" in meta and "1 note" in meta


def test_a_resume_command_is_a_copy_button(client, acme):
    conn = db.get_connection()
    item = _decision(acme, "Pick a queue")
    db.answer_decision_request(conn, item, "SQS")
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(hours=5), item))
    conn.commit()

    row = _row(client.get("/").text, "Pick a queue")

    assert re.search(r'<button[^>]*data-copy="cd [^"]+"[^>]*>((?!</button>).)*Copy resume command</button>', row, re.S)
    assert "<code>cd " not in row


def test_the_lightning_round_still_shows_the_suggestion(client, acme):
    _decision(acme, "Pick a queue", recommendation="SQS", confidence=92, options=[{"label": "SQS", "reversible": True}],
              recommended_option=0)

    assert "now-suggestion" in client.get("/round").text
