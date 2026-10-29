"""One search over everything ADO knows: items (question, context, answer…),
session recaps and outcomes — substring matching (FTS5 trigram), kept current by
triggers. Transcripts never enter the DB; "search transcripts too" greps them."""
import json
import shutil

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


def _hits(query, **kwargs):
    return [(h["kind"], h["title"]) for h in db.search(db.get_connection(), query, **kwargs)]


def _decision(conn, project, title, question, **fields):
    return db.create_decision_request(conn, project_id=project["id"], title=title, question=question, **fields)


def test_finds_an_item_by_part_of_a_word_in_its_question(acme):
    conn = db.get_connection()
    _decision(conn, acme, "Payments", "Should the Stripe webhook verify signatures?")
    _decision(conn, acme, "Queue", "Postgres or SQLite?")

    assert _hits("webho") == [("item", "Payments")]


def test_an_answer_becomes_searchable_once_given(acme):
    conn = db.get_connection()
    decision_id = _decision(conn, acme, "Queue", "Which backend?")
    assert _hits("rabbitmq") == []

    db.answer_decision_request(conn, decision_id, "Use RabbitMQ for now")

    assert _hits("rabbitmq") == [("item", "Queue")]


def test_session_recaps_and_outcomes_are_searchable(acme, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(tmp_path))
    db.record_session_summary(conn, "s-1", "Refactored the invoice exporter to stream rows")
    db.create_outcome(conn, acme["id"], "Launch the invoice portal")

    kinds = {kind for kind, _ in _hits("invoice")}

    assert kinds == {"session", "outcome"}


def test_every_word_must_match(acme):
    conn = db.get_connection()
    _decision(conn, acme, "One", "stripe webhook retries")
    _decision(conn, acme, "Two", "stripe payouts")

    assert _hits("stripe webhook") == [("item", "One")]


def test_short_words_still_match(acme):
    conn = db.get_connection()
    _decision(conn, acme, "Region", "Deploy to eu or us?")

    assert _hits("eu") == [("item", "Region")]


def test_quotes_and_operators_in_a_query_are_just_text(acme):
    conn = db.get_connection()
    _decision(conn, acme, "Quote", 'He said "ship it" OR NOT')

    assert _hits('"ship it" OR') == [("item", "Quote")]
    assert _hits('he said "') == [("item", "Quote")]


def test_rows_from_before_the_index_are_backfilled(acme):
    conn = db.get_connection()
    _decision(conn, acme, "Old", "an ancient question")
    conn.execute("DELETE FROM search_items")
    conn.commit()
    assert _hits("ancient") == []

    db.rebuild_search_index(conn)

    assert _hits("ancient") == [("item", "Old")]


def test_a_hit_shows_where_the_words_matched(acme):
    conn = db.get_connection()
    _decision(conn, acme, "Payments", "x " * 200 + "the Stripe webhook secret must rotate " + "y " * 200)

    hit = db.search(conn, "webhook")[0]

    assert "webhook" in hit["snippet"]
    assert len(hit["snippet"]) < 200
    assert hit["href"].startswith("/decisions/")
    assert hit["project_name"] == "acme"


def test_search_page(client, acme):
    conn = db.get_connection()
    decision_id = _decision(conn, acme, "Payments", "Should the Stripe webhook verify signatures?")

    text = client.get("/search?q=webhook").text

    assert f'href="/decisions/{decision_id}"' in text
    assert 'value="webhook"' in text
    assert "Search transcripts too" in text


def test_search_page_without_a_query(client):
    assert client.get("/search").status_code == 200


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_deep_search_greps_transcripts_of_known_sessions(client, acme, tmp_path, monkeypatch):
    conn = db.get_connection()
    db.register_session(conn, "11111111-2222-3333-4444-555555555555", str(tmp_path))
    folder = tmp_path / "claude-config" / "projects" / "-tmp-acme"
    folder.mkdir(parents=True)
    (folder / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "why does the flux capacitor overheat"}}) + "\n"
    )
    (folder / "99999999-0000-0000-0000-000000000000.jsonl").write_text("flux capacitor in an unknown session\n")

    hits = db.search_transcripts(conn, "flux capacitor")

    assert [h["ref"] for h in hits] == ["11111111-2222-3333-4444-555555555555"]
    assert "flux capacitor" in client.get("/search?q=flux+capacitor&deep=1").text.lower()


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_deep_search_covers_sessions_kept_by_other_claude_profiles(client, acme, tmp_path, monkeypatch):
    conn = db.get_connection()
    db.register_session(conn, "aaaaaaaa-2222-3333-4444-555555555555", str(tmp_path))
    monkeypatch.setenv("ADO_CLAUDE_CONFIG_DIRS", str(tmp_path / "work-profile"))
    folder = tmp_path / "work-profile" / "projects" / "-tmp-acme"
    folder.mkdir(parents=True)
    (folder / "aaaaaaaa-2222-3333-4444-555555555555.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "rotate the flux capacitor key"}}) + "\n"
    )

    assert [h["ref"] for h in db.search_transcripts(conn, "flux capacitor")] == ["aaaaaaaa-2222-3333-4444-555555555555"]
