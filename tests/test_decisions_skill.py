from pathlib import Path

SKILL = (Path(__file__).resolve().parent.parent / "src" / "apo" / "resources" / "skills" / "decisions" / "SKILL.md").read_text()


def test_a_bare_decisions_run_works_tasks_as_well_as_answers():
    """The session-start notice and INDEX.md both send READY tasks to /decisions, so a
    skill that stopped at "no answered decisions waiting" left them sitting there."""
    find = SKILL[SKILL.index("## 1."):SKILL.index("## One item")]

    assert "(ANSWERED|READY)" in find
    assert "/decisions answers" in find and "/decisions tasks" in find
    assert 'no answered decisions waiting" and stop' not in SKILL
    assert "asks for tasks too" not in SKILL


def test_the_skill_uses_the_installed_apo_command_and_no_one_machines_paths():
    assert "apo pending --all" in SKILL and "apo claim <id>" in SKILL and "apo resolve <id>" in SKILL
    for path in ("~/.claude/", "~/projects/", "python3 ", "cli.py", "ai-decision-orchestrator"):
        assert path not in SKILL, path
