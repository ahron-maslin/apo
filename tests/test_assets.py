"""Scripts and styles are referenced with a content version (?v=…), so a deploy can't
leave a browser running a cached old app.js against new server code — which once
brought back a fixed bug (clicking Done sent an empty answer) for a user whose
browser never re-fetched the script."""
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import db

STATIC = Path(__file__).resolve().parent.parent / "src" / "apo" / "web" / "static"


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def test_every_script_and_stylesheet_is_versioned_by_its_content(client, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))

    for path in ("/", "/sessions", f"/projects/{project['id']}", "/round?keys=r:1", "/settings"):
        html = client.get(path).text
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        assert refs, path
        for ref in refs:
            name, _, version = ref[len("/static/"):].partition("?v=")
            assert version, f"{path}: {ref} has no version"
            assert version == hashlib.md5((STATIC / name).read_bytes()).hexdigest()[:10], ref


def test_a_preview_dashboard_says_so_on_every_page(client, monkeypatch):
    assert "preview-banner" not in client.get("/settings").text

    monkeypatch.setenv("APO_PREVIEW", "1")

    assert "preview-banner" in client.get("/settings").text
