"""Running Claude Code without a terminal: apply an answer in a background session,
start one on a work item, stop one. Only answers given locally may
start an agent on their own — a Linear comment needs one dashboard click first,
which is the boundary that retired the old dispatcher."""
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import claude_runtime
from apo import db


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    log = tmp_path / "claude-calls.log"
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s\\n" "$PWD" "$*" >> {log}\n'
        'case "$1" in\n'
        "  agents) echo '[]' ;;\n"
        "  stop) echo 'Stopped 3f2a9c1b' ;;\n"
        "  *) echo 'Started background session 3f2a9c1b (claude attach 3f2a9c1b)' ;;\n"
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_CLAUDE_BIN", str(fake))

    def calls():
        if not log.exists():
            return []
        return [tuple(line.split("|", 1)) for line in log.read_text().splitlines() if "|agents" not in line]

    return calls


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _answered(conn, tmp_path, source="dashboard", origin="sess-1", origin_ended=True):
    project = db.get_or_create_project(conn, "acme")
    repo = db.get_repository(conn, db.ensure_repository_for_project(conn, project["id"], str(tmp_path)))
    if origin:
        db.register_session(conn, origin, str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], origin_session_id=origin,
        title="Queue backend", question="Postgres or SQLite?",
    )
    if origin and origin_ended:
        db.session_end(conn, origin, "prompt_input_exit")
    db.answer_decision_request(conn, decision_id, "Postgres", source=source)
    return decision_id


def test_start_background_runs_in_the_folder_and_returns_the_short_id(fake_claude, tmp_path):
    agent_id = claude_runtime.start_background(str(tmp_path), "/decisions #7", resume="sess-1")

    assert agent_id == "3f2a9c1b"
    assert fake_claude() == [(str(tmp_path), "--bg --resume sess-1 /decisions #7")]


def test_start_background_reports_a_failed_launch(tmp_path, monkeypatch):
    fake = tmp_path / "broken-claude"
    fake.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_CLAUDE_BIN", str(fake))

    with pytest.raises(claude_runtime.LaunchError, match="not logged in"):
        claude_runtime.start_background(str(tmp_path), "hello")


def test_nothing_that_looks_like_a_flag_reaches_the_cli(fake_claude, tmp_path):
    with pytest.raises(claude_runtime.LaunchError):
        claude_runtime.start_background(str(tmp_path), "--dangerously-skip-permissions")
    with pytest.raises(claude_runtime.LaunchError):
        claude_runtime.start_background(str(tmp_path), "hi", resume="--settings=/tmp/x")
    with pytest.raises(claude_runtime.LaunchError):
        claude_runtime.stop_background("-x")
    assert fake_claude() == []


def test_stop_background(fake_claude):
    assert claude_runtime.stop_background("3f2a9c1b") is True
    assert fake_claude()[0][1] == "stop 3f2a9c1b"


