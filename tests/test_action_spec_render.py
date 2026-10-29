"""An action's instructions can leave out any part. "values" is also the name of a dict
method, so a template that subscripts a missing "values" gets the method and crashes
the whole Now page."""
import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def test_an_action_with_steps_but_no_values_renders(client, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "portfolio")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    action_id = db.create_action_request(
        conn, project_id=project["id"], title="Install Playwright", question="Run the e2e tests",
        action_spec={"where": "Terminal in ~/projects/my_site", "steps": ["npx playwright install"]},
    )

    now = client.get("/")
    page = client.get(f"/decisions/{action_id}")

    assert now.status_code == 200 and page.status_code == 200
    assert "npx playwright install" in page.text and 'class="action-values"' not in page.text


def test_an_actions_values_still_show(client, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "portfolio")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    action_id = db.create_action_request(
        conn, project_id=project["id"], title="Set the env var", question="Add it in Cloudflare",
        action_spec={"steps": ["Open settings"], "values": [{"label": "NEXT_PUBLIC_SITE_URL", "value": "https://x.dev"}]},
    )

    assert 'data-copy="https://x.dev"' in client.get(f"/decisions/{action_id}").text
