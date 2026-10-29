"""AI Decision Orchestrator.

Core HTTP API, a dashboard for the create -> list -> answer path, and a Linear poll
loop.

Answering writes the answer into the target repository as markdown (repo_writer) and
the work is picked up by whatever Claude Code session next opens that repo. The only
thing ever spawned from here is a `claude --bg` session the human clicked for
("Apply in background", "Start a session"), and never from a Linear-sourced answer
without that click.
"""
import asyncio
import hashlib
import json
import os
import re
import shlex
import signal
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.datastructures import UploadFile as StarletteUploadFile

from urllib.parse import quote, urlencode

from apo import changelog, claude_runtime, config, credentials, db, transcripts, verify
from apo.integrations import linear_client, sources

# Registry of credential-backed providers: provider name -> validator taking the
# raw submitted/stored field dict and returning {"display_name", ...} or raising.
# Adding a second provider is adding an entry here, not a new route.
PROVIDER_VALIDATORS = {
    "linear": lambda data: linear_client.validate_credentials(data.get("api_key", ""), data.get("team", "")),
}
# Each provider's module may cache resolved ids (team/project/workflow-state) across
# calls — those caches are only ever valid for whichever credential was active when
# populated, so a connect/disconnect/reconnect must invalidate them or a later push
# silently keeps using an id that belonged to the old credential.
PROVIDER_CACHE_CLEARERS = {
    "linear": linear_client.clear_caches,
}
# What a successful connect or reconnect does beyond storing the credential, given the
# validator's result. Linear compares workspaces: ids stored under another one are
# dropped (db.linear_connected). A disconnect or a test of the same credential doesn't
# run this, so disconnecting and reconnecting to the same workspace loses nothing.
PROVIDER_ON_CONNECT = {
    "linear": lambda conn, result: db.linear_connected(conn, result.get("workspace")),
}


def _clear_provider_caches(provider: str) -> None:
    clearer = PROVIDER_CACHE_CLEARERS.get(provider)
    if clearer:
        clearer()


def _connected(conn, provider: str, result: dict) -> None:
    _clear_provider_caches(provider)
    on_connect = PROVIDER_ON_CONNECT.get(provider)
    if on_connect:
        on_connect(conn, result)


_STATUS_LABELS = {
    "running": "Running", "idle": "Idle", "ended": "Ended", "crashed": "Crashed",
    "blocked_decision": "Needs decision", "blocked_action": "Needs action",
    "waiting_permission": "Needs permission", "waiting_input": "Needs your input",
    "errored": "API error", "usage_limit": "Usage limit",
}


def _humanize_delta(iso_ts: str | None) -> str:
    if not iso_ts:
        return "never"
    seconds = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds())
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


RECENTLY_ENDED_DAYS = 7
RECENTLY_ENDED_LIMIT = 12


_VERDICT_LABELS = {"done": "Done", "partial": "Partly done", "abandoned": "Abandoned", "crashed": "Crashed",
                   "unknown": "Outcome unknown"}


def _enrich_sessions(sessions: list[dict]) -> list[dict]:
    for s in sessions:
        s["last_seen_human"] = _humanize_delta(s["last_seen_at"])
        s["last_summary_human"] = _humanize_delta(s["last_summary_at"]) if s["last_summary_at"] else None
        s["status_label"] = _STATUS_LABELS[s["display_status"]]
        s["verdict_label"] = _VERDICT_LABELS.get(s.get("verdict"))
        s["stale_minutes"] = db.STALE_MINUTES
        s["context_label"] = _tokens(s.get("context_tokens"))
        s["resume_command"] = db.session_resume_command(s) if s["status"] == "ended" else None
        s["terminal_command"] = db.session_resume_command(s) or f"claude --resume {shlex.quote(s['claude_session_id'])}"
    return sessions


def _tokens(count: int | None) -> str | None:
    if not count:
        return None
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1000)}k" if count >= 1000 else str(count)


RECONCILE_SECONDS = 20
CHANGELOG_SECONDS = 60
CHANGES_DAYS = 7
ACTIVITY_COMMIT_DAYS = 30
WATCHED_WINDOW_SECONDS = 90
STREAM_CHECK_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0
# A backstop for _end_streams_on_shutdown: the browser reconnects on its own.
STREAM_MAX_SECONDS = 120.0
_stopping = False
# Last time a fleet page or its live-refresh poll was served. The live-session
# reconciler only runs while this is recent: nothing runs when nobody is looking.
_last_viewed_at = 0.0


def _mark_viewed() -> None:
    global _last_viewed_at
    _last_viewed_at = time.monotonic()


def reconcile_once() -> None:
    """Check the fleet against Claude Code's own live view of its processes. Local
    process inspection only — no LLM is ever woken (see db.reconcile_live_sessions)."""
    db.reconcile_live_sessions(db.get_connection(), claude_runtime.list_agents())


async def reconcile_loop() -> None:
    while True:
        await asyncio.sleep(RECONCILE_SECONDS)
        if _last_viewed_at and time.monotonic() - _last_viewed_at < WATCHED_WINDOW_SECONDS:
            try:
                await asyncio.to_thread(reconcile_once)
            except Exception:  # noqa: BLE001
                pass


def _start_background(cwd: str, prompt: str, resume: str | None = None) -> str | None:
    """Every background session Switchboard starts runs with its project's autonomy."""
    mode = db.permission_mode_for_path(db.get_connection(), cwd)
    return claude_runtime.start_background(cwd, prompt, resume, **({"permission_mode": mode} if mode else {}))


def run_scheduled_resumes() -> None:
    """Start any session whose scheduled resume is due (see db.schedule_resume)."""
    conn = db.get_connection()
    for row in db.due_resumes(conn):
        try:
            _start_background(row["cwd"], db.CONTINUE_AFTER_LIMIT_PROMPT, row["claude_session_id"])
        except claude_runtime.LaunchError as exc:
            db.resume_finished(conn, row["claude_session_id"], error=str(exc))
        else:
            db.resume_finished(conn, row["claude_session_id"])


async def scheduled_resume_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(run_scheduled_resumes)
        except Exception as exc:  # noqa: BLE001
            db.record_sync_error(db.get_connection(), "scheduled_resume", None, str(exc))


def refresh_changelogs() -> None:
    """Pull requests for every active project's folders, each at most every
    changelog.PR_REFRESH_SECONDS, so the Overview only ever reads a cache."""
    conn = db.get_connection()
    for row in conn.execute("SELECT repositories.local_path FROM repositories JOIN projects "
                            "ON projects.id = repositories.project_id WHERE projects.archived_at IS NULL"):
        changelog.refresh_pull_requests(row["local_path"])


async def changelog_loop() -> None:
    while True:
        await asyncio.sleep(CHANGELOG_SECONDS)
        if _last_viewed_at and time.monotonic() - _last_viewed_at < WATCHED_WINDOW_SECONDS:
            try:
                await asyncio.to_thread(refresh_changelogs)
            except Exception:  # noqa: BLE001
                pass


def _end_streams_on_shutdown() -> None:
    """Uvicorn waits for open responses before it exits, and a tab's event stream never
    finishes by itself, so a restart (every deploy, under --reload) would wait forever.
    Uvicorn's own SIGTERM/SIGINT handlers are wrapped so the streams end first."""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous):
            global _stopping
            _stopping = True
            previous(signum, frame)

        signal.signal(sig, handler)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _end_streams_on_shutdown()
    tasks = [asyncio.create_task(source_poll_loop()), asyncio.create_task(reconcile_loop()),
             asyncio.create_task(scheduled_resume_loop()), asyncio.create_task(changelog_loop())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Switchboard", lifespan=lifespan)
# Package data, found through the package rather than cwd, so an installed wheel
# serves the same files as a checkout.
templates = Jinja2Templates(directory=str(files("apo.web") / "templates"))
STATIC_DIR = Path(str(files("apo.web") / "static"))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_asset_versions: dict[str, tuple[float, str]] = {}


def _asset(name: str) -> str:
    """/static/<name>?v=<content hash>. A changed file gets a new URL, so a browser
    can't keep running a cached old script against new server code."""
    path = STATIC_DIR / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return f"/static/{name}"
    cached = _asset_versions.get(name)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.md5(path.read_bytes()).hexdigest()[:10])
        _asset_versions[name] = cached
    return f"/static/{name}?v={cached[1]}"


def _visible_sessions(conn, **kwargs) -> list[dict]:
    return [s for s in db.list_sessions(conn, **kwargs) if not s["ghost"]]


def _nav_badges() -> dict:
    conn = db.get_connection()
    open_decisions = sum(c["open"] for c in db.project_counts(conn).values() if not c["archived"])
    sessions_needing_you = sum(1 for s in _visible_sessions(conn) if s["needs_you"])
    return {"decisions": open_decisions, "fleet": sessions_needing_you, "now": _now_count(db.now_queue(conn))}


templates.env.globals["nav_badges"] = _nav_badges
templates.env.globals["asset"] = _asset
templates.env.globals["saved_views"] = lambda: db.list_saved_views(db.get_connection())
templates.env.globals["page_query"] = lambda request: urlencode(
    [(k, v) for k, v in request.query_params.multi_items() if k != "bulk"])
# A second dashboard run against a copy of the data (ADO_PREVIEW=1) says so on every page.
templates.env.globals["is_preview"] = config.is_preview
templates.env.filters["ago"] = _humanize_delta
templates.env.filters["tokens"] = _tokens


def _duration(minutes: float | None) -> str:
    minutes = round(minutes or 0)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)}h"
    return f"{round(minutes / 1440)}d"


templates.env.filters["duration"] = _duration


def _hours(minutes: float | None) -> str:
    """Summed waits (agent-hours) — in days they'd read as calendar time."""
    minutes = round(minutes or 0)
    return f"{minutes}m" if minutes < 60 else f"{round(minutes / 60)}h"


templates.env.filters["hours"] = _hours


def _highlight(text: str | None, terms: list[str]) -> Markup:
    text = text or ""
    words = sorted({t for t in terms if t}, key=len, reverse=True)
    if not words:
        return escape(text)
    out, last = [], 0
    for m in re.finditer("|".join(re.escape(w) for w in words), text, re.IGNORECASE):
        out.append(str(escape(text[last:m.start()])) + f"<mark>{escape(m.group(0))}</mark>")
        last = m.end()
    out.append(str(escape(text[last:])))
    return Markup("".join(out))


templates.env.filters["highlight"] = _highlight


def _code_spans(text: str | None) -> Markup:
    """`branch` reads as <code>branch</code>, everything else escaped."""
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", str(escape(text or ""))))


templates.env.filters["code_spans"] = _code_spans
templates.env.filters["work_location"] = db.work_location_phrase
templates.env.filters["clock"] = lambda iso_ts: datetime.fromisoformat(iso_ts).astimezone().strftime("%H:%M")
templates.env.filters["home"] = lambda path: str(path).replace(str(Path.home()), "~", 1)


def _when(iso_ts: str) -> str:
    """A clock time today, the weekday this week, the date before that."""
    at = datetime.fromisoformat(iso_ts).astimezone()
    days = (datetime.now().astimezone().date() - at.date()).days
    if days == 0:
        return at.strftime("%H:%M")
    return at.strftime("%a %H:%M") if days < 7 else f"{at:%b} {at.day}"


templates.env.filters["when"] = _when


