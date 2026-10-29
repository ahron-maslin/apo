"""A session resumed from ~ (`claude -r <id>` in a fresh terminal) runs in ~, but it
belongs to the folder it was created in — which is where Claude Code keeps its
transcript (~/.claude/projects/<that folder, encoded>/<id>.jsonl). Use that."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from apo import db



def _transcript_for(folder: Path, session_id: str, config: Path) -> str:
    return str(config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder)) / f"{session_id}.jsonl")


def _acme(conn, tmp_path):
    folder = tmp_path / "sideProject2"
    folder.mkdir()
    project = db.get_or_create_project(conn, "Initech")
    db.ensure_repository_for_project(conn, project["id"], str(folder))
    return project, folder


def test_a_session_resumed_from_home_belongs_to_the_folder_it_was_created_in(db_path, tmp_path):
    conn = db.get_connection()
    project, folder = _acme(conn, tmp_path)

    db.register_session(conn, "s-1", str(Path.home()),
                        transcript_path=_transcript_for(folder, "s-1", tmp_path / "claude-config"))

    session = db.get_session(conn, "s-1")
    assert session["project_id"] == project["id"]
    assert session["cwd"] == str(Path.home())


def test_a_session_in_its_own_folder_is_unaffected(db_path, tmp_path):
    conn = db.get_connection()
    project, folder = _acme(conn, tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()

    db.register_session(conn, "s-1", str(folder), transcript_path=_transcript_for(other, "s-1", tmp_path))

    assert db.get_session(conn, "s-1")["project_id"] == project["id"]


def test_the_session_is_told_where_its_project_is(db_path, tmp_path):
    conn = db.get_connection()
    _, folder = _acme(conn, tmp_path)
    env = dict(os.environ, APO_DB=str(db_path))
    payload = {"session_id": "s-1", "cwd": str(Path.home()), "source": "resume",
               "transcript_path": _transcript_for(folder, "s-1", tmp_path / "claude-config")}

    result = subprocess.run([sys.executable, "-m", "apo", "session-start"], input=json.dumps(payload),
                            capture_output=True, text=True, env=env)

    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert str(folder) in context and "/decisions" in context
