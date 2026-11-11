"""Layout that was broken rather than a matter of taste: badges that sat on the next
card, and a folder path printed in full where everywhere else shortens it to ~."""
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path):
    folder = Path.home() / "projects" / "acme"
    folder.mkdir(parents=True)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(folder))
    return project


def _section_head(html, heading_id):
    start = html.index(f'id="{heading_id}"')
    return html[html.rindex('<div class="section-head">', 0, start):html.index("</div>", start)]


def test_the_overdue_and_stuck_counts_head_the_needs_you_list_on_the_overview(client, acme):
    db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain",
                due_on=(date.today() - timedelta(days=1)).isoformat())

    html = client.get(f"/projects/{acme['id']}").text

    assert "1 overdue" in _section_head(html, "needs-heading")
    assert html.count('class="due-summary"') == 1


def test_the_project_header_shows_its_folder_from_home(client, acme):
    html = client.get(f"/projects/{acme['id']}").text
    header = html[html.index('class="page-desc"'):html.index("</p>", html.index('class="page-desc"'))]

    assert "~/projects/acme" in header and str(Path.home()) not in header