def _due(due_on: str | None, today: date | None = None) -> dict | None:
    """How a due date reads, and how loud: overdue is danger, the next two days a warning."""
    if not due_on:
        return None
    today = today or datetime.now().astimezone().date()
    days = (date.fromisoformat(due_on) - today).days
    if days < 0:
        return {"label": f"Overdue {-days}d", "tone": "danger", "overdue": True}
    if days == 0:
        return {"label": "Due today", "tone": "warning", "overdue": False}
    if days == 1:
        return {"label": "Due tomorrow", "tone": "warning", "overdue": False}
    due = date.fromisoformat(due_on)
    label = f"Due {due:%a}" if days < 7 else f"Due {due:%b} {due.day}"
    return {"label": label, "tone": "info" if days < 7 else "neutral", "overdue": False}


templates.env.filters["due"] = _due


def _clock_day(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    moment = datetime.fromisoformat(iso_ts).astimezone()
    today = datetime.now().astimezone().date()
    day = "today" if moment.date() == today else "tomorrow" if (moment.date() - today).days == 1 else moment.strftime("%a %b %-d")
    return f"{day} {moment.strftime('%H:%M')}"


templates.env.filters["clock_day"] = _clock_day


async def poll_sources_once() -> None:
    """One pass over every configured, enabled AnswerSource. Recording the answer
    is all this does —
    db.answer_decision_request handles writing it into the repository — so the loop
    stays fast and never blocks on anything but the source's own network call.

    Also retries any previously-failed push or resolve first: that's the whole
    "queue" for a source that was down — the failed rows just sit there (error
    column set) until a cycle like this one catches up, and each success marks the
    earlier sync_errors it supersedes as resolved so the banner clears itself.

    Then brings in goals planned in the source (sync_goals: for Linear, new issues in
    projects that have it on, and their sub-issues and status)."""
    conn = db.get_connection()
    for src in sources.SOURCES:
        if not src.is_configured() or not db.is_connector_enabled(conn, src.name):
            continue
        try:
            await asyncio.to_thread(src.retry_pending)
        except Exception as exc:  # noqa: BLE001
            db.record_sync_error(db.get_connection(), f"{src.name}_retry", None, str(exc))
        try:
            await asyncio.to_thread(src.sync_goals)
        except Exception as exc:  # noqa: BLE001
            db.record_sync_error(db.get_connection(), f"{src.name}_goals", None, str(exc))
        try:
            new_answers = await asyncio.to_thread(src.poll_new_answers)
        except Exception as exc:  # noqa: BLE001
            db.record_sync_error(db.get_connection(), f"{src.name}_poll", None, str(exc))
            continue
        for new_answer in new_answers:
            conn = db.get_connection()
            db.answer_decision_request(
                conn, new_answer.decision_id, new_answer.answer_text, source=src.name
            )


async def source_poll_loop() -> None:
    while True:
        await asyncio.sleep(config.poll_seconds())
        await poll_sources_once()


def _now_count(queue: dict) -> int:
    return len(queue["problems"]) + len(queue["your_move"])


AWAY_AFTER_MINUTES = 30


def _away_digest(conn) -> dict | None:
    """'While you were away': visiting Now at least every AWAY_AFTER_MINUTES means
    you were watching. After a longer gap, a digest of everything since the previous
    visit opens and stays (across refreshes) until 'Mark seen'. A first visit only
    sets the marker — it doesn't dump the whole history."""
    latest = db.latest_event_seq(conn)
    now_ts = datetime.now(timezone.utc).isoformat()
    last_visit_at = db.get_ui_state(conn, "last_visit_at")
    last_visit_seq = db.get_ui_state(conn, "last_visit_seq")
    digest_from = db.get_ui_state(conn, "digest_from_seq")
    if last_visit_at and last_visit_seq is not None and digest_from is None:
        gap_minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(last_visit_at)).total_seconds() / 60
        if gap_minutes >= AWAY_AFTER_MINUTES and int(last_visit_seq) < latest:
            digest_from = last_visit_seq
            db.set_ui_state(conn, "digest_from_seq", digest_from)
            db.set_ui_state(conn, "digest_since_at", last_visit_at)
    db.set_ui_state(conn, "last_visit_at", now_ts)
    db.set_ui_state(conn, "last_visit_seq", str(latest))
    if digest_from is None:
        return None
    digest = db.away_digest(conn, int(digest_from))
    digest["since_at"] = db.get_ui_state(conn, "digest_since_at")
    return digest


def _project_filter(conn, project: str | None):
    row = db.find_project(conn, project) if project else None
    return (row["id"], row["name"]) if row else (None, None)


@app.get("/")
def now(request: Request, project: str | None = None):
    """Home: everything that needs the human, across every project, ranked."""
    _mark_viewed()
    conn = db.get_connection()
    project_id, project_name = _project_filter(conn, project)
    queue = db.now_queue(conn, project_id=project_id)
    return templates.TemplateResponse(
        request, "now.html",
        {"active_nav": "now", "q": queue, "count": _now_count(queue), "away": _away_digest(conn),
         "filter_project": project_name, "bulk_count": len(db.bulk_accept_candidates(conn, project_id=project_id)),
         "court": db.court_metrics(conn)},
    )


@app.post("/seen")
def mark_seen():
    conn = db.get_connection()
    conn.execute("DELETE FROM ui_state WHERE key IN ('digest_from_seq', 'digest_since_at')")
    conn.commit()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    return RedirectResponse("/", status_code=303)


def _item_json(item: dict) -> dict:
    keep = ("key", "kind", "project_name", "title", "reason", "since", "effort", "score", "resume_command",
            "due_on", "due_bucket", "stuck")
    return {k: item.get(k) for k in keep}


@app.get("/api/now")
def api_now(project: str | None = None):
    _mark_viewed()
    conn = db.get_connection()
    # now.js only polls from a visible tab: that's watching, so it counts as a visit.
    db.set_ui_state(conn, "last_visit_at", datetime.now(timezone.utc).isoformat())
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    queue = db.now_queue(conn, project_id=_project_filter(conn, project)[0])
    lanes = ("problems", "your_move", "asks", "in_transit", "review")
    return {
        **{lane: [_item_json(i) for i in queue[lane]] for lane in lanes},
        "quiet": queue["quiet"], "your_move_minutes": queue["your_move_minutes"], "count": _now_count(queue),
    }


TAB_SUFFIX = {"overview": "", "work": "/work", "decisions": "/decisions", "agents": "/agents", "activity": "/activity"}


def _rail_context(conn, tab: str, current: dict | None = None) -> dict:
    """The sidebar (every active project, plus the current one if it's archived) and
    where [ / ] go: All projects, then each project, round, on the same tab."""
    suffix = TAB_SUFFIX[tab]
    rail = db.project_overview(conn)
    if current is not None and not any(r["id"] == current["id"] for r in rail):
        rail.append(current)
    stops = [f"/projects{suffix}"] + [f"/projects/{r['id']}{suffix}" for r in rail]
    here = 0 if current is None else 1 + next(i for i, r in enumerate(rail) if r["id"] == current["id"])
    return {"rail": rail, "scope_prev": stops[here - 1], "scope_next": stops[(here + 1) % len(stops)]}


def _all_context(conn, tab: str, **extra) -> dict:
    """The All projects scope: the same shell, with no project picked."""
    work = db.project_work(conn, None)
    if extra.get("sort") == "due":
        work = db.sort_work_by_due(work)
    running = sum(1 for s in _visible_sessions(conn) if s["status"] != "ended")
    rail = _rail_context(conn, tab)
    count = len(rail["rail"])
    waiting = len(work["needs_you"])
    return {
        "active_nav": "projects", "tab": tab, "p": None, "work": work, "running": running,
        "all_summary": f"{count} project{'s' if count != 1 else ''} · {waiting} waiting on you · "
                       f"{running} agent{'s' if running != 1 else ''} running",
        "tab_counts": {"work": waiting + len(work["ready"]) + len(work["in_progress"]),
                       "decisions": sum(1 for i in work["needs_you"] if i["kind"] == "decision"), "agents": running},
        **rail, **extra,
    }


@app.get("/projects")
def projects_page(request: Request, archived: int = 0, new_project_error: str | None = None):
    _mark_viewed()
    conn = db.get_connection()
    overview = db.project_overview(conn, include_archived=bool(archived))
    live = [p for p in overview if not p["archived"]]
    root = config.get_projects_root()
    return templates.TemplateResponse(request, "all_overview.html", {
        **_all_context(conn, "overview"),
        "active": [p for p in live if p["health"] != "dormant"],
        "dormant": [p for p in live if p["health"] == "dormant"],
        "archived": [p for p in overview if p["archived"]],
        "dormant_days": db.DORMANT_DAYS,
        "outcomes": db.list_outcomes(conn),
        "finished": db.list_outcomes(conn, statuses=("achieved", "dropped")),
        "projects": [p["name"] for p in live],
        "projects_root": root, "folder_choices": db.project_folder_choices(conn, root),
        "new_project_error": new_project_error,
    })


@app.post("/projects")
def create_project_route(name: str = Form(""), folder: str = Form("")):
    conn = db.get_connection()
    try:
        project = db.create_project(conn, name, folder or None, config.get_projects_root())
    except ValueError as exc:
        return _with_param("/projects#new-project", "new_project_error", str(exc))
    return RedirectResponse(f"/projects/{project['id']}", status_code=303)


def _report_nav(end_day: date) -> dict:
    today = datetime.now().astimezone().date()
    following = end_day + timedelta(days=7)
    return {"prev_end": (end_day - timedelta(days=7)).isoformat(),
            "next_end": None if end_day >= today else ("today" if following >= today else following.isoformat())}


def _report_window(end: str | None) -> tuple[datetime, datetime, date]:
    """The 7 days ending with `end` (a local date, default today), midnight to midnight."""
    try:
        end_day = date.fromisoformat(end) if end else datetime.now().astimezone().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="end is a date, YYYY-MM-DD") from None
    stop = datetime.combine(end_day + timedelta(days=1), datetime.min.time()).astimezone()
    return stop - timedelta(days=7), stop, end_day


def _project_report(conn, project_id: int, start: datetime, stop: datetime) -> dict:
    report = db.weekly_report(conn, project_id, start, stop)
    folders = db.project_repositories(conn, project_id)
    changes = changelog.project_changes(folders, start.isoformat())
    report["commits"] = sum(1 for c in changes["commits"] if changelog.epoch(c["at"]) < stop.timestamp())
    report["prs_merged"] = sum(1 for p in changes["prs"] if p["state"] == "MERGED" and changelog.epoch(p["at"]) < stop.timestamp())
    report["status"] = db.project_status(conn, project_id)
    report["project"] = dict(_project_row(conn, project_id))
    report["markdown"] = _report_markdown(report, start, stop)
    return report


