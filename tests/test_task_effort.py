"""Time and tokens per finished item: elapsed from the attempt history, tokens from the
claiming session's transcript between claim and completion. Absent data shows nothing."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db
from apo import transcripts


def _ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _assistant(minutes_ago, msg_id, inp, out, cache=0):
    return json.dumps({"type": "assistant", "timestamp": _ts(minutes_ago).replace("+00:00", "Z"),
                       "message": {"id": msg_id, "usage": {"input_tokens": inp, "output_tokens": out,
                                                           "cache_read_input_tokens": cache}}})


def test_usage_between_sums_a_window_and_counts_a_message_once(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        _assistant(100, "m0", 1000, 1000),
        _assistant(50, "m1", 10, 20, cache=100),
        _assistant(50, "m1", 10, 20, cache=100),
        _assistant(40, "m2", 5, 7),
        _assistant(1, "m3", 9999, 9999),
    ]) + "\n")

    usage = transcripts.usage_between(str(p), _ts(60), _ts(30))

    assert usage == {"input": 115, "output": 27, "total": 142}


def test_usage_between_is_none_without_a_transcript_or_matching_entries(tmp_path):
    assert transcripts.usage_between(None, _ts(60), _ts(30)) is None
    assert transcripts.usage_between(str(tmp_path / "gone.jsonl"), _ts(60), _ts(30)) is None
    p = tmp_path / "t.jsonl"
    p.write_text(_assistant(500, "m", 1, 1) + "\n")
    assert transcripts.usage_between(str(p), _ts(60), _ts(30)) is None


@pytest.fixture
def finished(db_path, tmp_path, monkeypatch):
    config_dir = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    task = db.create_task(conn, project_id=project["id"], title="Add rate limiting")
    assert db.claim_task(conn, task, "sess-1")
    assert db.complete_task(conn, task, note="shipped")
    conn.execute("UPDATE task_attempts SET at = ? WHERE task_id = ? AND event = 'claimed'", (_ts(90), task))
    conn.execute("UPDATE task_attempts SET at = ? WHERE task_id = ? AND event = 'completed'", (_ts(30), task))
    conn.commit()
    return conn, project, task, config_dir


def test_effort_has_elapsed_and_no_tokens_without_a_transcript(finished):
    conn, _, task, _ = finished
    effort = db.task_effort(conn, task)
    assert effort["elapsed_seconds"] == 3600
    assert effort["tokens"] is None


def test_effort_reads_tokens_from_the_claiming_sessions_transcript(finished):
    conn, _, task, config_dir = finished
    folder = config_dir / "projects" / "-tmp-acme"
    folder.mkdir(parents=True)
    (folder / "sess-1.jsonl").write_text("\n".join([_assistant(120, "a", 500, 500), _assistant(60, "b", 100, 50)]) + "\n")

    effort = db.task_effort(conn, task)

    assert effort["tokens"] == {"input": 100, "output": 50, "total": 150}


def test_effort_is_none_for_an_unclaimed_or_unfinished_item(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    task = db.create_task(conn, project_id=project["id"], title="x")
    assert db.task_effort(conn, task) is None
    db.claim_task(conn, task, "s")
    assert db.task_effort(conn, task) is None


def test_effort_uses_the_last_claim_before_the_final_completion(finished):
    conn, _, task, _ = finished
    assert db.retry_task(conn, task)
    assert db.claim_task(conn, task, "sess-2")
    assert db.complete_task(conn, task)
    rows = conn.execute("SELECT id FROM task_attempts WHERE task_id = ? ORDER BY id", (task,)).fetchall()
    conn.execute("UPDATE task_attempts SET at = ? WHERE id = ?", (_ts(20), rows[-2]["id"]))
    conn.execute("UPDATE task_attempts SET at = ? WHERE id = ?", (_ts(10), rows[-1]["id"]))
    conn.commit()
    assert db.task_effort(conn, task)["elapsed_seconds"] == 600


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def test_item_page_shows_time_taken(finished, client):
    _, _, task, _ = finished
    html = client.get(f"/decisions/{task}").text
    assert "Took 1h" in html


def test_item_page_shows_nothing_for_unfinished_work(db_path, client):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    task = db.create_task(conn, project_id=project["id"], title="x")
    assert "Took " not in client.get(f"/decisions/{task}").text


def test_work_done_row_shows_time_and_tokens(finished, client):
    conn, project, task, config_dir = finished
    folder = config_dir / "projects" / "-tmp-acme"
    folder.mkdir(parents=True)
    (folder / "sess-1.jsonl").write_text(_assistant(60, "b", 1200, 300) + "\n")
    html = client.get(f"/projects/{project['id']}/work").text
    assert "1h" in html and "1.5k tokens" in html
