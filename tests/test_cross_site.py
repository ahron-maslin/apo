"""The dashboard listens on loopback only, but any page open in the user's browser can
still send it requests. Those are refused; the dashboard's own pages and clients that
aren't browsers (the CLI, curl) still work."""
import pytest
from fastapi.testclient import TestClient

from apo import db

DASHBOARD = "http://127.0.0.1:8430"


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app, base_url=DASHBOARD)


def _decision(tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id,
                                      title="Pick a queue", question="Which queue?")


def _answer(client, item, headers):
    return client.post(f"/decisions/{item}/answer", data={"answer_text": "Use SQS"},
                       headers=headers, follow_redirects=False)


def _status(item):
    return db.get_decision_request(db.get_connection(), item)["status"]


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9999"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Referer": "https://evil.example/page"},
])
def test_a_form_post_from_another_site_is_refused(client, tmp_path, headers):
    item = _decision(tmp_path)

    assert _answer(client, item, headers).status_code == 403
    assert _status(item) == "open"


@pytest.mark.parametrize("headers", [
    {"Origin": DASHBOARD, "Sec-Fetch-Site": "same-origin", "Referer": DASHBOARD + "/"},
    {"Sec-Fetch-Site": "none"},
    {},
])
def test_the_dashboards_own_pages_and_the_cli_can_still_post(client, tmp_path, headers):
    item = _decision(tmp_path)

    assert _answer(client, item, headers).status_code == 303
    assert _status(item) == "answered"


@pytest.mark.parametrize("host", ["rebind.evil.example", "rebind.evil.example:8430", "192.168.1.5:8430"])
def test_a_request_under_any_other_host_name_is_refused(client, tmp_path, host):
    """DNS rebinding: a hostile name pointed at 127.0.0.1 would otherwise read every page."""
    _decision(tmp_path)

    assert client.get("/api/decisions", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8430", "localhost:8430", "[::1]:8430", "LOCALHOST"])
def test_loopback_names_are_served(client, host):
    assert client.get("/api/decisions", headers={"Host": host}).status_code == 200


def test_no_page_can_be_framed_by_another_site(client):
    response = client.get("/")

    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("content_type", [None, "text/plain"])
def test_the_json_api_takes_only_json(client, content_type):
    """A cross-site fetch can send a typeless body without a CORS preflight."""
    headers = {"Content-Type": content_type} if content_type else {}

    response = client.post("/api/decisions", content=b'{"project": "acme", "title": "T", "question": "Q?"}',
                           headers=headers)

    assert response.status_code == 415
    assert client.get("/api/decisions").json() == []