def _report_markdown(r: dict, start: datetime, stop: datetime) -> str:
    def section(title: str, rows: list[str]) -> list[str]:
        return [f"**{title} ({len(rows)})**", *[f"- {row}" for row in rows], ""] if rows else []

    last = stop - timedelta(days=1)
    lines = [f"## {r['project']['name']} — {start:%b} {start.day} to {last:%b} {last.day}",
             f"Status: {r['status']['value'].replace('_', ' ')} ({r['status']['note'] or r['status']['reason']})", ""]
    lines += section("Done", [f"#{i['id']} {i['title']}" for i in r["done"]])
    lines += section("Goals achieved", [g["title"] for g in r["goals_achieved"]])
    lines += section("Decided", [f"#{d['id']} {d['title']}: {db.plain_text(d['answer_text'] or '')[:200]}" for d in r["decided"]])
    lines += section("Blocked on you", [f"#{b['id']} {b['title']} (waiting {b['waited_hours']}h)" for b in r["blocked"]])
    lines += section("Overdue", [f"#{o['id']} {o['title']} (due {o['due_on']})" for o in r["overdue"]])
    median = r["median_answer_minutes"]
    lines.append(f"Agents waited on you {r['waited_hours']}h"
                 + (f" · median answer {median // 60}h {median % 60}m" if median is not None else "")
                 + f" · {r['filed']} filed, {len(r['done'])} done · last 4 weeks done: {' · '.join(map(str, r['trend']))}")
    lines.append(f"Code: {r['commits']} commit{'s' if r['commits'] != 1 else ''}"
                 + (f" · {r['prs_merged']} PR{'s' if r['prs_merged'] != 1 else ''} merged" if r["prs_merged"] else ""))
    return "\n".join(lines)


def _status_response(project_id: int | None, since: str, until: str | None, fmt: str, download: int):
    conn = db.get_connection()
    if project_id is not None:
        _project_row(conn, project_id)
    try:
        start = db.parse_since(since)
        stop = (datetime.combine(date.fromisoformat(until) + timedelta(days=1), datetime.min.time()).astimezone()
                if until else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    report = db.status_report(conn, project_id, start, stop)
    name = f"status-{project_id}" if project_id is not None else "status"
    headers = {"Content-Disposition": f'attachment; filename="{name}.{fmt}"'} if download else {}
    if fmt == "json":
        return JSONResponse(report, headers=headers)
    return Response(db.status_markdown(report), media_type="text/markdown; charset=utf-8", headers=headers)


@app.get("/projects/status.md")
def all_projects_status_md(since: str = "7d", until: str | None = None, download: int = 0):
    return _status_response(None, since, until, "md", download)


@app.get("/projects/status.json")
def all_projects_status_json(since: str = "7d", until: str | None = None, download: int = 0):
    return _status_response(None, since, until, "json", download)


@app.get("/projects/{project_id}/status.md")
def project_status_md(project_id: int, since: str = "7d", until: str | None = None, download: int = 0):
    return _status_response(project_id, since, until, "md", download)


@app.get("/projects/{project_id}/status.json")
def project_status_json(project_id: int, since: str = "7d", until: str | None = None, download: int = 0):
    return _status_response(project_id, since, until, "json", download)


@app.get("/projects/report")
def all_projects_report(request: Request, end: str | None = None):
    conn = db.get_connection()
    start, stop, end_day = _report_window(end)
    projects = conn.execute("SELECT id FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE").fetchall()
    reports = [_project_report(conn, row["id"], start, stop) for row in projects]
    return templates.TemplateResponse(request, "report.html", {
        "active_nav": "projects", "reports": reports, "start": start, "last": stop - timedelta(days=1), "end_day": end_day,
        "base": "/projects/report", "status_base": "/projects/status", "title": "All projects", "back": "/projects", **_report_nav(end_day),
        "markdown": "\n\n".join(r["markdown"] for r in reports),
    })


@app.get("/projects/week")
def week_page(request: Request, week: str = "this", start: str | None = None, end: str | None = None):
    """This week, last week, or a custom range (start and end are local dates, inclusive)."""
    today = datetime.now().astimezone().date()
    try:
        if start or end:
            first = date.fromisoformat(start) if start else date.fromisoformat(end) - timedelta(days=6)
            last = date.fromisoformat(end) if end else today
        else:
            last = today - timedelta(days=7) if week == "last" else today
            first = last - timedelta(days=6)
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end are dates, YYYY-MM-DD") from None
    if first > last:
        raise HTTPException(status_code=400, detail="start is after end")
    local = lambda d: datetime.combine(d, datetime.min.time()).astimezone()  # noqa: E731
    conn = db.get_connection()
    overview = db.week_overview(conn, local(first), local(last + timedelta(days=1)))
    return templates.TemplateResponse(request, "week.html", {
        "active_nav": "projects", "week": overview, "first": first, "last": last,
        "mode": "range" if start or end else ("last" if week == "last" else "this"),
    })


@app.get("/projects/{project_id}/report")
def project_report(request: Request, project_id: int, end: str | None = None):
    conn = db.get_connection()
    _project_row(conn, project_id)
    start, stop, end_day = _report_window(end)
    report = _project_report(conn, project_id, start, stop)
    return templates.TemplateResponse(request, "report.html", {
        "active_nav": "projects", "reports": [report], "start": start, "last": stop - timedelta(days=1),
        "end_day": end_day, "base": f"/projects/{project_id}/report", "status_base": f"/projects/{project_id}/status",
        "title": report["project"]["name"],
        **_report_nav(end_day),
        "back": f"/projects/{project_id}", "markdown": report["markdown"],
    })


def _filtered_work(conn, project_id: int | None, ctx: dict, label: str, owner: str, status: str) -> dict:
    """The Work tab narrowed by its filter bar; no params leaves it untouched."""
    label, owner, status = label.strip().lower(), owner.strip(), status.strip()
    ctx["filters"] = {"label": label, "owner": owner, "status": status, "active": bool(label or owner or status)}
    ctx["filter_labels"] = db.known_labels(conn, project_id)
    ctx["filter_statuses"] = db.WORK_STATUSES
    ctx["work"] = db.filter_work(ctx["work"], label, owner, status)
    return ctx


def _work_sort(sort: str) -> dict:
    return {"sort": "due" if sort == "due" else ""}


@app.get("/projects/work")
def all_work_page(request: Request, sort: str = "", label: str = "", owner: str = "", status: str = ""):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _filtered_work(conn, None, _all_context(conn, "work", **_work_sort(sort)), label, owner, status)
    db.add_token_usage(conn, ctx["work"]["done"])
    return templates.TemplateResponse(request, "all_work.html", {
        **ctx,
        "projects": conn.execute("SELECT id, name FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE").fetchall(),
        "destinations": _destinations(conn),
    })


def _destinations(conn) -> list[dict]:
    """Where new work can go from All projects: each project, and each of its folders
    when it has several (its default folder first)."""
    out = []
    for row in conn.execute("SELECT id, name FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE"):
        folders = db.project_repositories(conn, row["id"])
        if len(folders) < 2:
            out.append({"name": row["name"], "value": str(row["id"]), "folders": []})
            continue
        default = (db.default_repository(conn, row["id"]) or {}).get("id")
        folders.sort(key=lambda f: f["id"] != default)
        out.append({"name": row["name"], "value": str(row["id"]),
                    "folders": [{"value": f"{row['id']}:{f['id']}", **f} for f in folders]})
    return out


@app.post("/projects/items")
async def all_item_route(project: str = Form(...), title: str = Form(""), detail: str = Form(""),
                         owner: str = Form("agent"), labels: str = Form("")):
    """The project picker's value is "project" or "project:folder"."""
    project_part, _, repository = project.partition(":")
    if not project_part.isdigit():
        raise HTTPException(status_code=400, detail="Pick a project")
    project = int(project_part)
    response = await _add_work(project, "me" if owner == "me" else "task", title, repository, detail=detail,
                                labels=labels)
    if response.headers.get("location", "").startswith(f"/projects/{project}/work"):
        return RedirectResponse(response.headers["location"].replace(f"/projects/{project}/work", "/projects/work", 1),
                                status_code=303)
    return response


@app.get("/projects/decisions")
def all_decisions_page(request: Request):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _all_context(conn, "decisions")
    return templates.TemplateResponse(request, "all_decisions.html", {
        **ctx,
        "open_decisions": [i for i in ctx["work"]["needs_you"] if i["kind"] in ("decision", "action")],
        "log": db.decision_log(conn, None),
    })


@app.get("/projects/agents")
def all_agents_page(request: Request):
    _mark_viewed()
    conn = db.get_connection()
    return templates.TemplateResponse(request, "all_agents.html", {**_all_context(conn, "agents"), **_fleet_context(conn)})


@app.get("/projects/activity")
def all_activity_page(request: Request, all: int = 0):
    _mark_viewed()
    conn = db.get_connection()
    events = db.list_events(conn, min_importance=0 if all else 1, limit=300)
    by_day: dict[str, list] = {}
    for event in events:
        by_day.setdefault(_day_label(event["occurred_at"]), []).append(event)
    return templates.TemplateResponse(request, "all_activity.html", {
        **_all_context(conn, "activity"), "show_all": bool(all),
        "by_day": {day: db.collapse_repeats(day_events) for day, day_events in by_day.items()},
    })


def _project_row(conn, project_id: int):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _project_context(conn, project_id: int, tab: str, **extra) -> dict:
    """What every project tab shows above its content: the header, status and tab bar."""
    row = _project_row(conn, project_id)
    queue = db.now_queue(conn, project_id=project_id)
    overview = next(p for p in db.project_overview(conn, include_archived=True, queue=queue) if p["id"] == project_id)
    work = db.project_work(conn, project_id)
    if extra.get("sort") == "due":
        work = db.sort_work_by_due(work)
    running = sum(1 for s in _visible_sessions(conn) if s["project_id"] == project_id and s["status"] != "ended")
    return {
        **_rail_context(conn, tab, overview),
        "active_nav": "projects", "tab": tab, "p": overview, "queue": queue, "work": work, "running": running,
        "status": db.project_status(conn, project_id),
        "priority": db.project_priority(conn, project_id), "priorities": db.PROJECT_PRIORITIES,
        "autonomy": db.project_autonomy(conn, project_id), "autonomies": list(db.AUTONOMY_MODES),
        "repositories": db.project_repositories(conn, project_id),
        "folder_default": (db.default_repository(conn, project_id) or {}).get("id"),
        "tab_counts": {"work": len(work["needs_you"]) + len(work["ready"]) + len(work["in_progress"]),
                       "decisions": sum(1 for i in work["needs_you"] if i["kind"] == "decision"),
                       "agents": running},
        "others": [r["name"] for r in conn.execute(
            "SELECT name FROM projects WHERE id != ? AND archived_at IS NULL ORDER BY name COLLATE NOCASE", (row["id"],))],
        **extra,
    }


@app.get("/projects/{project_id}")
def project_page(request: Request, project_id: int, choose_linear: int = 0):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _project_context(conn, project_id, "overview")
    queue = ctx["queue"]
    return templates.TemplateResponse(request, "project_overview.html", {
        **ctx,
        "brief": db.get_project_brief(conn, project_id),
        "statuses": db.PROJECT_STATUSES,
        "items": [(lane, item) for lane in ("problems", "your_move", "asks", "in_transit", "review") for item in queue[lane]],
        "outcomes": db.list_outcomes(conn, project_id=project_id),
        "events": db.collapse_repeats(db.list_events(conn, project_id=project_id, limit=40))[:8],
        "linear_goals": _linear_goals_panel(conn, _project_row(conn, project_id), choose_linear),
        "changes": _project_changes(conn, project_id, ctx["repositories"]),
        "rules": db.list_standing_rules(conn, project_id),
        "rule_suggestion": db.rule_suggestion(conn, project_id),
    })


def _project_changes(conn, project_id: int, folders: list[dict]) -> dict:
    """The last CHANGES_DAYS of commits and pull requests, by day. A folder whose pull
    requests were never fetched gets fetched in the background for the next visit."""
    for folder in folders:
        if not changelog.has_pull_requests_cache(folder["local_path"]):
            threading.Thread(target=changelog.refresh_pull_requests, args=(folder["local_path"],), daemon=True).start()
    since = (datetime.now(timezone.utc) - timedelta(days=CHANGES_DAYS)).isoformat()
    changes = changelog.project_changes(folders, since, seen_at=db.project_look(conn, project_id))
    items = sorted([{**c, "kind": "commit"} for c in changes["commits"]] + [{**pr, "kind": "pr"} for pr in changes["prs"]],
                   key=lambda i: changelog.epoch(i["at"]), reverse=True)
    days: dict[str, list] = {}
    for item in items:
        days.setdefault(_day_label(item["at"]), []).append(item)
    changes["days"] = days
    changes["several_folders"] = len(folders) > 1
    return changes


def _linear_goals_panel(conn, project, choose: bool) -> dict:
    """The Linear project is the one picked, else the one with this project's name. The
    team's list is only fetched when you ask to choose, so the page never waits on Linear."""
    panel = {"on": project["linear_goals_since"] is not None, "connected": _linear_syncing(conn),
             "project_name": project["linear_project_name"] or project["name"],
             "choices": None, "selected": project["linear_project_id"], "error": None}
    if choose and panel["connected"]:
        try:
            panel["choices"] = linear_client.list_projects(linear_client.get_team_id())
        except Exception as exc:  # noqa: BLE001
            panel["error"] = str(exc)
        else:
            panel["selected"] = panel["selected"] or next(
                (lp["id"] for lp in panel["choices"] if lp["name"] == project["name"]), None)
    return panel


def _linear_syncing(conn) -> bool:
    return any(src.name == "linear" and src.is_configured() and db.is_connector_enabled(conn, src.name)
               for src in sources.SOURCES)


@app.post("/projects/{project_id}/linear-goals")
def project_linear_goals_route(project_id: int, enabled: str = Form(...)):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.set_linear_goals(conn, project_id, enabled == "1")
    return RedirectResponse(f"/projects/{project_id}#linear-goals", status_code=303)


@app.post("/projects/{project_id}/linear-project")
def project_linear_project_route(project_id: int, linear_project: str = Form(...)):
    """Checked against the team's own projects, so only one of those can be stored."""
    conn = db.get_connection()
    _project_row(conn, project_id)
    try:
        choices = linear_client.list_projects(linear_client.get_team_id())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach Linear: {exc}") from exc
    chosen = next((lp for lp in choices if lp["id"] == linear_project), None)
    if chosen is None:
        raise HTTPException(status_code=400, detail="That isn't one of your Linear team's projects.")
    db.set_linear_project(conn, project_id, chosen["id"], chosen["name"])
    return RedirectResponse(f"/projects/{project_id}#linear-goals", status_code=303)


@app.get("/projects/{project_id}/work")
def project_work_page(request: Request, project_id: int, sort: str = "", label: str = "", owner: str = "",
                      status: str = ""):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _filtered_work(conn, project_id, _project_context(conn, project_id, "work", **_work_sort(sort)),
                         label, owner, status)
    db.add_token_usage(conn, ctx["work"]["done"])
    return templates.TemplateResponse(request, "project_work.html", {
        **ctx, "severities": db.BUG_SEVERITIES,
        "outcomes": db.list_outcomes(conn, project_id=project_id),
    })


@app.get("/projects/{project_id}/decisions")
def project_decisions_page(request: Request, project_id: int):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _project_context(conn, project_id, "decisions")
    return templates.TemplateResponse(request, "project_decisions.html", {
        **ctx,
        "open_decisions": [i for i in ctx["work"]["needs_you"] if i["kind"] in ("decision", "action")],
        "log": db.decision_log(conn, project_id),
    })


@app.get("/projects/{project_id}/agents")
def project_agents_page(request: Request, project_id: int):
    _mark_viewed()
    conn = db.get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENTLY_ENDED_DAYS)).isoformat()
    mine = [s for s in _visible_sessions(conn, ended_since=cutoff) if s["project_id"] == project_id]
    sessions = _enrich_sessions([s for s in mine if s["status"] != "ended"])
    checklists = db.list_checklist_items_for_sessions(conn, [s["id"] for s in sessions])
    for s in sessions:
        s["checklist"] = checklists[s["id"]]
        s["checklist_done"] = sum(1 for i in s["checklist"] if i["status"] == "done")
    return templates.TemplateResponse(request, "project_agents.html", {
        **_project_context(conn, project_id, "agents"), "sessions": sessions,
        "ended": _enrich_sessions([s for s in mine if s["status"] == "ended"])[:RECENTLY_ENDED_LIMIT],
    })


