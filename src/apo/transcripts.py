"""Zero-LLM-cost "what was this session doing" extraction from a Claude Code
transcript (.jsonl). Called from the Stop hook, which runs under a 2s timeout, so
only the tail of the file is ever read — a long session's transcript can be tens
of MB. Anything unreadable just yields None fields; callers keep prior values."""
import json
import re
from datetime import datetime
from pathlib import Path

DEFAULT_TAIL_BYTES = 512 * 1024
# The session page reads more, but still only the tail.
CONVERSATION_TAIL_BYTES = 4 * 1024 * 1024
CONVERSATION_LIMIT = 400

_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content>", re.DOTALL)
# Claude Code appends this hint to an away_summary's own content the first few
# times it shows one per session — useful in the terminal, meaningless on a
# dashboard card.
_AWAY_SUMMARY_HINT = re.compile(r"\s*\(disable recaps in /config\)\s*$")


def _iso(timestamp) -> str | None:
    """Transcript timestamps end in 'Z', which datetime.fromisoformat (3.10) rejects."""
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def clean_prompt(text: str) -> str:
    return " ".join(_PASTED.sub(" [pasted] ", text).split())


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def describe_tool_call(tool: str | None, tool_input) -> str | None:
    """One line saying what a tool call wants to do — what a person needs to see to
    know what they're being asked to allow."""
    if not tool:
        return None
    if not isinstance(tool_input, dict):
        return tool
    target = (
        tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url")
        or tool_input.get("path") or tool_input.get("pattern")
    )
    if not isinstance(target, str) or not target.strip():
        return tool
    target = " ".join(target.split())
    return f"{tool}: {target[:200]}{'…' if len(target) > 200 else ''}"


def context_tokens(usage) -> int | None:
    """Everything a call sent the model, cached or not: what the next turn re-reads."""
    if not isinstance(usage, dict):
        return None
    parts = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    return sum(p for p in parts if isinstance(p, int)) or None


def find(session_id: str, config_dirs) -> Path | None:
    """<config dir>/projects/<encoded cwd>/<session id>.jsonl, in any of the profiles."""
    for config_dir in config_dirs:
        matches = list((Path(config_dir) / "projects").glob(f"*/{session_id}.jsonl"))
        if matches:
            return matches[0]
    return None


def _read_tail_lines(path: str, max_bytes: int) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # A mid-file start almost certainly lands inside a line.
    return lines[1:] if start > 0 else lines


def tail_info(path: str | None, max_bytes: int = DEFAULT_TAIL_BYTES) -> dict:
    """away_summary is Claude Code's own built-in session recap — the "✳ recap:"
    line shown automatically when you return after stepping away, or on demand via
    /recap: a real synthesized catch-up (what's been done, what's current), not a
    raw message dump. It's written into the transcript as a plain
    type:"system"/subtype:"away_summary" entry, so it costs nothing extra to read
    — and it's a strictly better "what was this session doing" than
    last_assistant alone, so callers should prefer it when present."""
    info = {"title": None, "last_prompt": None, "last_assistant": None, "away_summary": None, "away_summary_at": None,
            "context_tokens": None}
    if not path:
        return info
    try:
        lines = _read_tail_lines(path, max_bytes)
    except OSError:
        return info

    fallback_prompt = None
    # A recap is only current if it was written after the latest human prompt: Claude
    # Code writes one when you come back, *before* your next prompt, so by that turn's
    # Stop it no longer covers the turn that just happened.
    away_summary_settled = False
    for line in reversed(lines):
        if (info["title"] and info["last_prompt"] and info["last_assistant"] and away_summary_settled
                and info["context_tokens"]):
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "ai-title" and not info["title"]:
            info["title"] = entry.get("aiTitle") or None
        elif kind == "last-prompt" and not fallback_prompt:
            fallback_prompt = entry.get("lastPrompt") or None
        elif kind == "user":
            origin = entry.get("origin")
            if isinstance(origin, dict) and origin.get("kind") == "human":
                away_summary_settled = True
                text = _text_of((entry.get("message") or {}).get("content"))
                if text and not info["last_prompt"]:
                    info["last_prompt"] = clean_prompt(text) or None
        elif kind == "assistant" and not entry.get("isSidechain"):
            message = entry.get("message") or {}
            if not info["context_tokens"]:
                info["context_tokens"] = context_tokens(message.get("usage"))
            text = _text_of(message.get("content"))
            if text and text.strip() and not info["last_assistant"]:
                info["last_assistant"] = text.strip()
        elif kind == "system" and entry.get("subtype") == "away_summary" and not away_summary_settled:
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                info["away_summary"] = _AWAY_SUMMARY_HINT.sub("", content.strip()).strip()
                info["away_summary_at"] = _iso(entry.get("timestamp"))
                away_summary_settled = True

    if not info["last_prompt"] and fallback_prompt:
        info["last_prompt"] = clean_prompt(fallback_prompt) or None
    return info


