"""apo is the only name a person or an agent sees. The old names (ai-decision-orchestrator,
ADO, Control Center, Switchboard) survive only where something existing depends on them:
old settings, the old data folder, old POLICY.md files, old MCP and hook entries, browser
settings saved under the old name, and the deprecated `ado` command."""
import re
from pathlib import Path

import apo

PACKAGE = Path(apo.__file__).resolve().parent
OLD = re.compile(r"Switchboard|Control Center|Decision Orchestrator|ai-decision-orchestrator|\bADO\b|\bado\b")
# file -> the old names it may still contain, because something existing depends on them
ALLOWED = {
    "config.py": {"ai-decision-orchestrator", "ADO"},
    "repo_writer.py": {"Control Center"},
    "installer.py": {"ai-decision-orchestrator", "ado", "ADO"},
    "cli.py": {"ado"},
    "base.html": {"ado"},
}


def _files():
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def test_only_compatibility_code_mentions_an_old_name():
    found = {}
    for path in _files():
        names = set(OLD.findall(path.read_text(errors="ignore"))) - ALLOWED.get(path.name, set())
        if names:
            found[str(path.relative_to(PACKAGE))] = sorted(names)

    assert found == {}


# Spelled with a character class so this file doesn't match its own search.
MAINTAINER_PATHS = re.compile(r"ado-liv[e]|/hom[e]/|~/projects/ai-decision-orchestrato[r]")


def test_no_file_names_one_machines_paths():
    for path in _files():
        assert not MAINTAINER_PATHS.search(path.read_text(errors="ignore")), path
