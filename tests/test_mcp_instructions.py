"""What every session learns about apo. Claude Code puts an MCP server's
instructions into each session's system prompt, so they're how an agent knows when to
file a decision, start a goal, work a task or read the brief — the tool descriptions
alone don't say how the pieces fit together."""
import asyncio

from apo import mcp_server

from apo import db


def test_the_instructions_cover_every_tool_the_server_offers():
    tools = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    text = mcp_server.mcp.instructions or ""

    assert [t for t in tools if t not in text] == []


def test_the_instructions_cover_the_workflow_around_the_tools():
    text = mcp_server.mcp.instructions

    for topic in ("TaskCreate", "/decisions", "project's folder", "apo claim", "resume_plan", "secret"):
        assert topic in text, topic


def test_reading_the_project_brief(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    db.set_project_brief(conn, project["id"], goal="Sell the app to schools", done_when="10 paying schools",
                         links="Repo https://github.com/me/acme")

    brief = mcp_server.get_project_brief("acme")

    assert brief["project"] == "acme"
    assert brief["purpose"] == "Sell the app to schools" and brief["done_when"] == "10 paying schools"
    assert brief["links"] == [{"label": "Repo", "url": "https://github.com/me/acme"}]
    assert brief["status"] == "quiet"
    assert "error" in mcp_server.get_project_brief("nope")


def test_listing_goals_with_their_steps(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    outcome_id = db.create_outcome(conn, project["id"], "Launch the listing", done_when="A real order")
    write = db.add_step(conn, outcome_id, "Write the listing copy", owner="agent")
    db.add_step(conn, outcome_id, "Approve the price", owner="me")
    db.complete_task(conn, write, note="Done")

    goals = mcp_server.list_outcomes("acme")

    assert [g["title"] for g in goals] == ["Launch the listing"]
    goal = goals[0]
    assert goal["progress"] == "1/2" and goal["done_when"] == "A real order"
    assert [(s["title"], s["owner"], s["done"]) for s in goal["steps"]] == [
        ("Write the listing copy", "agent", True), ("Approve the price", "me", False)]


def test_sessions_are_told_the_dashboard_is_called_apo():
    assert mcp_server.INSTRUCTIONS.startswith("apo is where")
    for old in ("Switchboard", "Control Center", "ADO", "ado "):
        assert old not in mcp_server.INSTRUCTIONS, old