@app.get("/projects/{project_id}/activity")
def project_activity_page(request: Request, project_id: int, all: int = 0):
    _mark_viewed()
    conn = db.get_connection()
    ctx = _project_context(conn, project_id, "activity")
    events = db.list_events(conn, project_id=project_id, min_importance=0 if all else 1, limit=300)
    since = events[-1]["occurred_at"] if len(events) >= 300 else \
        (datetime.now(timezone.utc) - timedelta(days=ACTIVITY_COMMIT_DAYS)).isoformat()
    commits = [{"occurred_at": c["at"], "summary": c["subject"], "verb": "git.commit", "actor": "git", "importance": 1,
                "commit": c}
               for c in changelog.project_changes(ctx["repositories"], since)["commits"]]
    by_day: dict[str, list] = {}
    for event in sorted([*events, *commits], key=lambda e: changelog.epoch(e["occurred_at"]), reverse=True):
        by_day.setdefault(_day_label(event["occurred_at"]), []).append(event)
    return templates.TemplateResponse(request, "project_activity.html", {
        **ctx, "show_all": bool(all),
        "by_day": {day: db.collapse_repeats(day_events) for day, day_events in by_day.items()},
    })


@app.post("/projects/{project_id}/brief")
def project_brief_route(project_id: int, goal: str = Form(""), links: str = Form("")):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.set_project_brief(conn, project_id, goal=goal, links=links)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/status")
def project_status_route(project_id: int, status: str = Form(""), note: str = Form(""), priority: str = Form("")):
    conn = db.get_connection()
    _project_row(conn, project_id)
    try:
        db.set_project_status(conn, project_id, status or None, note=note)
        if priority:
            db.set_project_priority(conn, project_id, priority)
    except ValueError as exc:
        return _with_param(f"/projects/{project_id}", "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _picked(raw: str | None) -> int | None:
    return int(raw) if raw and raw.strip().isdigit() else None


@app.post("/projects/{project_id}/items")
async def project_item_route(
    request: Request, project_id: int, kind: str = Form("task"), title: str = Form(""), detail: str = Form(""),
    expected: str = Form(""), steps: str = Form(""), severity: str = Form("normal"), outcome: str = Form(""),
    due_on: str = Form(""), run_now: str | None = Form(None), repository: str = Form(""), labels: str = Form(""),
):
    """New work: a task for an agent, a bug, or something for you. An agent's work can
    start in a background session right away, so every session started here is tracked."""
    # Not a File() parameter: with nothing picked, a browser still sends the field as a
    # part with an empty filename, which FastAPI reads as a string and refuses with a 422.
    form = await request.form()
    uploads = [(f.filename, f.content_type, await f.read()) for f in form.getlist("screenshots")
               if isinstance(f, StarletteUploadFile) and f.filename]
    return await _add_work(project_id, kind, title, repository, run_now == "1", detail=detail, expected=expected,
                           steps=steps, severity=severity, outcome_id=_picked(outcome), due_on=due_on,
                           labels=labels, uploads=uploads)


async def _add_work(project_id: int, kind: str, title: str, repository: str = "", run_now: bool = False,
                    uploads: list[tuple[str, str, bytes]] = (), **fields) -> RedirectResponse:
    conn = db.get_connection()
    _project_row(conn, project_id)
    back = f"/projects/{project_id}/work"
    try:
        db.validate_attachments(uploads)
        repository_id = db.repository_for_new_item(conn, project_id, _picked(repository))
        item_id = db.add_work(conn, project_id, kind, title, repository_id=repository_id, **fields)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    if uploads:
        db.add_attachments(conn, item_id, uploads)
    if run_now and kind != "me":
        error = await _dispatch(conn, item_id)
        if error:
            return _with_launch_error(f"/decisions/{item_id}", error)
    return RedirectResponse(back, status_code=303)


@app.post("/projects/{project_id}/pin")
def pin_project(project_id: int, pinned: str = Form("1"), next: str | None = Form(None)):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.set_project_pinned(conn, project_id, pinned == "1")
    return RedirectResponse(_safe_next(next, "/projects"), status_code=303)


@app.post("/projects/{project_id}/archive")
def archive_project_route(project_id: int):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.archive_project(conn, project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/unarchive")
def unarchive_project_route(project_id: int):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.unarchive_project(conn, project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/rename")
def rename_project_route(project_id: int, name: str = Form("")):
    conn = db.get_connection()
    _project_row(conn, project_id)
    try:
        db.rename_project(conn, project_id, name)
    except ValueError as exc:
        return _with_param(f"/projects/{project_id}", "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/merge")
def merge_project_route(project_id: int, into: str = Form(...)):
    conn = db.get_connection()
    _project_row(conn, project_id)
    target = db.find_project(conn, into)
    if target is None:
        raise HTTPException(status_code=400, detail=f"No project named {into!r}")
    try:
        db.merge_projects(conn, project_id, target["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/projects/{target['id']}", status_code=303)


@app.get("/api/stream")
async def api_stream(request: Request):
    """Server-sent "change" events whenever anything commits to the database. Hooks
    write from their own processes, so this is the only way a page can hear about it
    without polling. data_version changes on any other connection's commit, so the
    stream needs a connection of its own."""
    async def events():
        conn = db.get_connection()
        try:
            last = db.data_version(conn)
            quiet, ends_at = 0.0, time.monotonic() + STREAM_MAX_SECONDS
            yield "retry: 3000\n\n"
            while not _stopping and time.monotonic() < ends_at and not await request.is_disconnected():
                await asyncio.sleep(STREAM_CHECK_SECONDS)
                version = db.data_version(conn)
                if version != last:
                    last, quiet = version, 0.0
                    _mark_viewed()
                    yield "event: change\ndata: {}\n\n"
                else:
                    quiet += STREAM_CHECK_SECONDS
                    if quiet >= STREAM_HEARTBEAT_SECONDS:
                        quiet = 0.0
                        yield ": keepalive\n\n"
        finally:
            conn.close()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/notify")
def api_notify():
    """Polled by static/notify.js, also from a hidden tab — so it deliberately
    doesn't count as viewing (no live-session reconcile): hooks keep state current."""
    return db.notifications_due(db.get_connection())


GUIDE_PATH = Path(str(files("apo") / "resources" / "guide.md"))


@app.get("/guide")
def guide_page(request: Request):
    import markdown

    md = markdown.Markdown(extensions=["tables", "toc", "sane_lists"])
    html = md.convert(GUIDE_PATH.read_text())
    top = md.toc_tokens[0]["children"] if md.toc_tokens else []
    return templates.TemplateResponse(request, "guide.html", {
        "active_nav": None,
        "content": Markup(html),  # our own file, not user input
        "sections": [{"id": t["id"], "name": t["name"]} for t in top],
    })


@app.get("/round")
def lightning_round(request: Request, minutes: float | None = None, keys: str | None = None, skip: str = ""):
    """One move at a time. The plan is fixed when the round starts (it lives in the
    URL), so answering doesn't pull new work into a time box you already chose."""
    conn = db.get_connection()
    queue = db.now_queue(conn)
    if keys is None:
        plan = db.time_boxed_plan(queue, minutes)
        if not plan:
            return RedirectResponse("/", status_code=303)
        return RedirectResponse(f"/round?keys={','.join(i['key'] for i in plan)}", status_code=303)
    planned = [k for k in keys.split(",") if k]
    skipped = [k for k in skip.split(",") if k]
    live = {i["key"]: i for i in queue["your_move"]}
    remaining = [k for k in planned if k in live and k not in skipped]
    here = f"/round?keys={keys}" + (f"&skip={skip}" if skip else "")
    current = live[remaining[0]] if remaining else None
    return templates.TemplateResponse(request, "round.html", {
        "active_nav": "now",
        "item": current,
        "here": here,
        "position": len(planned) - len(remaining) + 1,
        "total": len(planned),
        "handled": len([k for k in planned if k not in live]),
        "minutes_left": sum(live[k].get("effort") or 0 for k in remaining),
        "skip_url": f"/round?keys={keys}&skip={','.join(skipped + [remaining[0]])}" if remaining else None,
    })


@app.get("/accept")
def bulk_accept_page(request: Request):
    conn = db.get_connection()
    return templates.TemplateResponse(request, "accept.html", {
        "active_nav": "now", "candidates": db.bulk_accept_candidates(conn),
        "min_confidence": db.BULK_ACCEPT_MIN_CONFIDENCE,
    })


@app.post("/accept")
def bulk_accept_route(ids: list[int] = Form([])):
    db.bulk_accept(db.get_connection(), ids)
    return RedirectResponse("/", status_code=303)


@app.get("/search")
async def search_page(request: Request, q: str = "", deep: int = 0, status: str = "", project: str = "",
                      owner: str = "", since: str = "", until: str = "", label: str = ""):
    conn = db.get_connection()
    filters = {"status": status.strip(), "project": project.strip(), "owner": owner.strip(),
               "since": since.strip(), "until": until.strip(), "label": label.strip().lower()}
    filtered = any(filters.values())
    error = None
    hits = []
    if q.strip() or filtered:
        try:
            hits = db.search_filtered(conn, q, **{k: v or None for k, v in filters.items()})
        except ValueError as exc:
            error = str(exc)
    if deep and q.strip() and not filtered:
        hits += await asyncio.to_thread(lambda: db.search_transcripts(db.get_connection(), q))
    groups: dict[str, list] = {}
    for hit in hits:
        groups.setdefault(hit["kind"], []).append(hit)
    return templates.TemplateResponse(request, "search.html", {
        "active_nav": "search", "q": q, "deep": bool(deep), "groups": groups, "terms": db._search_terms(q),
        "filters": filters, "filtered": filtered, "error": error,
        "filter_labels": db.known_labels(conn),
        "project_names": [r["name"] for r in conn.execute(
            "SELECT name FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE")],
    })


@app.post("/bulk")
def bulk_route(action: str = Form(...), ids: list[int] = Form([]), preset: str = Form(""), owner: str = Form(""),
               due_on: str = Form(""), next: str | None = Form(None)):
    try:
        result = db.bulk_apply(db.get_connection(), action, ids, preset=preset or None, owner=owner or None,
                               due_on=due_on or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _with_param(_safe_next(next, "/projects/work"), "bulk", result["summary"])


@app.post("/views")
def save_view_route(name: str = Form(...), path: str = Form(...), query: str = Form("")):
    try:
        db.save_view(db.get_connection(), name, path, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    query = query.lstrip("?")
    return RedirectResponse(path + (f"?{query}" if query else ""), status_code=303)


@app.post("/views/{view_id}/delete")
def delete_view_route(view_id: int, next: str | None = Form(None)):
    db.delete_saved_view(db.get_connection(), view_id)
    return RedirectResponse(_safe_next(next, "/projects/work"), status_code=303)


@app.get("/outcomes")
def outcomes_moved():
    return RedirectResponse("/projects")


@app.post("/outcomes")
def create_outcome_route(project: str = Form(...), title: str = Form(...), done_when: str = Form(""),
                         due_on: str = Form(""), next: str | None = Form(None)):
    conn = db.get_connection()
    project_row = db.find_project(conn, project)
    if project_row is None:
        raise HTTPException(status_code=400, detail=f"No project called {project}")
    try:
        outcome_id = db.create_outcome(conn, project_row["id"], title.strip(), done_when=done_when.strip() or None,
                                       due_on=due_on)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(_safe_next(next, f"/outcomes/{outcome_id}"), status_code=303)


@app.get("/outcomes/{outcome_id}")
def outcome_page(request: Request, outcome_id: int):
    conn = db.get_connection()
    outcome = db.get_outcome(conn, outcome_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="Outcome not found")
    progress = db.outcome_progress(conn, outcome_id)
    for step in progress["steps"]:
        step["work_location"] = db.current_work_location(conn, step)
    sessions = [s for s in _visible_sessions(conn, include_ended=True) if s.get("outcome_id") == outcome_id]
    return templates.TemplateResponse(request, "outcome.html", {
        "active_nav": "outcomes", "outcome": outcome, "progress": progress, "sessions": sessions,
        "trend": db.outcome_trend(conn, outcome_id),
        "titles": {s["id"]: s["title"] for s in progress["steps"]},
        "folders": db.project_repositories(conn, outcome["project_id"]),
        "folder_default": (db.default_repository(conn, outcome["project_id"]) or {}).get("id"),
    })


@app.post("/outcomes/{outcome_id}/steps")
def add_step_route(
    outcome_id: int, title: str = Form(...), owner: str = Form(...), detail: str = Form(""),
    waiting_on: str = Form(""), blocked_by: str = Form(""), repository: str = Form(""),
):
    conn = db.get_connection()
    outcome = db.get_outcome(conn, outcome_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="Outcome not found")
    try:
        db.add_step(
            conn, outcome_id, title.strip(), owner=owner, detail=detail.strip() or None,
            waiting_on=waiting_on.strip() or None, blocked_by=[int(blocked_by)] if blocked_by.strip() else None,
            repository_id=db.repository_for_new_item(conn, outcome["project_id"], _picked(repository)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/outcomes/{outcome_id}", status_code=303)


@app.post("/outcomes/{outcome_id}/due")
def outcome_due_route(outcome_id: int, due_on: str = Form("")):
    conn = db.get_connection()
    if db.get_outcome(conn, outcome_id) is None:
        raise HTTPException(status_code=404, detail="Outcome not found")
    back = f"/outcomes/{outcome_id}"
    try:
        db.set_outcome_due(conn, outcome_id, due_on)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


@app.post("/decisions/{decision_id}/due")
def item_due_route(decision_id: int, due_on: str = Form("")):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    back = f"/decisions/{decision_id}"
    try:
        db.set_due(conn, decision_id, due_on)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


def _known_item(conn, item_id: int) -> None:
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")


@app.post("/decisions/{decision_id}/notes")
def item_note_route(decision_id: int, body: str = Form("")):
    conn = db.get_connection()
    _known_item(conn, decision_id)
    try:
        db.add_note(conn, decision_id, body, author="human")
    except ValueError as exc:
        return _with_param(f"/decisions/{decision_id}#notes", "note_error", str(exc))
    return RedirectResponse(f"/decisions/{decision_id}#notes", status_code=303)


@app.post("/decisions/{decision_id}/labels")
def item_label_add_route(decision_id: int, label: str = Form("")):
    conn = db.get_connection()
    _known_item(conn, decision_id)
    try:
        for each in db.parse_labels(label):
            db.add_label(conn, decision_id, each)
    except ValueError as exc:
        return _with_param(f"/decisions/{decision_id}#labels", "note_error", str(exc))
    return RedirectResponse(f"/decisions/{decision_id}#labels", status_code=303)


@app.post("/decisions/{decision_id}/labels/remove")
def item_label_remove_route(decision_id: int, label: str = Form("")):
    conn = db.get_connection()
    _known_item(conn, decision_id)
    try:
        db.remove_label(conn, decision_id, label)
    except ValueError as exc:
        return _with_param(f"/decisions/{decision_id}#labels", "note_error", str(exc))
    return RedirectResponse(f"/decisions/{decision_id}#labels", status_code=303)


@app.post("/projects/{project_id}/autonomy")
def project_autonomy_route(project_id: int, autonomy: str = Form("ask")):
    conn = db.get_connection()
    _project_row(conn, project_id)
    try:
        db.set_project_autonomy(conn, project_id, autonomy)
    except ValueError as exc:
        return _with_param(f"/projects/{project_id}", "error", str(exc))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/rules")
def add_rule_route(project_id: int, text: str = Form(""), source_decision_id: str = Form(""),
                   next: str | None = Form(None)):
    conn = db.get_connection()
    _project_row(conn, project_id)
    back = _safe_next(next, f"/projects/{project_id}")
    try:
        db.add_standing_rule(conn, project_id, text, source_decision_id=_picked(source_decision_id))
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


@app.post("/projects/{project_id}/rules/{rule_id}/retire")
def retire_rule_route(project_id: int, rule_id: int):
    conn = db.get_connection()
    _project_row(conn, project_id)
    db.retire_standing_rule(conn, rule_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/outcomes/{outcome_id}/status")
def outcome_status_route(outcome_id: int, status: str = Form(...), next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_outcome(conn, outcome_id) is None:
        raise HTTPException(status_code=404, detail="Outcome not found")
    try:
        db.set_outcome_status(conn, outcome_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(_safe_next(next, f"/outcomes/{outcome_id}"), status_code=303)


def _item_redirect(conn, item_id: int, next: str | None) -> RedirectResponse:
    item = db.get_decision_request(conn, item_id)
    default = f"/outcomes/{item['outcome_id']}" if item and item.get("outcome_id") else f"/decisions/{item_id}"
    return RedirectResponse(_safe_next(next, default), status_code=303)


def _with_undo(response: RedirectResponse, item_id: int, label: str) -> RedirectResponse:
    """A short-lived cookie the next page turns into an Undo toast (static/app.js)."""
    response.set_cookie("ado_undo", quote(json.dumps({"id": item_id, "label": label})), max_age=60, path="/",
                        samesite="lax")
    return response


@app.post("/items/{item_id}/done")
def item_done(item_id: int, note: str = Form(""), next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.snapshot_for_undo(conn, item_id, "done")
    if not db.complete_step(conn, item_id, by="human", note=note.strip() or None):
        db.clear_undo(conn, item_id)
        return _item_redirect(conn, item_id, next)
    return _with_undo(_item_redirect(conn, item_id, next), item_id, "Marked done")


@app.post("/items/{item_id}/undo")
def item_undo(item_id: int, next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.undo_last(conn, item_id)
    return _item_redirect(conn, item_id, next)


@app.post("/items/{item_id}/reopen")
def item_reopen(item_id: int, next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.reopen_item(conn, item_id)
    return _item_redirect(conn, item_id, next)


@app.post("/decisions/{decision_id}/edit")
def edit_decision(decision_id: int, title: str = Form(...), description: str = Form(""), details: str = Form(""),
                  next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    back = _safe_next(next, f"/decisions/{decision_id}")
    try:
        # A blank details box leaves the existing details alone rather than wiping them.
        db.edit_item(conn, decision_id, title=title, description=description, details=details.strip() or None)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


@app.post("/items/{item_id}/assign")
def item_assign(item_id: int, owner: str = Form(...), next: str | None = Form(None)):
    conn = db.get_connection()
    try:
        db.reassign_step(conn, item_id, owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _item_redirect(conn, item_id, next)


@app.post("/items/{item_id}/retry")
def item_retry(item_id: int, note: str = Form(""), next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.retry_task(conn, item_id, note=note.strip() or None, by="human")
    return RedirectResponse(_safe_next(next, f"/decisions/{item_id}"), status_code=303)


@app.post("/items/{item_id}/rank")
def item_rank(item_id: int, direction: str = Form(...), next: str | None = Form(None)):
    conn = db.get_connection()
    if db.get_decision_request(conn, item_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    back = _safe_next(next, f"/decisions/{item_id}")
    try:
        db.move_rank(conn, item_id, direction)
    except ValueError as exc:
        if direction not in ("up", "down", "top"):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back.partition("#")[0] + f"#item-{item_id}", status_code=303)


@app.post("/decisions/{decision_id}/blockers")
def item_add_blocker(decision_id: int, blocker: int = Form(...)):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    back = f"/decisions/{decision_id}#dependencies"
    try:
        db.link_blocker(conn, decision_id, blocker)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


@app.post("/decisions/{decision_id}/blockers/{blocker_id}/remove")
def item_remove_blocker(decision_id: int, blocker_id: int):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    db.remove_dependency(conn, decision_id, blocker_id)
    return RedirectResponse(f"/decisions/{decision_id}#dependencies", status_code=303)


@app.post("/items/{item_id}/cancel")
def item_cancel(item_id: int, reason: str = Form("Cancelled by the user"), next: str | None = Form(None)):
    conn = db.get_connection()
    db.snapshot_for_undo(conn, item_id, "cancel")
    if not db.cancel_request(conn, item_id, reason.strip() or "Cancelled by the user"):
        db.clear_undo(conn, item_id)
        return _item_redirect(conn, item_id, next)
    return _with_undo(_item_redirect(conn, item_id, next), item_id, "Cancelled")


@app.get("/api/palette")
def api_palette():
    """Everything ⌘K can jump to or act on (static/palette.js)."""
    conn = db.get_connection()
    pages = [
        {"label": "Now", "href": "/"}, {"label": "Projects", "href": "/projects"},
        {"label": "Goals", "href": "/projects#goals"}, {"label": "Work", "href": "/projects/work"},
        {"label": "Agents", "href": "/projects/agents"}, {"label": "Decisions", "href": "/projects/decisions"},
        {"label": "Activity", "href": "/projects/activity"}, {"label": "This week", "href": "/projects/week"},
        {"label": "Settings", "href": "/settings"}, {"label": "Search", "href": "/search"},
        {"label": "Guide", "href": "/guide"},
    ]
    projects = [
        {"id": r["id"], "name": r["name"]} for r in conn.execute(
            "SELECT id, name FROM projects WHERE archived_at IS NULL ORDER BY pinned_at IS NULL, name COLLATE NOCASE"
        )
    ]
    requests = []
    for row in conn.execute(
        "SELECT decision_requests.id FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id "
        "WHERE decision_requests.status = 'open' AND projects.archived_at IS NULL ORDER BY decision_requests.id"
    ).fetchall():
        d = db.get_decision_request(conn, row["id"])
        requests.append({
            "id": d["id"], "title": d["title"], "kind": d["kind"], "project": db.get_project_name(conn, d["project_id"]),
            "options": [o["label"] for o in d["options"] or []], "recommendation": d["recommendation"],
        })
    sessions = [
        {"title": db._session_name(s), "project": s["project_name"], "status": _STATUS_LABELS.get(s["display_status"], ""),
         "href": f"/projects/agents#session-card-{s['id']}"}
        for s in _visible_sessions(conn)
    ]
    views = [{"id": v["id"], "name": v["name"], "href": v["href"]} for v in db.list_saved_views(conn)]
    return {"pages": pages, "projects": projects, "requests": requests, "sessions": sessions, "views": views}


@app.post("/decisions/{decision_id}/snooze")
def snooze_decision(decision_id: int, preset: str = Form(...), next: str | None = Form(None)):
    conn = db.get_connection()
    if not db.get_decision_request(conn, decision_id):
        raise HTTPException(status_code=404, detail="Decision not found")
    undoable = False
    if preset == "clear":
        db.snooze_request(conn, decision_id)
    elif preset in db.SNOOZE_PRESETS:
        db.snapshot_for_undo(conn, decision_id, "snooze")
        condition = "until_blocks" if preset == "until_blocks" else None
        db.snooze_request(conn, decision_id, until=db.snooze_preset_until(preset), condition=condition)
        undoable = True
    else:
        raise HTTPException(status_code=400, detail=f"Unknown snooze: {preset}")
    response = RedirectResponse(_safe_next(next, f"/decisions/{decision_id}"), status_code=303)
    return _with_undo(response, decision_id, "Snoozed") if undoable else response


@app.post("/sessions/{session_id}/reviewed")
def mark_reviewed(session_id: int, next: str | None = Form(None)):
    conn = db.get_connection()
    row = conn.execute("SELECT claude_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    db.mark_session_reviewed(conn, row["claude_session_id"])
    return RedirectResponse(_safe_next(next, "/"), status_code=303)


def _with_param(path: str, name: str, value: str) -> RedirectResponse:
    """Back to where you were, with a message the page shows — never a raw error page."""
    base, _, anchor = path.partition("#")
    joined = f"{base}{'&' if '?' in base else '?'}{name}={quote(value)}"
    return RedirectResponse(joined + (f"#{anchor}" if anchor else ""), status_code=303)


def _with_launch_error(path: str, error: str) -> RedirectResponse:
    return _with_param(path, "launch_error", error)


@app.post("/decisions/{decision_id}/apply")
async def apply_in_background(decision_id: int, approved: str | None = Form(None), next: str | None = Form(None)):
    conn = db.get_connection()
    back = _safe_next(next, f"/decisions/{decision_id}")
    plan = db.apply_plan(conn, decision_id)
    if plan["error"]:
        return _with_launch_error(back, plan["error"])
    if plan["needs_approval"] and approved != "1":
        raise HTTPException(status_code=409, detail="This answer came from Linear — approve it on the dashboard first.")
    try:
        agent_id = await asyncio.to_thread(_start_background, plan["cwd"], plan["prompt"], plan["resume"])
    except claude_runtime.LaunchError as exc:
        return _with_launch_error(back, str(exc))
    origin = (db.get_decision_request(conn, decision_id) or {}).get("origin_session_id")
    db.record_dispatch(conn, decision_id, agent_id, resumed=bool(plan["resume"]), parent_session_id=origin)
    return RedirectResponse(back, status_code=303)


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, next: str | None = Form(None)):
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["agent_kind"] != "background" or not row["agent_id"]:
        raise HTTPException(status_code=409, detail="Only a background session can be stopped from here — this one runs in a terminal.")
    back = _safe_next(next, "/projects/agents")
    try:
        await asyncio.to_thread(claude_runtime.stop_background, row["agent_id"])
    except claude_runtime.LaunchError as exc:
        return _with_launch_error(back, str(exc))
    db.record_event(conn, verb="session.stopped", actor="human", source="dashboard", project_id=row["project_id"],
                    session_id=row["claude_session_id"], summary=f"Stopped background session {row['agent_id']}")
    return RedirectResponse(back, status_code=303)


@app.post("/sessions/{session_id}/resume")
async def resume_session(session_id: int, next: str | None = Form(None), at: str | None = Form(None),
                         now: str | None = Form(None), cancel: str | None = Form(None)):
    """A session closed while it waited for the usage limit: Claude Code's own
    auto-continue can't reach it, so resume it in the background with the same
    message auto-continue would have sent."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["status"] != "ended":
        raise HTTPException(status_code=409, detail="It's still open — Claude Code continues it there, or press Enter in its terminal.")
    back = _safe_next(next, "/")
    if cancel == "1":
        db.schedule_resume(conn, row["claude_session_id"], None)
        return RedirectResponse(back, status_code=303)
    if at and now != "1":
        try:
            hour, minute = (int(x) for x in at.split(":")[:2])
            local = datetime.now().astimezone()
            when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return _with_param(back, "error", f"Not a time: {at}")
        if when <= local:
            when += timedelta(days=1)
        db.schedule_resume(conn, row["claude_session_id"], when)
        return RedirectResponse(back, status_code=303)
    try:
        agent_id = await asyncio.to_thread(_start_background, row["cwd"], db.CONTINUE_AFTER_LIMIT_PROMPT,
                                           row["claude_session_id"])
    except claude_runtime.LaunchError as exc:
        return _with_launch_error(back, str(exc))
    conn.execute("UPDATE sessions SET last_error = NULL WHERE id = ?", (session_id,))
    conn.commit()
    db.record_event(conn, verb="session.launched", actor="human", source="dashboard", project_id=row["project_id"],
                    session_id=row["claude_session_id"],
                    summary=f"Resumed in the background after the usage limit{f' ({agent_id})' if agent_id else ''}")
    return RedirectResponse(back, status_code=303)


async def _dispatch(conn, task_id: int, parent_session_id: str | None = None) -> str | None:
    """Hand a READY task to a new background session in its folder. The error, if it couldn't start."""
    task = db.get_decision_request(conn, task_id)
    repository = (db.get_repository(conn, task["repository_id"]) if task["repository_id"]
                  else db.get_repository_for_project(conn, task["project_id"]))
    if repository is None:
        return "Choose a folder for it first."
    try:
        agent_id = await asyncio.to_thread(_start_background, repository["local_path"], db.dispatch_prompt(task_id))
    except claude_runtime.LaunchError as exc:
        return str(exc)
    db.record_dispatch(conn, task_id, agent_id, resumed=False, parent_session_id=parent_session_id)
    return None


def _safe_next(target: str | None, default: str) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else default


def _fleet_context(conn) -> dict:
    """Every session, grouped by project, for the Agents tab."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENTLY_ENDED_DAYS)).isoformat()
    all_sessions = _enrich_sessions(_visible_sessions(conn, ended_since=cutoff))
    sessions = [s for s in all_sessions if s["status"] != "ended"]
    recently_ended = [s for s in all_sessions if s["status"] == "ended"][:RECENTLY_ENDED_LIMIT]
    checklists = db.list_checklist_items_for_sessions(conn, [s["id"] for s in sessions + recently_ended])
    for s in sessions + recently_ended:
        items = checklists[s["id"]]
        s["checklist"] = items
        s["checklist_done"] = sum(1 for i in items if i["status"] == "done")

    sessions_by_project: dict[str, list] = {}
    unregistered_sessions: list = []
    for s in sorted(sessions, key=lambda s: (s["project_name"] or "", s["last_seen_at"]), reverse=True):
        if s["project_name"]:
            sessions_by_project.setdefault(s["project_name"], []).append(s)
        else:
            unregistered_sessions.append(s)
    # Restore ascending project-name order (the reverse sort above was only to get
    # last_seen_at descending within each group) and put sessions waiting on the
    # human first within each project, mirroring INDEX.md's actionable-rows-first
    # convention.
    sessions_by_project = {
        name: sorted(group, key=lambda s: 0 if s["needs_you"] else 1)
        for name, group in sorted(sessions_by_project.items(), key=lambda kv: kv[0].lower())
    }

    return {
        "open_counts": {name: c["open"] for name, c in db.project_counts(conn).items()},
        "project_ids": {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM projects")},
        "sessions_by_project": sessions_by_project,
        "unregistered_sessions": unregistered_sessions,
        "recently_ended": recently_ended,
        "recently_ended_days": RECENTLY_ENDED_DAYS,
        "stale_minutes": db.STALE_MINUTES,
    }


@app.get("/sessions")
def sessions_moved():
    return RedirectResponse("/projects/agents")


@app.get("/api/sessions")
def api_list_sessions(project: str | None = None):
    _mark_viewed()
    conn = db.get_connection()
    sessions = _enrich_sessions(_visible_sessions(conn))
    if project:
        sessions = [s for s in sessions if s["project_name"] == project]
    return sessions


def _session_back(conn, session_id: int, next: str | None) -> RedirectResponse:
    """Back to the page the card was on: the form swaps the card in from there."""
    row = conn.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    default = f"/projects/{row['project_id']}/agents" if row and row["project_id"] else "/projects/agents"
    return RedirectResponse(_safe_next(next, default), status_code=303)


def _session(conn, session_id: int) -> dict:
    session = next((s for s in db.list_sessions(conn, include_ended=True) if s["id"] == session_id), None)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _enrich_sessions([session])[0]


def _conversation(session: dict) -> dict | None:
    """The transcript's entries, with each run of tool calls folded into one block."""
    path = transcripts.find(session["claude_session_id"], claude_runtime.claude_config_dirs())
    if not path:
        return None
    conversation = transcripts.conversation(str(path))
    blocks: list[dict] = []
    for entry in conversation["entries"]:
        if entry["kind"] == "tool":
            if not blocks or blocks[-1]["kind"] != "tools":
                blocks.append({"kind": "tools", "items": []})
            blocks[-1]["items"].append(entry)
        else:
            blocks.append(entry)
    conversation["blocks"] = blocks
    return conversation


@app.get("/sessions/{session_id}")
def session_page(request: Request, session_id: int):
    _mark_viewed()
    conn = db.get_connection()
    session = _session(conn, session_id)
    session["checklist"] = db.list_checklist_items_for_sessions(conn, [session_id])[session_id]
    session["checklist_done"] = sum(1 for i in session["checklist"] if i["status"] == "done")
    return templates.TemplateResponse(request, "session.html", {
        "active_nav": "projects", "s": session, "conversation": _conversation(session),
        "plan": db.continue_plan(session),
    })


@app.post("/sessions/{session_id}/recap")
async def session_recap_route(session_id: int):
    """Claude Code writes its own recap only when you come back to a session in the
    terminal; this asks a small model for one from the transcript instead."""
    conn = db.get_connection()
    session = _session(conn, session_id)
    back = f"/sessions/{session_id}"
    conversation = _conversation(session)
    if not conversation or not conversation["entries"]:
        return _with_param(back, "error", "There's no transcript to make a recap from.")
    try:
        recap = await asyncio.to_thread(claude_runtime.summarize, transcripts.as_text(conversation["entries"]))
    except claude_runtime.LaunchError as exc:
        return _with_param(back, "error", f"Couldn't make a recap: {exc}")
    db.record_session_summary(conn, session["claude_session_id"], recap)
    return RedirectResponse(back, status_code=303)


@app.post("/sessions/{session_id}/prompt")
async def answer_prompt_route(request: Request, session_id: int):
    """An answer to the prompt a background session's hook is holding: allow or deny
    (a permission or a plan), the chosen options (a question), or back to the terminal."""
    form = await request.form()
    conn = db.get_connection()
    session = _session(conn, session_id)
    back = _safe_next(form.get("next"), f"/sessions/{session_id}")
    prompt = session["prompt"]
    if not prompt or prompt["id"] != form.get("prompt_id"):
        return _with_param(back, "error", "That prompt isn't waiting any more: it was answered, or went back to the terminal.")
    choice = form.get("choice")
    if choice in ("allow", "terminal"):
        answer = {"behavior": choice}
    elif choice == "deny":
        message = (form.get("message") or "").strip()
        answer = {"behavior": "deny", **({"message": message} if message else {})}
    elif choice == "answer" and prompt["kind"] == "question":
        answers = {}
        for i, question in enumerate(prompt.get("questions") or []):
            own = (form.get(f"other-{i}") or "").strip()
            value = own or ", ".join(v for v in form.getlist(f"answer-{i}") if v)
            if not value:
                return _with_param(back, "error", "Answer every question first.")
            answers[question["question"]] = value
        answer = {"answers": answers}
    else:
        raise HTTPException(status_code=400, detail="Not an answer to this prompt.")
    if not db.answer_prompt(conn, session["claude_session_id"], prompt["id"], answer):
        return _with_param(back, "error", "That prompt isn't waiting any more: it was answered, or went back to the terminal.")
    return RedirectResponse(back, status_code=303)


@app.post("/sessions/{session_id}/continue")
async def continue_session_route(session_id: int, message: str = Form(""), how: str = Form(""),
                                 next: str | None = Form(None)):
    """Reply to a session that finished its turn: resume it with the message, or start
    a fresh session from a handoff, tracked as a task like any other dashboard start.
    Resuming without a message just picks up where it left off."""
    conn = db.get_connection()
    session = _session(conn, session_id)
    back = _safe_next(next, f"/sessions/{session_id}")
    plan = db.continue_plan(session)
    message = message.strip()
    fresh = (how or plan["recommend"]) == "fresh"
    if not plan["allowed"]:
        return _with_param(back, "error", plan["why_not"])
    if fresh and not message:
        return _with_param(back, "error", "Write what it should do next.")
    if fresh:
        if not session["project_id"]:
            return _with_param(back, "error", "This session isn't in a project, so a fresh one has nowhere to be tracked.")
        path = transcripts.find(session["claude_session_id"], claude_runtime.claude_config_dirs())
        title = message.splitlines()[0]
        task_id = db.add_work(conn, session["project_id"], "task", title[:120] + ("…" if len(title) > 120 else ""),
                              detail=db.handoff(session, message, str(path) if path else None),
                              repository_id=session["repository_id"])
        error = await _dispatch(conn, task_id, parent_session_id=session["claude_session_id"])
        return _with_param(back, "error", error) if error else RedirectResponse(f"/decisions/{task_id}", status_code=303)
    cwd = session["cwd"] if session["cwd"] and Path(session["cwd"]).is_dir() else None
    if cwd is None:
        repository = db.get_repository(conn, session["repository_id"]) if session["repository_id"] else None
        cwd = repository["local_path"] if repository else None
    if cwd is None:
        return _with_param(back, "error", "Its folder is gone, so there's nowhere to resume it.")
    try:
        if session["status"] != "ended" and session["agent_id"]:
            # A second process on the same conversation would interleave the two.
            await asyncio.to_thread(claude_runtime.stop_background, session["agent_id"])
        agent_id = await asyncio.to_thread(_start_background, cwd, message or db.RESUME_PROMPT,
                                           session["claude_session_id"])
    except claude_runtime.LaunchError as exc:
        return _with_param(back, "error", str(exc))
    db.record_event(conn, verb="session.continued", actor="human", source="dashboard", project_id=session["project_id"],
                    session_id=session["claude_session_id"],
                    summary=f"Continued in the background{f' ({agent_id})' if agent_id else ''}: "
                            f"{message[:120] or 'picking up where it left off'}")
    return RedirectResponse(back, status_code=303)


@app.post("/sessions/{session_id}/checklist")
def add_checklist_item(session_id: int, title: str = Form(...), next: str | None = Form(None)):
    conn = db.get_connection()
    db.add_checklist_item(conn, session_id, title)
    return _session_back(conn, session_id, next)


@app.post("/sessions/{session_id}/checklist/{item_id}/complete")
def complete_checklist_item(session_id: int, item_id: int, next: str | None = Form(None)):
    conn = db.get_connection()
    db.complete_checklist_item_by_id(conn, item_id)
    return _session_back(conn, session_id, next)


def _moved_to_tab(tab: str, project: str | None, query: str = "") -> RedirectResponse:
    conn = db.get_connection()
    row = db.find_project(conn, project) if project else None
    base = f"/projects/{row['id']}" if row else "/projects"
    return RedirectResponse(f"{base}{TAB_SUFFIX[tab]}{query}")


@app.get("/activity")
def activity_moved(project: str | None = None, all: int = 0):
    return _moved_to_tab("activity", project, "?all=1" if all else "")


def _day_label(iso_ts: str) -> str:
    day = datetime.fromisoformat(iso_ts).astimezone().date()
    today = datetime.now().astimezone().date()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return day.strftime("%a %b %-d")


def _build_settings_context(
    conn, submitted: dict | None = None, error: str | None = None, error_provider: str | None = None
) -> dict:
    connectors = []
    for src in sources.SOURCES:
        display_name = credentials.get_display_name(conn, src.name)
        connectors.append({
            "name": src.name,
            "configured": src.is_configured(),
            "enabled": db.is_connector_enabled(conn, src.name),
            "display_name": display_name,
            "connected_via_env": src.is_configured() and display_name is None,
            "has_credential_form": src.name in PROVIDER_VALIDATORS,
            "submitted": submitted if src.name == error_provider else None,
            "error": error if src.name == error_provider else None,
        })
    start, _, end = (db.get_ui_state(conn, "notify_hours") or db.NOTIFY_DEFAULT_HOURS).partition("-")
    notify = {"start": start, "end": end, "after": db.NOTIFY_AFTER_MINUTES, "daily": db.NOTIFY_DAILY_LIMIT,
              "gap": db.NOTIFY_GAP_MINUTES, "remind": db.NOTIFY_REMIND_AFTER_HOURS}
    return {"active_nav": "settings", "connectors": connectors, "notify": notify,
            "linear_goal_projects": db.linear_goal_projects(conn)}


def _require_known_provider(provider: str) -> None:
    if provider not in PROVIDER_VALIDATORS:
        raise HTTPException(status_code=404, detail=f"Unknown integration provider: {provider}")


@app.get("/settings")
def settings_page(request: Request):
    conn = db.get_connection()
    return templates.TemplateResponse(request, "settings.html", _build_settings_context(conn))


@app.post("/settings/notify-hours")
def notify_hours(start: int = Form(...), end: int = Form(...)):
    if not (0 <= start <= 23 and 1 <= end <= 24):
        raise HTTPException(status_code=400, detail="Hours run 0–24")
    db.set_ui_state(db.get_connection(), "notify_hours", f"{start}-{end}")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/connectors/{name}/toggle")
def toggle_connector(name: str):
    conn = db.get_connection()
    db.set_connector_enabled(conn, name, not db.is_connector_enabled(conn, name))
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/integrations/{provider}/connect")
async def connect_integration(request: Request, provider: str):
    _require_known_provider(provider)
    form = dict(await request.form())
    conn = db.get_connection()
    try:
        result = PROVIDER_VALIDATORS[provider](form)
    except Exception:  # noqa: BLE001
        return templates.TemplateResponse(
            request, "settings.html",
            _build_settings_context(
                conn, submitted=form,
                error=f"Could not verify those credentials with {provider.capitalize()}. "
                      "Check the values and try again.",
                error_provider=provider,
            ),
            status_code=400,
        )
    credentials.set_credentials(conn, provider, form, display_name=result.get("display_name"))
    _connected(conn, provider, result)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/integrations/{provider}/disconnect")
def disconnect_integration(provider: str):
    _require_known_provider(provider)
    conn = db.get_connection()
    credentials.delete_credentials(conn, provider)
    _clear_provider_caches(provider)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/integrations/{provider}/test")
def test_integration(request: Request, provider: str):
    _require_known_provider(provider)
    conn = db.get_connection()
    stored = credentials.get_credentials(conn, provider)
    if not stored:
        return templates.TemplateResponse(
            request, "settings.html",
            _build_settings_context(conn, error="Not connected yet — add credentials first.", error_provider=provider),
            status_code=400,
        )
    try:
        result = PROVIDER_VALIDATORS[provider](stored)
    except Exception:  # noqa: BLE001
        return templates.TemplateResponse(
            request, "settings.html",
            _build_settings_context(
                conn,
                error=f"Could not verify those credentials with {provider.capitalize()}. "
                      "Check the values and try again.",
                error_provider=provider,
            ),
            status_code=400,
        )
    credentials.set_credentials(conn, provider, stored, display_name=result.get("display_name"))
    _clear_provider_caches(provider)
    return RedirectResponse("/settings", status_code=303)


@app.get("/decisions")
def decisions_moved(project: str | None = None):
    return _moved_to_tab("decisions", project)


@app.get("/decisions/{decision_id}")
def decision_detail(request: Request, decision_id: int, unverified: str | None = None, launch_error: str | None = None,
                    note_error: str | None = None):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")
    repository = None
    if decision["repository_id"]:
        repository = db.get_repository(conn, decision["repository_id"])
    else:
        repository = db.get_repository_for_project(conn, decision["project_id"])
    attachments = db.list_attachments(conn, decision_id)
    folders = db.project_repositories(conn, decision["project_id"])
    decision["work_location"] = db.current_work_location(conn, decision)
    return templates.TemplateResponse(
        request,
        "decision.html",
        {
            "active_nav": "decisions",
            "decision": decision,
            "project_name": db.get_project_name(conn, decision["project_id"]),
            "repository": repository,
            "transit": next((d for d in db.list_in_transit(conn) if d["id"] == decision_id), None),
            "unverified": unverified,
            "launch_error": launch_error,
            "note_error": note_error,
            "notes": db.list_notes(conn, decision_id),
            "apply": db.apply_plan(conn, decision_id) if decision["status"] == "answered" else None,
            "attachments": attachments,
            "attempts": db.list_attempts(conn, decision_id) if decision["kind"] == "task" else [],
            "effort_text": db.effort_text(db.task_effort(conn, decision_id)),
            "claim_stale": decision["kind"] == "task" and any(t["id"] == decision_id for t in db.stuck_claims(conn)),
            "folders": folders,
            "dependencies": db.item_dependencies(conn, decision_id),
            "dependency_candidates": db.dependency_candidates(conn, decision_id)
            if decision["status"] not in ("resolved", "cancelled") else [],
            "rule_draft": _rule_draft(decision),
            "can_undo": db.can_undo(decision),
            "folder_default": repository["id"] if repository else (db.default_repository(conn, decision["project_id"]) or {}).get("id"),
        },
    )


def _rule_draft(decision: dict) -> str | None:
    """A first wording for "make this a standing rule", from an answered decision."""
    if decision["kind"] != "decision" or not decision.get("answer_text") or decision.get("answer_source") == "archive":
        return None
    if decision.get("answer_kind") == "delegate":
        return f"For questions like \u201c{decision['title']}\u201d: use your judgment, prefer what's easiest to reverse, and record your reasoning."
    if decision.get("answer_kind") in ("clarify", "decline"):
        return None
    return f"For questions like \u201c{decision['title']}\u201d: {db.plain_text(decision['answer_text'])[:300]}"


@app.post("/decisions/{decision_id}/start")
async def start_task(decision_id: int):
    """Hand a READY task to a new background session in its folder."""
    conn = db.get_connection()
    task = db.get_decision_request(conn, decision_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    back = f"/decisions/{decision_id}"
    if task["kind"] != "task" or task["status"] != "open":
        return _with_launch_error(back, "Only a task that's ready for an agent can be started.")
    error = await _dispatch(conn, decision_id)
    return _with_launch_error(back, error) if error else RedirectResponse(back, status_code=303)


@app.post("/decisions/{decision_id}/repository")
def move_item_route(decision_id: int, repository: str = Form("")):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    back = f"/decisions/{decision_id}"
    try:
        db.move_item(conn, decision_id, _picked(repository) or 0)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    return RedirectResponse(back, status_code=303)


@app.post("/decisions/{decision_id}/answer")
async def answer_decision(
    request: Request, decision_id: int, answer_text: str = Form(""), choice: str | None = Form(None),
    option: int | None = Form(None), next: str | None = Form(None), allow_secret: str | None = Form(None),
    force: str | None = Form(None),
):
    """A typed answer, or a one-click choice (an option, the recommendation, "you
    decide", "need more info", "decline", "done") with the text box as its note. For a
    structured Action, also its inputs (secret ones stored locally, never in the
    answer) and its verify check, which must pass unless force=1."""
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")
    back = _safe_next(next, f"/decisions/{decision_id}")
    secret = db.looks_like_secret(answer_text)
    if secret and allow_secret != "1":
        return _with_param(back, "error",
                           f"That answer looks like it contains a {secret}. Answers are written into the repo's "
                           ".claude/decisions/ files, and some repos commit them. Put the secret in the project's .env "
                           "or a secret manager and refer to it by name — or confirm to send it anyway.")
    kind = "option" if option is not None else (choice or "text")
    note = answer_text
    spec = decision.get("action_spec") or {}
    if decision["kind"] == "action" and kind in ("done", "text") and spec:
        form = await request.form()
        extras = []
        check = spec.get("verify")
        if check and force == "1":
            extras.append("Not verified — marked done anyway.")
        elif check:
            repository = (db.get_repository(conn, decision["repository_id"]) if decision["repository_id"]
                          else db.get_repository_for_project(conn, decision["project_id"]))
            result = await asyncio.to_thread(verify.run_check, check, repository["local_path"] if repository else None)
            if not result.ok:
                return RedirectResponse(f"/decisions/{decision_id}?unverified={quote(result.detail)}", status_code=303)
            extras.append(f"Verified: {result.detail}")
        provided = []
        for field in spec.get("inputs") or []:
            value = str(form.get(f"input_{field.get('name')}") or "").strip()
            if not value:
                continue
            label = field.get("label") or field.get("name")
            if field.get("secret"):
                db.store_action_secret(conn, decision_id, field["name"], value)
                provided.append(f"- {label}: provided and stored locally — fetch it with "
                                f"get_action_secret(decision_id={decision_id}, name=\"{field['name']}\")")
            else:
                provided.append(f"- {label}: {value}")
        if provided:
            extras.append("Provided:\n" + "\n".join(provided))
        note = "\n\n".join(part for part in [note.strip(), *extras] if part)
    try:
        text, kind, chosen = db.compose_answer(decision, kind, option=option, note=note)
    except ValueError as exc:
        return _with_param(back, "error", str(exc))
    db.snapshot_for_undo(conn, decision_id, "done")
    if not db.answer_decision_request(conn, decision_id, text, answer_kind=kind, answer_option=chosen):
        db.clear_undo(conn, decision_id)
        return RedirectResponse(back, status_code=303)
    return _with_undo(RedirectResponse(back, status_code=303), decision_id, "Answer sent")


def _attachment_json(attachment: dict) -> dict:
    return {
        "id": attachment["id"],
        "filename": attachment["filename"],
        "url": f"/attachments/{attachment['id']}",
        "content_type": attachment["content_type"],
        "size_bytes": attachment["size_bytes"],
    }


def _require_unanswered(decision: dict) -> None:
    # Attachments are only meant to be edited while composing an answer — once a
    # decision is answered, its markdown (and any copied attachment files) have
    # already been written into the target repo, and nothing re-syncs that
    # projection on a later attachment add/remove. Locking both ends (the
    # composer's Remove button is also hidden post-answer, see
    # _attachment_composer.html) keeps the dashboard and the repo from
    # silently diverging rather than trying to re-sync on every edit.
    if decision["answer_text"]:
        raise HTTPException(status_code=409, detail="This decision is already answered — attachments are locked.")


@app.post("/decisions/{decision_id}/attachments")
async def upload_attachments(decision_id: int, files: list[UploadFile]):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")
    _require_unanswered(decision)

    uploads = [(upload.filename, upload.content_type, await upload.read()) for upload in files]

    existing_count = len(db.list_attachments(conn, decision_id))
    if existing_count + len(uploads) > db.ATTACHMENT_MAX_PER_DECISION:
        raise HTTPException(
            status_code=400,
            detail=f"That would exceed the {db.ATTACHMENT_MAX_PER_DECISION}-attachment limit for this decision.",
        )
    # Validate every file before persisting any of them — otherwise file N failing
    # in a multi-file request would leave files 1..N-1 from the same request
    # already saved with no way for the caller to know.
    for filename, content_type, data in uploads:
        try:
            db.validate_attachment(content_type, data, filename or "That file")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    saved = [
        db.save_attachment(conn, decision_id, filename, content_type, data)
        for filename, content_type, data in uploads
    ]
    return [_attachment_json(a) for a in saved]


@app.get("/attachments/{attachment_id}")
def serve_attachment(attachment_id: int):
    conn = db.get_connection()
    attachment = db.get_attachment(conn, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = config.get_attachments_dir() / str(attachment["decision_id"]) / attachment["stored_name"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")
    return Response(
        content=path.read_bytes(),
        media_type=attachment["content_type"],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.delete("/decisions/{decision_id}/attachments/{attachment_id}")
def remove_attachment(decision_id: int, attachment_id: int):
    conn = db.get_connection()
    attachment = db.get_attachment(conn, attachment_id)
    if not attachment or attachment["decision_id"] != decision_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    _require_unanswered(db.get_decision_request(conn, decision_id))
    db.delete_attachment(conn, attachment_id)
    return Response(status_code=204)


@app.post("/api/decisions")
def api_create_decision(payload: dict):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, payload["project"])
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        title=payload["title"],
        question=payload["question"],
        summary=payload.get("summary"),
        context=payload.get("context"),
        why_it_matters=payload.get("why_it_matters"),
        options=payload.get("options"),
        blocked_work=payload.get("blocked_work"),
        safe_work=payload.get("safe_work"),
        recommendation=payload.get("recommendation"),
        confidence=payload.get("confidence"),
        resume_plan=payload.get("resume_plan"),
    )
    return {"id": decision_id}


@app.get("/api/decisions")
def api_list_decisions(project: str | None = None, status: str | None = None):
    conn = db.get_connection()
    rows = db.list_decision_requests(conn, project_name=project, status=status)
    return [dict(row) for row in rows]


@app.patch("/api/decisions/{decision_id}")
def api_edit_decision(decision_id: int, payload: dict):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    try:
        changed = db.edit_item(conn, decision_id, title=payload.get("title"),
                               description=payload.get("description"), details=payload.get("details"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": decision_id, "changed": changed}


@app.post("/api/decisions/{decision_id}/reopen")
def api_reopen_decision(decision_id: int):
    conn = db.get_connection()
    if db.get_decision_request(conn, decision_id) is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    status = db.reopen_item(conn, decision_id)
    if status is None:
        raise HTTPException(status_code=409, detail="Only a resolved or cancelled item can be reopened")
    return {"id": decision_id, "status": status}


@app.get("/api/decisions/{decision_id}")
def api_get_decision(decision_id: int):
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")
    return decision


def run_dashboard() -> None:
    """Entry point for the `ado-dashboard` console script — same as run.sh, minus
    --reload, which is a dev-only convenience for this checkout."""
    import uvicorn
    uvicorn.run("apo.web.app:app", host="127.0.0.1", port=8430)
