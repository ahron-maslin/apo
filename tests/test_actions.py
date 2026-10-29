"""Structured Actions: what an agent needs the human to do, in a shape the human can
do in one pass — where to go, steps, values to copy, what to hand back (secrets kept
out of the repo), and a check that it actually landed."""
import re

from fastapi.testclient import TestClient

from apo import db
from apo import verify

SPEC = {
    "where": "https://dash.cloudflare.com/example.com/dns",
    "steps": ["Add the TXT record below", "Click Verify in Resend"],
    "values": [{"label": "Name", "value": "resend._domainkey.news"}, {"label": "Value", "value": "p=MIGf"}],
    "inputs": [{"name": "WEBHOOK_SECRET", "label": "Stripe webhook secret", "secret": True},
               {"name": "account_email", "label": "Stripe account email"}],
    "done_when": "TXT resend._domainkey.news.example.com returns p=MIGf",
    "verify": {"type": "dns", "name": "resend._domainkey.news.example.com", "record_type": "TXT", "contains": "p=MIGf"},
}


def _action(conn, tmp_path, **spec):
    project = db.get_or_create_project(conn, "acme")
    repo = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return db.create_action_request(
        conn, project_id=project["id"], repository_id=repo, title="Add the DNS record", question="Q?",
        action_spec={**SPEC, **spec}, est_minutes=5,
    )


def test_an_action_keeps_its_spec_and_estimate(db_path, tmp_path):
    conn = db.get_connection()
    action = db.get_decision_request(conn, _action(conn, tmp_path))
    assert action["action_spec"]["where"] == SPEC["where"]
    assert action["est_minutes"] == 5
    assert next(i for i in db.now_queue(conn)["your_move"] if i["key"] == f"r:{action['id']}")["effort"] == 5


def test_the_mcp_tool_takes_the_structured_fields(db_path):
    from apo import mcp_server

    result = mcp_server.create_action_request(
        project="acme", title="Add the DNS record", question="Q?", where=SPEC["where"], steps=SPEC["steps"],
        values=SPEC["values"], inputs=SPEC["inputs"], done_when=SPEC["done_when"], verify=SPEC["verify"],
        est_minutes=5,
    )
    spec = db.get_decision_request(db.get_connection(), result["id"])["action_spec"]
    assert spec["verify"]["type"] == "dns" and spec["inputs"][0]["secret"] is True

    bad = mcp_server.create_action_request(project="acme", title="T", question="Q?", verify={"type": "shell"})
    assert "error" in bad


def test_the_action_page_lays_out_where_steps_values_and_inputs(db_path, tmp_path):
    from apo.web import app

    conn = db.get_connection()
    action_id = _action(conn, tmp_path)

    text = TestClient(app.app).get(f"/decisions/{action_id}").text

    assert SPEC["where"] in text
    assert "Click Verify in Resend" in text
    assert 'data-copy="resend._domainkey.news"' in text
    assert 'name="input_WEBHOOK_SECRET"' in text and 'type="password"' in text
    assert 'name="input_account_email"' in text
    assert SPEC["done_when"] in text


def test_done_runs_the_check_and_refuses_until_it_passes(db_path, tmp_path, monkeypatch):
    from apo.web import app

    conn = db.get_connection()
    action_id = _action(conn, tmp_path)
    client = TestClient(app.app)
    monkeypatch.setattr(app.verify, "run_check", lambda check, repo_path=None: verify.Result(False, "No TXT record yet"))

    resp = client.post(f"/decisions/{action_id}/answer", data={"choice": "done"}, follow_redirects=False)

    assert resp.status_code == 303 and "unverified=" in resp.headers["location"]
    assert db.get_decision_request(conn, action_id)["status"] == "open"
    assert "No TXT record yet" in client.get(resp.headers["location"]).text

    monkeypatch.setattr(app.verify, "run_check", lambda check, repo_path=None: verify.Result(True, "TXT: p=MIGf"))
    client.post(f"/decisions/{action_id}/answer", data={"choice": "done"})
    row = db.get_decision_request(conn, action_id)
    assert row["status"] == "answered" and "Verified: TXT: p=MIGf" in row["answer_text"]


def test_done_from_the_work_list_comes_back_to_it_only_once_the_check_passes(db_path, tmp_path, monkeypatch):
    from apo.web import app

    conn = db.get_connection()
    action_id = _action(conn, tmp_path)
    work = f"/projects/{db.get_decision_request(conn, action_id)['project_id']}/work"
    client = TestClient(app.app)
    form = re.search(rf'<form[^>]*action="/decisions/{action_id}/answer"[^>]*>(.*?)</form>', client.get(work).text, re.S)
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(1)))
    monkeypatch.setattr(app.verify, "run_check", lambda check, repo_path=None: verify.Result(False, "No TXT record yet"))

    resp = client.post(f"/decisions/{action_id}/answer", data=fields, follow_redirects=False)

    assert resp.headers["location"].startswith(f"/decisions/{action_id}?unverified=")
    assert db.get_decision_request(conn, action_id)["status"] == "open"

    monkeypatch.setattr(app.verify, "run_check", lambda check, repo_path=None: verify.Result(True, "TXT: p=MIGf"))
    resp = client.post(f"/decisions/{action_id}/answer", data=fields, follow_redirects=False)

    assert resp.headers["location"] == work
    assert db.get_decision_request(conn, action_id)["status"] == "answered"


def test_mark_done_anyway_skips_the_check(db_path, tmp_path, monkeypatch):
    from apo.web import app

    conn = db.get_connection()
    action_id = _action(conn, tmp_path)
    monkeypatch.setattr(app.verify, "run_check", lambda check, repo_path=None: verify.Result(False, "nope"))

    TestClient(app.app).post(f"/decisions/{action_id}/answer", data={"choice": "done", "force": "1"})

    row = db.get_decision_request(conn, action_id)
    assert row["status"] == "answered" and "Not verified" in row["answer_text"]


def test_inputs_go_to_the_agent_and_secrets_stay_out_of_the_repo(db_path, tmp_path, monkeypatch):
    from apo.web import app
    from apo import mcp_server

    conn = db.get_connection()
    action_id = _action(conn, tmp_path, verify=None)
    TestClient(app.app).post(f"/decisions/{action_id}/answer", data={
        "choice": "done", "input_WEBHOOK_SECRET": "whsec_live_supersecret", "input_account_email": "ops@example.com",
    })

    answer = db.get_decision_request(conn, action_id)["answer_text"]
    assert "ops@example.com" in answer
    assert "whsec_live_supersecret" not in answer and "get_action_secret" in answer
    record = next((tmp_path / ".claude" / "decisions").glob(f"{action_id:04d}-*.md")).read_text()
    assert "whsec_live_supersecret" not in record

    assert mcp_server.get_action_secret(decision_id=action_id, name="WEBHOOK_SECRET") == {"value": "whsec_live_supersecret"}
    assert "error" in mcp_server.get_action_secret(decision_id=action_id, name="NOPE")
