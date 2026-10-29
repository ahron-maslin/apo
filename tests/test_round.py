"""Clearing your moves fast: "I have 5 minutes" fills the time by score per minute,
a lightning round takes them one at a time with the keyboard, and reversible
recommendations the agent is sure of can be accepted in one go — after a list."""
from urllib.parse import parse_qs, urlparse

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


def _item(conn, project, title, minutes, **fields):
    return db.create_decision_request(conn, project_id=project["id"], title=title, question=f"{title}?",
                                      est_minutes=minutes, **fields)


def _same_age(conn):
    conn.execute("UPDATE decision_requests SET created_at = '2026-10-01T00:00:00+00:00'")
    conn.commit()


def _choice(conn, project, title, confidence, reversible, recommended=0):
    return db.create_decision_request(
        conn, project_id=project["id"], title=title, question=f"{title}?",
        options=[{"label": "Postgres", "reversible": reversible}, {"label": "SQLite", "reversible": reversible}],
        recommended_option=recommended, recommendation="Postgres — it's already running", confidence=confidence,
    )


def test_a_time_box_is_filled_by_score_per_minute(acme):
    conn = db.get_connection()
    _item(conn, acme, "Big", 10)
    _item(conn, acme, "Small", 1)
    _item(conn, acme, "Medium", 2)
    _same_age(conn)

    plan = db.time_boxed_plan(db.now_queue(conn), 3)

    assert [i["title"] for i in plan] == ["Small", "Medium"]


def test_a_time_box_always_has_something_in_it(acme):
    conn = db.get_connection()
    _item(conn, acme, "Big", 10)

    assert [i["title"] for i in db.time_boxed_plan(db.now_queue(conn), 2)] == ["Big"]


def test_no_time_box_means_everything_ranked(acme):
    conn = db.get_connection()
    for title in ("A", "B", "C"):
        _item(conn, acme, title, 5)

    assert len(db.time_boxed_plan(db.now_queue(conn), None)) == 3


def test_starting_a_round_fixes_the_plan_in_the_url(client, acme):
    conn = db.get_connection()
    first = _item(conn, acme, "Small", 1)
    second = _item(conn, acme, "Medium", 2)
    _same_age(conn)

    resp = client.get("/round?minutes=3", follow_redirects=False)

    assert resp.status_code == 303
    keys = parse_qs(urlparse(resp.headers["location"]).query)["keys"][0].split(",")
    assert keys == [f"r:{first}", f"r:{second}"]


def test_the_round_walks_through_the_plan_one_item_at_a_time(client, acme):
    conn = db.get_connection()
    first = _item(conn, acme, "Pick a queue", 1)
    second = _item(conn, acme, "Pick a cache", 2)
    url = f"/round?keys=r:{first},r:{second}"

    page = client.get(url).text
    assert "Pick a queue" in page and "Pick a cache" not in page
    assert "1 of 2" in page
    assert f'name="next" value="{url}"' in page

    client.post(f"/decisions/{first}/answer", data={"answer_text": "Postgres", "next": url})
    page = client.get(url).text
    assert "Pick a cache" in page and "2 of 2" in page

    client.post(f"/decisions/{second}/answer", data={"answer_text": "Redis", "next": url})
    assert "Round done" in client.get(url).text


def test_skipping_moves_on_without_answering(client, acme):
    conn = db.get_connection()
    first = _item(conn, acme, "Pick a queue", 1)
    second = _item(conn, acme, "Pick a cache", 2)

    page = client.get(f"/round?keys=r:{first},r:{second}&skip=r:{first}").text

    assert "Pick a cache" in page
    assert db.get_decision_request(conn, first)["status"] == "open"


def test_bulk_accept_takes_only_sure_reversible_recommendations(acme):
    conn = db.get_connection()
    sure = _choice(conn, acme, "Sure and reversible", 90, True)
    _choice(conn, acme, "Unsure", 70, True)
    _choice(conn, acme, "One-way door", 95, False)
    _choice(conn, acme, "Unknown door", 95, None)
    db.create_decision_request(conn, project_id=acme["id"], title="No pick", question="?", confidence=95,
                               recommendation="Do something")

    assert [d["id"] for d in db.bulk_accept_candidates(conn)] == [sure]


def test_bulk_accept_page_lists_then_accepts(client, acme):
    conn = db.get_connection()
    sure = _choice(conn, acme, "Sure and reversible", 90, True)
    one_way = _choice(conn, acme, "Drop the legacy table", 95, False)

    page = client.get("/accept").text
    assert "Sure and reversible" in page and "Drop the legacy table" not in page
    assert f'name="ids" value="{sure}"' in page

    client.post("/accept", data={"ids": [str(sure), str(one_way)]})

    assert db.get_decision_request(conn, sure)["answer_kind"] == "recommendation"
    assert db.get_decision_request(conn, one_way)["status"] == "open"


def test_now_offers_the_time_boxes_and_bulk_accept(client, acme):
    conn = db.get_connection()
    _choice(conn, acme, "Sure and reversible", 90, True)

    page = client.get("/").text

    assert 'href="/round?minutes=5"' in page
    assert 'href="/accept"' in page
