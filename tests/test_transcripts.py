import json

from apo import transcripts


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n")


def test_away_summary_written_before_the_latest_prompt_is_ignored(tmp_path):
    """Claude Code writes a recap when you come back, before your next prompt. By the
    time that turn's Stop fires, the recap is missing the turn that just happened."""
    p = tmp_path / "t.jsonl"
    _write(p, [
        json.dumps({"type": "system", "subtype": "away_summary", "content": "Recap of before",
                    "timestamp": "2026-10-07T10:00:00.000Z"}),
        json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": "now do X"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Did X."}]}}),
    ])

    info = transcripts.tail_info(str(p))

    assert info["away_summary"] is None
    assert info["last_assistant"] == "Did X."


def test_away_summary_after_the_latest_prompt_comes_with_its_own_time(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": "do X"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Did X."}]}}),
        json.dumps({"type": "system", "subtype": "away_summary", "content": "Did X and Y.",
                    "timestamp": "2026-10-07T12:06:05.571Z"}),
    ])

    info = transcripts.tail_info(str(p))

    assert info["away_summary"] == "Did X and Y."
    assert info["away_summary_at"] == "2026-10-07T12:06:05.571000+00:00"


def test_tail_info_reads_title_prompt_and_last_assistant_text(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        json.dumps({"type": "ai-title", "aiTitle": "Old title"}),
        json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": "first ask"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first reply"}]}}),
        json.dumps({"type": "ai-title", "aiTitle": "New title"}),
        json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": [{"type": "text", "text": "second ask"}]}}),
        json.dumps({"type": "user", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "second reply"}]}}),
    ])

    info = transcripts.tail_info(str(p))

    assert info == {
        "title": "New title", "last_prompt": "second ask", "last_assistant": "second reply",
        "away_summary": None, "away_summary_at": None, "context_tokens": None,
    }


def test_tail_info_skips_malformed_and_blank_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, ["", "not json", json.dumps({"type": "ai-title", "aiTitle": "T"}), "{broken"])

    assert transcripts.tail_info(str(p))["title"] == "T"


def test_tail_info_missing_file_returns_empty(tmp_path):
    assert transcripts.tail_info(str(tmp_path / "nope.jsonl")) == {
        "title": None, "last_prompt": None, "last_assistant": None, "away_summary": None,
        "away_summary_at": None, "context_tokens": None,
    }
    assert transcripts.tail_info(None)["title"] is None


def test_tail_info_reads_the_latest_away_summary(tmp_path):
    """away_summary is Claude Code's own built-in session recap (the "✳ recap:"
    line shown when you return after stepping away, or via /recap) — a real
    synthesized catch-up, not a raw message. It's written into the transcript as
    a type:"system"/subtype:"away_summary" entry, free to read."""
    p = tmp_path / "t.jsonl"
    _write(p, [
        json.dumps({"type": "system", "subtype": "away_summary", "content": "Older recap."}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}}),
        json.dumps({"type": "system", "subtype": "away_summary", "content": "Newest recap."}),
    ])

    assert transcripts.tail_info(str(p))["away_summary"] == "Newest recap."


def test_tail_info_strips_the_disable_recaps_hint_from_away_summary(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        json.dumps({
            "type": "system", "subtype": "away_summary",
            "content": "Working on the thing. (disable recaps in /config)",
        }),
    ])

    assert transcripts.tail_info(str(p))["away_summary"] == "Working on the thing."


def test_tail_info_ignores_other_system_subtypes(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 100})])

    assert transcripts.tail_info(str(p))["away_summary"] is None


def test_tail_info_only_reads_the_tail_of_large_files(tmp_path):
    p = tmp_path / "t.jsonl"
    filler = json.dumps({"type": "progress", "data": "x" * 1000})
    _write(p, [json.dumps({"type": "ai-title", "aiTitle": "Too far back"})] + [filler] * 600 + [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "recent"}]}}),
    ])

    info = transcripts.tail_info(str(p), max_bytes=64 * 1024)

    assert info["title"] is None
    assert info["last_assistant"] == "recent"


def test_tail_info_falls_back_to_last_prompt_entry(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [json.dumps({"type": "last-prompt", "lastPrompt": "from last-prompt"})])

    assert transcripts.tail_info(str(p))["last_prompt"] == "from last-prompt"


def test_clean_prompt_strips_pasted_content_blocks():
    text = 'look at this <pasted_content id="1">huge\npaste</pasted_content>  and fix it'

    assert transcripts.clean_prompt(text) == "look at this [pasted] and fix it"
    assert transcripts.clean_prompt('<pasted_content id="1">only paste</pasted_content>') == "[pasted]"
