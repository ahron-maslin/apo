"""An item's page puts answering first: the options right under the summary, the
question and handoff folded below, and everything for managing the item under one
Manage in the side column."""
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
    return project


def _decision(acme, **kw):
    return db.create_decision_request(db.get_connection(), project_id=acme["id"], title="Pick a queue",
                                      question="Which queue should the export use?", summary="Exports need a queue.",
                                      context="Exports time out under load.", **kw)


def test_the_options_come_right_after_the_summary(client, acme):
    item = _decision(acme, options=[{"label": "SQS"}, {"label": "Redis"}], recommended_option=1,
                     recommendation="Redis, it's already running", confidence=80)

    html = client.get(f"/decisions/{item}").text

    assert html.index("Exports need a queue.") < html.index('class="option-btn') < html.index("Which queue should the export use?")
    assert "reco-card" not in html
    recommended = re.search(r'<button[^>]*class="option-btn is-recommended".*?</button>', html, re.S).group(0)
    assert "Suggested" in recommended and "80%" in recommended


def test_a_suggestion_without_options_is_one_line_above_the_answer(client, acme):
    item = _decision(acme, recommendation="Use SQS", confidence=70)

    html = client.get(f"/decisions/{item}").text

    line = re.search(r'<p class="suggestion-line">(.*?)</p>', html, re.S).group(1)
    assert "Use SQS" in line and "70%" in line
    assert html.index('class="suggestion-line"') < html.index('name="answer_text"')


def test_the_question_and_handoff_fold_into_one_section(client, acme):
    item = _decision(acme, options=[{"label": "SQS"}])

    html = client.get(f"/decisions/{item}").text
    fold = re.search(r'<details class="[^"]*question-fold[^"]*".*?</details>', html, re.S).group(0)

    assert "Which queue should the export use?" in fold and "Exports time out under load." in fold


def test_notes_fold_with_a_count(client, acme):
    item = _decision(acme)
    db.add_note(db.get_connection(), item, "Tried SQS last year")

    html = client.get(f"/decisions/{item}").text

    notes = re.search(r'<details[^>]*id="notes".*?</details>', html, re.S).group(0)
    summary = notes[notes.index("<summary"):notes.index("</summary>")]
    assert "Notes" in summary and "1" in summary and "Tried SQS last year" in notes


def test_managing_the_item_sits_under_one_manage(client, acme):
    item = _decision(acme)

    html = client.get(f"/decisions/{item}").text
    aside = html[html.index('class="detail-aside'):]
    manage = re.search(r'<details class="[^"]*manage[^"]*".*</details>', aside, re.S).group(0)

    for action in (f"/items/{item}/cancel", f"/decisions/{item}/edit", f"/decisions/{item}/due", f"/decisions/{item}/labels"):
        assert f'action="{action}"' in manage and aside.count(f'action="{action}"') == 1, action
    assert "Reference" in aside[:aside.index('class="disclosure manage')]


def test_an_open_action_has_one_done_and_its_box_sends_a_note(client, acme):
    item = db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain")

    html = client.get(f"/decisions/{item}").text

    assert len(re.findall(r'name="choice" value="done"', html)) == 1
    send = re.search(r'<button[^>]*formaction="/decisions/%d/notes"[^>]*>([^<]*)</button>' % item, html)
    assert send and send.group(1).strip() == "Send note"
    assert 'id="note-body"' not in html

    resp = client.post(f"/decisions/{item}/notes", data={"answer_text": "Registrar emailed"}, follow_redirects=False)
    assert resp.status_code == 303
    assert [n["body"] for n in db.list_notes(db.get_connection(), item)] == ["Registrar emailed"]
    assert db.get_decision_request(db.get_connection(), item)["status"] == "open"