def test_plan_resumes_the_session_that_asked_once_it_has_ended(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _answered(conn, tmp_path)

    plan = db.apply_plan(conn, decision_id)

    assert plan == {"cwd": str(tmp_path), "resume": "sess-1", "prompt": f"/decisions #{decision_id} --here",
                    "needs_approval": False, "error": None}


def test_plan_never_resumes_a_session_that_is_still_open(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _answered(conn, tmp_path, origin_ended=False)

    plan = db.apply_plan(conn, decision_id)

    assert plan["resume"] is None
    assert plan["cwd"] == str(tmp_path)


def test_plan_for_an_answer_from_linear_needs_a_dashboard_click(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _answered(conn, tmp_path, source="linear")

    assert db.apply_plan(conn, decision_id)["needs_approval"] is True


def test_plan_refuses_an_unanswered_decision(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="T", question="Q?")

    assert db.apply_plan(conn, decision_id)["error"]


def test_apply_route_launches_and_records_the_dispatch(client, fake_claude, tmp_path):
    conn = db.get_connection()
    decision_id = _answered(conn, tmp_path)

    resp = client.post(f"/decisions/{decision_id}/apply", follow_redirects=False)

    assert resp.status_code == 303
    assert fake_claude() == [(str(tmp_path), f"--bg --resume sess-1 /decisions #{decision_id} --here")]
    decision = db.get_decision_request(conn, decision_id)
    assert decision["dispatch_agent_id"] == "3f2a9c1b"
    assert decision["dispatched_at"]
    verbs = [e["verb"] for e in db.list_events(conn, decision_id=decision_id)]
    assert "delivery.dispatched" in verbs
    assert "Applying in background" in client.get(f"/decisions/{decision_id}").text


def test_apply_route_does_not_launch_twice(client, fake_claude, tmp_path):
    decision_id = _answered(db.get_connection(), tmp_path)

    client.post(f"/decisions/{decision_id}/apply")
    client.post(f"/decisions/{decision_id}/apply")

    assert len(fake_claude()) == 1


def test_a_linear_answer_needs_the_approval_click(client, fake_claude, tmp_path):
    decision_id = _answered(db.get_connection(), tmp_path, source="linear")

    page = client.get(f"/decisions/{decision_id}").text
    assert "Approve and apply in background" in page

    refused = client.post(f"/decisions/{decision_id}/apply", follow_redirects=False)
    assert refused.status_code == 409
    assert fake_claude() == []

    client.post(f"/decisions/{decision_id}/apply", data={"approved": "1"})
    assert len(fake_claude()) == 1


def test_apply_button_shows_for_an_answer_nothing_will_pick_up(client, fake_claude, tmp_path):
    decision_id = _answered(db.get_connection(), tmp_path)

    page = client.get(f"/decisions/{decision_id}").text

    assert f'action="/decisions/{decision_id}/apply"' in page
    assert "Apply in background" in page


def test_a_failed_launch_is_shown_not_swallowed(client, tmp_path, monkeypatch):
    fake = tmp_path / "broken-claude"
    fake.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_CLAUDE_BIN", str(fake))
    decision_id = _answered(db.get_connection(), tmp_path)

    resp = client.post(f"/decisions/{decision_id}/apply")

    assert "not logged in" in resp.text
    assert db.get_decision_request(db.get_connection(), decision_id)["dispatch_agent_id"] is None


def test_reconcile_records_which_sessions_run_in_the_background(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "bg-1", str(tmp_path))

    db.reconcile_live_sessions(conn, [{"id": "3f2a9c1b", "kind": "background", "sessionId": "bg-1",
                                       "cwd": str(tmp_path), "status": "busy"}])

    row = db.get_session(conn, "bg-1")
    assert row["agent_id"] == "3f2a9c1b"
    assert row["agent_kind"] == "background"


def test_stopping_a_background_session(client, fake_claude, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "bg-1", str(tmp_path))
    db.reconcile_live_sessions(conn, [{"id": "3f2a9c1b", "kind": "background", "sessionId": "bg-1",
                                       "cwd": str(tmp_path), "status": "busy"}])

    resp = client.post(f"/sessions/{db.get_session(conn, 'bg-1')['id']}/stop", follow_redirects=False)

    assert resp.status_code == 303
    assert [call[1] for call in fake_claude()] == ["stop 3f2a9c1b"]


def test_an_interactive_session_cannot_be_stopped_from_here(client, fake_claude, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "term-1", str(tmp_path))
    db.reconcile_live_sessions(conn, [{"id": "aa11bb22", "kind": "interactive", "sessionId": "term-1",
                                       "cwd": str(tmp_path), "status": "busy"}])

    resp = client.post(f"/sessions/{db.get_session(conn, 'term-1')['id']}/stop", follow_redirects=False)

    assert resp.status_code == 409
    assert fake_claude() == []


def test_session_cards_offer_open_in_vscode(client, tmp_path):
    db.register_session(db.get_connection(), "sess-9", str(tmp_path))

    assert "vscode://anthropic.claude-code/open?session=sess-9" in client.get("/sessions").text


def test_a_stuck_answer_on_now_can_be_applied_and_then_stops_asking(client, fake_claude, tmp_path):
    conn = db.get_connection()
    decision_id = _answered(conn, tmp_path)
    conn.execute("UPDATE decision_requests SET answered_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (decision_id,))
    conn.commit()

    assert f'action="/decisions/{decision_id}/apply"' in client.get("/").text

    client.post(f"/decisions/{decision_id}/apply", data={"next": "/"})

    queue = db.now_queue(conn)
    assert [i["key"] for i in queue["your_move"]] == []
    transit = [i for i in queue["in_transit"] if i["key"] == f"t:{decision_id}"]
    assert transit and transit[0]["reason"].startswith("Applying in background")


def test_the_decisions_skill_works_a_dispatched_item_itself():
    """A session the dashboard starts for one item already has nothing else in it, so
    handing the item to a subagent would only pay for a second context."""
    skill = (Path(__file__).resolve().parent.parent / "src" / "apo" / "resources" / "skills" / "decisions" / "SKILL.md").read_text()

    here = skill[skill.index("--here"):]
    assert "do not spawn a subagent" in here.lower()
    assert "claim_task" in here and "mark_decision_resolved" in here and "complete_task" in here