def conversation(path: str, max_bytes: int = CONVERSATION_TAIL_BYTES, limit: int = CONVERSATION_LIMIT) -> dict:
    """The session's main thread, oldest first: what you asked, what it said, the tools
    it ran, and Claude Code's recaps. Subagent lines and tool output are left out."""
    out = {"entries": [], "context_tokens": None, "model": None, "truncated": False}
    try:
        lines = _read_tail_lines(path, max_bytes)
        out["truncated"] = Path(path).stat().st_size > max_bytes
    except OSError:
        return out
    entries = out["entries"]
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind, at = entry.get("type"), _iso(entry.get("timestamp"))
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if kind == "user":
            origin = entry.get("origin")
            text = _text_of(message.get("content"))
            if isinstance(origin, dict) and origin.get("kind") == "human" and text:
                entries.append({"kind": "prompt", "text": _PASTED.sub(" [pasted] ", text).strip(), "at": at})
        elif kind == "assistant":
            out["context_tokens"] = context_tokens(message.get("usage")) or out["context_tokens"]
            model = message.get("model")
            if isinstance(model, str) and not model.startswith("<"):
                out["model"] = model
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    entries.append({"kind": "assistant", "text": block["text"].strip(), "at": at})
                elif block.get("type") == "tool_use":
                    entries.append({"kind": "tool", "text": describe_tool_call(block.get("name"), block.get("input")),
                                    "at": at})
        elif kind == "system" and entry.get("subtype") == "away_summary" and isinstance(entry.get("content"), str):
            entries.append({"kind": "recap", "text": _AWAY_SUMMARY_HINT.sub("", entry["content"].strip()).strip(),
                            "at": at})
    if len(entries) > limit:
        out["entries"], out["truncated"] = entries[-limit:], True
    return out


def usage_between(path: str | None, start: str, end: str) -> dict | None:
    """Tokens the session's assistant turns used between two ISO times: input counts
    everything sent the model, cached or not. A streamed message appears more than once
    and is counted at its last, fullest entry. None when there is no transcript or
    nothing in the window."""
    if not path:
        return None
    try:
        lo, hi = datetime.fromisoformat(start), datetime.fromisoformat(end)
        by_message: dict[str, tuple[int, int]] = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                at = _iso(entry.get("timestamp"))
                message = entry.get("message") or {}
                usage = message.get("usage")
                if not at or not isinstance(usage, dict) or not lo <= datetime.fromisoformat(at) <= hi:
                    continue
                sent = context_tokens(usage) or 0
                made = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else 0
                by_message[message.get("id") or f"line-{n}"] = (sent, made)
    except (OSError, ValueError):
        return None
    if not by_message:
        return None
    sent = sum(s for s, _ in by_message.values())
    made = sum(m for _, m in by_message.values())
    return {"input": sent, "output": made, "total": sent + made}


def as_text(entries: list[dict], max_chars: int = 40_000) -> str:
    """The conversation as plain text for a summary, newest kept when it's too long."""
    label = {"prompt": "User", "assistant": "Claude", "tool": "Tool", "recap": "Recap"}
    text = "\n\n".join(f"{label[e['kind']]}: {e['text']}" for e in entries)
    return text[-max_chars:]
