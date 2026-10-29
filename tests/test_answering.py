"""Answering without typing. Live: 7 of 17 answers were under 62 characters and most
picked one of the options, yet every answer had to be typed — including "done"."""
import pytest

from apo import db


def _decision(conn, **fields):
    project = db.get_or_create_project(conn, "acme")
    defaults = {"title": "Queue backend", "question": "Postgres or SQLite?"}
    return db.create_decision_request(conn, project_id=project["id"], **{**defaults, **fields})


def test_compose_answer_for_each_quick_choice(db_path):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, _decision(
        conn, options=["Postgres", "SQLite"], recommendation="Postgres, for concurrent workers.",
    ))

    assert db.compose_answer(decision, "option", option=1) == ("Chose option 2: SQLite", "option", 1)
    assert db.compose_answer(decision, "option", option=0, note="and use v16")[0] == (
        "Chose option 1: Postgres\n\nNote: and use v16"
    )
    assert db.compose_answer(decision, "recommendation")[0] == (
        "Accepted your recommendation: Postgres, for concurrent workers."
    )
    assert db.compose_answer(decision, "delegate")[0].startswith("Use your judgment")
    assert "need more information first: which region?" in db.compose_answer(decision, "clarify", note="which region?")[0]
    assert db.compose_answer(decision, "decline", note="too risky")[0] == "Declined — don't do this. too risky"
    assert db.compose_answer(decision, "done")[0] == "Done."
    assert db.compose_answer(decision, "text", note="Use SQLite for now")[0] == "Use SQLite for now"


@pytest.mark.parametrize("kind,kwargs", [
    ("text", {}),
    ("clarify", {}),
    ("option", {"option": 5}),
    ("nonsense", {}),
])
def test_compose_answer_rejects_incomplete_answers(db_path, kind, kwargs):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, _decision(conn, options=["A", "B"]))
    with pytest.raises(ValueError):
        db.compose_answer(decision, kind, **kwargs)


def test_accepting_a_missing_recommendation_is_rejected(db_path):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, _decision(conn))
    with pytest.raises(ValueError):
        db.compose_answer(decision, "recommendation")


def test_the_answer_kind_and_option_are_recorded(db_path):
    conn = db.get_connection()
    decision_id = _decision(conn, options=["Postgres", "SQLite"])

    db.answer_decision_request(conn, decision_id, "Chose option 2: SQLite", answer_kind="option", answer_option=1)

    row = db.get_decision_request(conn, decision_id)
    assert (row["answer_kind"], row["answer_option"]) == ("option", 1)


def test_options_keep_consequence_and_reversibility(db_path):
    conn = db.get_connection()
    decision_id = _decision(conn, options=[
        {"label": "Postgres", "consequence": "adds a compose service", "reversible": True},
        "SQLite",
    ])

    options = db.get_decision_request(conn, decision_id)["options"]
    assert options[0] == {"label": "Postgres", "detail": None, "consequence": "adds a compose service", "reversible": True}
    assert options[1] == {"label": "SQLite", "detail": None, "consequence": None, "reversible": None}


def test_the_recommended_option_is_found_from_its_label_when_not_given(db_path):
    """Agents mark it in the label today: "Drawer (RECOMMENDED)"."""
    conn = db.get_connection()
    decision_id = _decision(conn, options=["Bottom tabs", "Drawer (RECOMMENDED)", "Sidebar"])
    assert db.get_decision_request(conn, decision_id)["recommended_option"] == 1

    explicit = _decision(conn, options=["A", "B (recommended)"], recommended_option=0)
    assert db.get_decision_request(conn, explicit)["recommended_option"] == 0


@pytest.mark.parametrize("text,kind", [
    ("use sk_live_51HxQe2abcdefghijklmnop", "Stripe secret key"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GitHub token"),
    ("AKIAIOSFODNN7EXAMPLE is the key id", "AWS access key"),
    ("xoxb-123456789012-abcdefghijkl", "Slack token"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
])
def test_answers_that_look_like_secrets_are_recognised(text, kind):
    assert db.looks_like_secret(text) == kind


def test_ordinary_answers_are_not_secrets():
    for text in ("Use Postgres", "the sk_ prefix is fine", "Chose option 2: SQLite", "see AKIA docs"):
        assert db.looks_like_secret(text) is None


def test_the_server_refuses_a_secret_shaped_answer_unless_confirmed(db_path):
    from fastapi.testclient import TestClient

    from apo.web import app

    client = TestClient(app.app)
    conn = db.get_connection()
    decision_id = _decision(conn)

    # Refused back onto the page with the reason, never a raw error page.
    refused = client.post(f"/decisions/{decision_id}/answer", data={"answer_text": "sk_live_51HxQe2abcdefghijklmnop"})
    assert refused.status_code == 200 and "page-alert" in refused.text
    assert "written into the repo" in refused.text
    assert db.get_decision_request(conn, decision_id)["answer_text"] is None

    client.post(f"/decisions/{decision_id}/answer",
                data={"answer_text": "sk_live_51HxQe2abcdefghijklmnop", "allow_secret": "1"})
    assert db.get_decision_request(conn, decision_id)["answer_text"].startswith("sk_live_")


def test_answer_forms_carry_the_secret_check(db_path):
    from fastapi.testclient import TestClient

    from apo.web import app

    conn = db.get_connection()
    decision_id = _decision(conn)
    text = TestClient(app.app).get(f"/decisions/{decision_id}").text
    assert "data-secret-check" in text


def test_a_missing_summary_is_derived_from_the_question(db_path):
    """1 of 21 live requests had the human-facing summary; cards fell back to
    questions of up to 1,100 characters."""
    conn = db.get_connection()
    decision_id = _decision(conn, question="Should the queue use Postgres or SQLite? We have two workers now. More later.")

    row = db.get_decision_request(conn, decision_id)
    assert row["summary"] == "Should the queue use Postgres or SQLite?"
    assert row["summary_derived"] == 1

    given = _decision(conn, summary="Pick the queue database.")
    assert db.get_decision_request(conn, given)["summary_derived"] == 0
