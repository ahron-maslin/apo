"""Outcomes: a result the human wants ("Launch the site"), with steps owned by the human,
an agent, or someone external. A checklist is just an outcome's steps in order; a
handoff is a step changing owner."""
import pytest

from apo import db


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project


def test_an_outcome_with_mixed_owner_steps_tracks_progress_and_next_owner(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "Launch the site", done_when="Live with a real payment")
    build = db.add_step(conn, outcome_id, "Configure the build", owner="agent")
    dns = db.add_step(conn, outcome_id, "Add the DNS record", owner="me")
    propagate = db.add_step(conn, outcome_id, "DNS propagation", owner="external", waiting_on="Cloudflare")

    progress = db.outcome_progress(conn, outcome_id)
    assert [s["owner"] for s in progress["steps"]] == ["agent", "me", "external"]
    assert progress["done"] == 0 and progress["total"] == 3
    assert progress["next"]["id"] == build and progress["next_owner"] == "agent"

    db.complete_step(conn, build, by="agent")
    db.complete_step(conn, dns, by="human")
    progress = db.outcome_progress(conn, outcome_id)
    assert progress["done"] == 2
    assert progress["next"]["id"] == propagate and progress["next_owner"] == "external"
    assert progress["ready_to_close"] is False

    db.complete_step(conn, propagate, by="human")
    assert db.outcome_progress(conn, outcome_id)["ready_to_close"] is True


def test_step_kinds_map_to_owners(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    assert db.get_decision_request(conn, db.add_step(conn, outcome_id, "a", owner="me"))["kind"] == "action"
    assert db.get_decision_request(conn, db.add_step(conn, outcome_id, "b", owner="agent"))["kind"] == "task"
    assert db.get_decision_request(conn, db.add_step(conn, outcome_id, "c", owner="external"))["kind"] == "wait"
    with pytest.raises(ValueError):
        db.add_step(conn, outcome_id, "d", owner="someone")


def test_a_human_created_step_marked_done_is_closed_not_waiting_for_pickup(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    step = db.add_step(conn, outcome_id, "Verify payouts", owner="me")

    db.complete_step(conn, step, by="human", note="looked fine")

    row = db.get_decision_request(conn, step)
    assert row["status"] == "resolved" and row["delivery"] == "none"
    assert db.list_in_transit(conn) == []


def test_a_blocked_step_is_not_your_move_until_its_blocker_is_done(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "Launch")
    domain = db.add_step(conn, outcome_id, "Add custom domain", owner="me")
    dns = db.add_step(conn, outcome_id, "Add DNS record", owner="me", blocked_by=[domain])

    keys = [i["key"] for i in db.now_queue(conn)["your_move"]]
    assert f"r:{domain}" in keys and f"r:{dns}" not in keys
    assert db.outcome_progress(conn, outcome_id)["steps"][1]["blocked_by"] == [domain]

    db.complete_step(conn, domain, by="human")
    assert f"r:{dns}" in [i["key"] for i in db.now_queue(conn)["your_move"]]


def test_dependencies_cannot_form_a_cycle(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    a = db.add_step(conn, outcome_id, "a", owner="me")
    b = db.add_step(conn, outcome_id, "b", owner="me", blocked_by=[a])
    with pytest.raises(ValueError):
        db.add_dependency(conn, a, b)
    with pytest.raises(ValueError):
        db.add_dependency(conn, a, a)


def test_agent_tasks_are_not_in_the_humans_queue(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    task = db.add_step(conn, outcome_id, "Write the webhook handler", owner="agent")

    now = db.now_queue(conn)
    assert f"r:{task}" not in [i["key"] for lane in ("your_move", "asks") for i in now[lane]]


def test_handing_a_step_between_you_and_an_agent(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    step = db.add_step(conn, outcome_id, "Set up Stripe webhooks", owner="me")

    db.reassign_step(conn, step, "agent")
    assert db.get_decision_request(conn, step)["kind"] == "task"
    db.reassign_step(conn, step, "me")
    assert db.get_decision_request(conn, step)["kind"] == "action"


def test_an_overdue_external_wait_becomes_check_on_this(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    wait = db.add_step(conn, outcome_id, "App Store review", owner="external", waiting_on="Apple",
                       follow_up_at="2026-01-01T00:00:00+00:00")

    item = next(i for i in db.now_queue(conn)["your_move"] if i["key"] == f"r:{wait}")
    assert "Apple" in item["reason"]


def test_an_outcome_ready_to_close_is_up_for_review_and_only_you_close_it(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "Ship it")
    step = db.add_step(conn, outcome_id, "Do it", owner="agent")
    db.complete_step(conn, step, by="agent")

    assert f"o:{outcome_id}" in [i["key"] for i in db.now_queue(conn)["review"]]

    db.set_outcome_status(conn, outcome_id, "achieved")
    assert db.get_outcome(conn, outcome_id)["status"] == "achieved"
    assert f"o:{outcome_id}" not in [i["key"] for i in db.now_queue(conn)["review"]]


def test_dropping_an_outcome_cancels_its_open_steps(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "O")
    open_step = db.add_step(conn, outcome_id, "a", owner="me")
    done_step = db.add_step(conn, outcome_id, "b", owner="me")
    db.complete_step(conn, done_step, by="human")

    db.set_outcome_status(conn, outcome_id, "dropped")

    assert db.get_decision_request(conn, open_step)["status"] == "cancelled"
    assert db.get_decision_request(conn, done_step)["status"] == "resolved"


def test_list_outcomes_by_project_with_progress(acme):
    conn, project = acme
    first = db.create_outcome(conn, project["id"], "First")
    db.add_step(conn, first, "a", owner="me")
    db.create_outcome(conn, project["id"], "Second")

    outcomes = db.list_outcomes(conn, project_id=project["id"])
    assert [o["title"] for o in outcomes] == ["First", "Second"]
    assert outcomes[0]["progress"]["total"] == 1
    assert outcomes[0]["project_name"] == "acme"


def test_outcome_events_are_recorded(acme):
    conn, project = acme
    outcome_id = db.create_outcome(conn, project["id"], "Launch")
    step = db.add_step(conn, outcome_id, "a", owner="agent")
    db.complete_step(conn, step, by="agent")
    db.set_outcome_status(conn, outcome_id, "achieved")

    verbs = [e["verb"] for e in db.list_events(conn, project_id=project["id"], oldest_first=True, min_importance=0)]
    assert "outcome.created" in verbs and "outcome.achieved" in verbs
