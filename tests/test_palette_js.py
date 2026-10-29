"""The ⌘K command grammar (static/palette.js), run under node. The parser decides
what a POST will do, so it gets real tests, not just a syntax check."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PALETTE = str(Path(__file__).resolve().parent.parent / "src" / "apo" / "web" / "static" / "palette.js")
DATA = {
    "pages": [{"label": "Now", "href": "/"}, {"label": "Sessions", "href": "/sessions"},
              {"label": "Decisions", "href": "/decisions"}],
    "projects": [{"name": "acme"}],
    "requests": [
        {"id": 12, "title": "Pick a queue", "kind": "decision", "project": "acme",
         "options": ["Postgres", "SQLite"], "recommendation": "Postgres."},
        {"id": 7, "title": "Add DNS record", "kind": "action", "project": "acme", "options": [], "recommendation": None},
    ],
    "sessions": [{"title": "Webhook handler", "project": "acme", "status": "Running", "href": "/sessions#session-card-3"}],
}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js(expression):
    script = f"const P = require({json.dumps(PALETTE)}); const data = {json.dumps(DATA)}; console.log(JSON.stringify({expression}));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _parse(text):
    return _js(f"P.parseCommand({json.dumps(text)}, data)")


def test_accept_posts_the_recommendation_choice():
    command = _parse("accept 12")
    assert command["run"] == {"method": "post", "action": "/decisions/12/answer",
                              "fields": {"choice": "recommendation", "answer_text": ""}}
    assert "Postgres." in command["label"]
    assert _parse("y #12")["run"]["fields"]["choice"] == "recommendation"


def test_answer_with_a_number_picks_that_option():
    command = _parse("a 12 2")
    assert command["run"]["fields"] == {"option": "1"}
    assert "option 2: SQLite" in command["label"]
    assert _parse("answer 12 5")["error"] is True


def test_answer_with_words_is_a_typed_answer():
    assert _parse("answer 12 use the v2 API")["run"]["fields"] == {"answer_text": "use the v2 API"}


def test_done_decide_decline_and_snooze():
    assert _parse("done 7")["run"]["fields"]["choice"] == "done"
    assert _parse("decide 12")["run"]["fields"]["choice"] == "delegate"
    assert _parse("decline 12 too risky")["run"]["fields"] == {"choice": "decline", "answer_text": "too risky"}
    snooze = _parse("snooze 12 blocks")
    assert snooze["run"] == {"method": "post", "action": "/decisions/12/snooze", "fields": {"preset": "until_blocks"}}
    assert _parse("snooze 12 someday")["error"] is True


def test_commands_on_unknown_requests_or_missing_recommendations_are_refused():
    assert _parse("accept 99")["error"] is True
    assert _parse("accept 7")["error"] is True


def test_seen_and_go():
    assert _parse("seen")["run"]["action"] == "/seen"
    assert _parse("go sess")["run"] == {"href": "/sessions"}


def test_plain_text_is_a_search_not_a_command():
    assert _parse("pick a queue") is None
    labels = [r["label"] for r in _js('P.search("queue", data)')]
    assert labels[0] == "#12 Pick a queue"
    assert "Webhook handler" in [r["label"] for r in _js('P.search("webh", data)')]
    assert _js('P.search("zzzz", data)') == []


def test_search_verb_opens_the_search_page():
    assert _parse("search stripe webhook")["run"] == {"href": "/search?q=stripe%20webhook"}


def test_accept_all_opens_the_bulk_list_never_posts():
    assert _parse("accept all")["run"] == {"href": "/accept"}


def test_plan_starts_a_lightning_round():
    assert _parse("plan 5")["run"] == {"href": "/round?minutes=5"}
    assert _parse("round")["run"] == {"href": "/round"}


def test_apply_verb_posts_apply_in_background_for_any_item_number():
    # Answered items aren't in the palette's open-request list; the server decides.
    assert _parse("apply 99")["run"] == {"method": "post", "action": "/decisions/99/apply", "fields": {}}
