"""Domain model + DB access for the apo.

Schema: Project -> Repository / DecisionRequest -> ResumePlan (1:1), plus an
append-only AuditLog per decision.

This module is the single source of truth and the only place decision state changes.
Every state transition also refreshes the in-repo markdown projection via
_sync_repo_files, so an answer arriving from any channel — dashboard, Linear, MCP,
CLI — reaches the repository the same way.
"""
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from apo import config


SCHEMA_VERSION = 24


def data_version(conn: sqlite3.Connection) -> int:
    """Changes whenever another connection commits, in this process or any other."""
    return conn.execute("PRAGMA data_version").fetchone()[0]


def get_connection() -> sqlite3.Connection:
    db_path = config.get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Session-tracking hooks mean many short-lived writer processes now hit this file
    # concurrently (one per tool call, across every open session) — a plain
    # "database is locked" failure would make a hook noisy/flaky instead of just slow.
    conn.execute("PRAGMA busy_timeout = 2000")
    # Safe under WAL (a crash can lose the last commit, never corrupt the file), and
    # it turns each hook's commit from a full fsync into a WAL append.
    conn.execute("PRAGMA synchronous = NORMAL")
    # Every hook process opens a connection, so schema work must not run on each one:
    # a single integer read decides whether there's anything to do.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the DB up to SCHEMA_VERSION. Every DDL step is idempotent, and the
    version check is repeated under a write lock, so concurrent hook processes that
    all saw an old version can't double-apply anything."""
    try:
        # Persistent once set. Readers (the dashboard) stop blocking hook writers.
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # another process holds the file; WAL is an optimization, not required
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            conn.rollback()
            return
        _apply_ddl(conn)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            step = _DATA_MIGRATIONS.get(version)
            if step:
                step(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_checklist_session ON checklist_items(session_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_status_seen ON sessions(status, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_dr_status_project ON decision_requests(status, project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_dr_origin_session ON decision_requests(origin_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_dr ON audit_log(decision_request_id)",
    "CREATE INDEX IF NOT EXISTS ix_attach_dr ON attachments(decision_id)",
    "CREATE INDEX IF NOT EXISTS ix_sync_errors_time ON sync_errors(occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_item_notes_item ON item_notes(item_id, id)",
    "CREATE INDEX IF NOT EXISTS ix_events_project ON events(project_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_events_decision ON events(decision_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, seq)",
    "CREATE INDEX IF NOT EXISTS ix_dr_outcome ON decision_requests(outcome_id, sort_order)",
    "CREATE INDEX IF NOT EXISTS ix_deps_blocker ON work_item_deps(blocker_id)",
    "CREATE INDEX IF NOT EXISTS ix_outcomes_project ON outcomes(project_id, status)",
    # One goal per Linear issue, even if two pollers race to import it.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomes_linear ON outcomes(linear_issue_id) WHERE linear_issue_id IS NOT NULL",
)


# Search: one trigram FTS5 table per source, rowid = the source row's id, kept
# current by triggers (so every writer — hooks, MCP, dashboard — is covered without
# remembering to). Trigram means any substring of 3+ characters matches.
_SEARCH_SOURCES = {
    # table: (source table, indexed text columns, columns whose update re-indexes)
    "search_items": ("decision_requests", ("question", "summary", "context", "why_it_matters", "recommendation",
                                           "answer_text", "waiting_on"), ("title", "project_id")),
    "search_sessions": ("sessions", ("last_prompt", "last_summary", "cwd"), ("title", "project_id")),
    "search_outcomes": ("outcomes", ("done_when",), ("title", "project_id")),
}


def _search_body(alias: str, columns: tuple) -> str:
    return " || ' ' || ".join(f"COALESCE({alias}.{c}, '')" for c in columns)


def _search_ddl() -> list[str]:
    statements = []
    for fts, (source, columns, extra) in _SEARCH_SOURCES.items():
        insert = (f"INSERT INTO {fts} (rowid, title, body) VALUES "
                  f"(new.id, COALESCE(new.title, ''), {_search_body('new', columns)});")
        statements += [
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5(title, body, tokenize='trigram')",
            f"CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {source} BEGIN {insert} END",
            f"CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE OF {', '.join(columns + extra)} ON {source} "
            f"BEGIN DELETE FROM {fts} WHERE rowid = old.id; {insert} END",
            f"CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {source} BEGIN DELETE FROM {fts} WHERE rowid = old.id; END",
        ]
    return statements


def rebuild_search_index(conn: sqlite3.Connection, commit: bool = True) -> None:
    for fts, (source, columns, _extra) in _SEARCH_SOURCES.items():
        conn.execute(f"DELETE FROM {fts}")
        conn.execute(f"INSERT INTO {fts} (rowid, title, body) SELECT id, COALESCE(title, ''), "
                     f"{_search_body(source, columns)} FROM {source}")
    if commit:
        conn.commit()


def _apply_ddl(conn: sqlite3.Connection) -> None:
    # One statement at a time rather than executescript, which would COMMIT the
    # write lock ensure_schema is holding.
    for statement in _TABLES.split(";"):
        if statement.strip():
            conn.execute(statement)
    _add_column(conn, "projects", "archived_at", "TEXT")
    _add_column(conn, "projects", "pinned_at", "TEXT")
    # The project brief and status (see project_status): what it's for, how you know
    # it's done, its links, and a status you set over the suggested one.
    for column in ("goal", "done_when", "links", "status_override", "status_note", "status_set_at"):
        _add_column(conn, "projects", column, "TEXT")
    # Goals from Linear (linear_goals.py): NULL is off; otherwise the createdAt of the
    # newest issue already looked at, so it's the switch and the import cursor at once.
    _add_column(conn, "projects", "linear_goals_since", "TEXT")
    # The Linear project they come from: picked, or the name match remembered, so a
    # rename here doesn't lose it.
    for column in ("linear_project_id", "linear_project_name"):
        _add_column(conn, "projects", column, "TEXT")
    # A goal imported from a Linear issue: its description, the issue, the issue's last
    # state seen (only a change in Linear moves the goal), and the status here last
    # squared with Linear (an achieve or drop here is sent once).
    for column in ("description", "linear_issue_id", "linear_identifier", "linear_url", "linear_state",
                   "linear_status_synced"):
        _add_column(conn, "outcomes", column, "TEXT")
    # Priority between projects (hot / normal / background; NULL is normal), and due
    # dates (YYYY-MM-DD) on goals and on any item.
    _add_column(conn, "projects", "priority", "TEXT")
    # What a project's background agents may do without asking (NULL is ask).
    _add_column(conn, "projects", "autonomy", "TEXT")
    _add_column(conn, "outcomes", "due_on", "TEXT")
    _add_column(conn, "decision_requests", "due_on", "TEXT")
    _add_column(conn, "sync_errors", "resolved_at", "TEXT")
    # occurred_at is the latest occurrence; repeats of an unresolved failure bump count.
    _add_column(conn, "sync_errors", "count", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "sync_errors", "first_occurred_at", "TEXT")
    for column, coltype in (
        ("linear_issue_id", "TEXT"),
        ("linear_identifier", "TEXT"),
        ("linear_url", "TEXT"),
        ("linear_synced_comment_id", "TEXT"),
        ("linear_push_error", "TEXT"),
        ("linear_resolve_error", "TEXT"),
        ("linear_retry_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("linear_next_retry_at", "TEXT"),
        # What still has to reach the Linear issue after an edit/reopen: {"text": bool, "state": str|None}.
        ("linear_edit_pending", "TEXT"),
        ("linear_edit_error", "TEXT"),
        # JSON of the fields the last Done/Cancel/Snooze overwrote, so Undo can put them back.
        ("undo_snapshot", "TEXT"),
        # The answer a Reopen set aside because the item can be answered afresh.
        ("previous_answer", "TEXT"),
        ("origin_session_id", "TEXT"),
        ("summary", "TEXT"),
        ("kind", "TEXT NOT NULL DEFAULT 'decision'"),
        # How it was answered: text | option | recommendation | delegate | clarify | decline | done
        ("answer_kind", "TEXT"),
        ("answer_option", "INTEGER"),
        ("recommended_option", "INTEGER"),
        ("summary_derived", "INTEGER NOT NULL DEFAULT 0"),
        # Whether the answer reached an agent: none (unanswered) | pending (answered,
        # nobody told yet) | delivered (the asking session was told) | applied (resolved)
        ("delivery", "TEXT DEFAULT 'none'"),
        ("delivered_at", "TEXT"),
        ("delivered_to_session", "TEXT"),
        # Personal triage, not status: out of the queue until a time, or (condition
        # 'until_blocks') while the agent that asked is still working.
        ("snoozed_until", "TEXT"),
        ("snooze_condition", "TEXT"),
        # Work items beyond decisions/actions (kind task | review | wait): who owns the
        # move (human | agent | external), where it sits in an outcome, who filed it.
        ("assignee", "TEXT"),
        ("requested_by", "TEXT NOT NULL DEFAULT 'agent'"),
        ("outcome_id", "INTEGER"),
        ("sort_order", "INTEGER"),
        # Hand-set position within a work group (1 = first); NULL keeps the default order.
        ("work_rank", "INTEGER"),
        ("waiting_on", "TEXT"),
        ("follow_up_at", "TEXT"),
        ("supersedes_id", "INTEGER"),
        ("claimed_by_session_id", "TEXT"),
        ("claimed_at", "TEXT"),
        ("cancel_reason", "TEXT"),
        # A structured Action: {where, steps, values, inputs, done_when, verify} (JSON).
        ("action_spec", "TEXT"),
        ("est_minutes", "INTEGER"),
        # "Apply in background": the `claude --bg` session started to apply the answer.
        ("dispatched_at", "TEXT"),
        ("dispatch_agent_id", "TEXT"),
        # JSON list, e.g. ["bug", "urgent"] for a reported bug.
        ("labels", "TEXT"),
        # Where the agent that closed it left the work: {branch, commit, default_branch,
        # merged} (JSON). See _work_location.
        ("work_location", "TEXT"),
        # What complete_task was told beyond the note: {outcome, done, not_done,
        # left_for_human, branch} (JSON). NULL for a plain completion.
        ("completion", "TEXT"),
    ):
        _add_column(conn, "decision_requests", column, coltype)
    for column in ("title", "last_prompt", "last_summary", "last_summary_at"):
        _add_column(conn, "sessions", column, "TEXT")
    # Tokens the session's latest model call sent: what its next turn re-reads.
    _add_column(conn, "sessions", "context_tokens", "INTEGER")
    # A background session's prompt a hook is holding for the dashboard to answer, and
    # the answer (JSON each). See hold_prompt.
    _add_column(conn, "sessions", "pending_prompt", "TEXT")
    _add_column(conn, "sessions", "prompt_answer", "TEXT")
    # exec_state is what the session is doing right now (see the session-tracking
    # section below); status stays as the coarser running/idle/blocked/ended view.
    for column, coltype in (
        ("exec_state", "TEXT"),
        ("waiting_detail", "TEXT"),
        ("waiting_since", "TEXT"),
        ("last_stop_at", "TEXT"),
        ("last_error", "TEXT"),
        ("active_at", "TEXT"),
        ("pid", "INTEGER"),
        ("live_checked_at", "TEXT"),
        # When the human last engaged with this session (a prompt, or "Reviewed"): a
        # turn that ended after it is finished work they haven't looked at.
        ("reviewed_at", "TEXT"),
        ("outcome_id", "INTEGER"),
        # From `claude agents --json`: the short id `claude stop` takes, and whether the
        # session runs in a terminal (interactive) or detached (background).
        ("agent_id", "TEXT"),
        ("agent_kind", "TEXT"),
        # The usage limit: Claude Code's own message (it carries the reset time), the
        # parsed reset, and a resume the human scheduled for a session that was closed.
        ("last_error_detail", "TEXT"),
        ("limit_resets_at", "TEXT"),
        ("resume_scheduled_at", "TEXT"),
        ("resume_attempts", "INTEGER NOT NULL DEFAULT 0"),
        # A rule-based reading of how the session ended (see compute_verdict), and the
        # claude_session_id of the session that dispatched this one, if one did.
        ("verdict", "TEXT"),
        ("verdict_reason", "TEXT"),
        ("parent_session_id", "TEXT"),
    ):
        _add_column(conn, "sessions", column, coltype)
    # The session that dispatched this item, so the session it starts can link back.
    _add_column(conn, "decision_requests", "dispatch_parent_session_id", "TEXT")
    for statement in _INDEXES:
        conn.execute(statement)
    for statement in _search_ddl():
        conn.execute(statement)


def _backfill_active_at(conn: sqlite3.Connection) -> None:
    """A session whose last_seen_at moved past started_at did something after
    registering; one where they're equal never took a turn (a ghost)."""
    conn.execute(
        "UPDATE sessions SET active_at = last_seen_at WHERE active_at IS NULL AND last_seen_at != started_at"
    )


def _collapse_duplicate_sync_errors(conn: sqlite3.Connection) -> None:
    """Fold each run of identical failures into its latest row with a count."""
    conn.execute("UPDATE sync_errors SET first_occurred_at = occurred_at WHERE first_occurred_at IS NULL")
    groups = conn.execute(
        """
        SELECT source, decision_request_id, message, resolved_at, COUNT(*) AS n,
               MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at, MAX(id) AS keep_id
        FROM sync_errors GROUP BY source, decision_request_id, message, resolved_at HAVING COUNT(*) > 1
        """
    ).fetchall()
    for g in groups:
        conn.execute(
            "DELETE FROM sync_errors WHERE source = ? AND decision_request_id IS ? AND message = ? "
            "AND resolved_at IS ? AND id != ?",
            (g["source"], g["decision_request_id"], g["message"], g["resolved_at"], g["keep_id"]),
        )
        conn.execute(
            "UPDATE sync_errors SET count = ?, first_occurred_at = ?, occurred_at = ? WHERE id = ?",
            (g["n"], g["first_at"], g["last_at"], g["keep_id"]),
        )


# One-time data steps, keyed by the schema version that introduces them. DDL lives in
# _apply_ddl and is idempotent; these run once, when a DB crosses that version.
def _rescale_ten_point_confidences(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE decision_requests SET confidence = confidence * 10 WHERE confidence > 0 AND confidence <= 10")


def _drop_home_directory_registrations(conn: sqlite3.Connection) -> None:
    """Undo repositories auto-registered at ~ (or above) by sessions started there.
    Only ones nothing was ever filed against: a decision on record is a human's call."""
    for repo in conn.execute("SELECT * FROM repositories").fetchall():
        if not _is_home_or_above(repo["local_path"]):
            continue
        if conn.execute("SELECT 1 FROM decision_requests WHERE repository_id = ?", (repo["id"],)).fetchone():
            continue
        conn.execute(
            "UPDATE sessions SET repository_id = NULL, project_id = NULL WHERE repository_id = ?", (repo["id"],),
        )
        conn.execute("DELETE FROM repositories WHERE id = ?", (repo["id"],))
        project_id = repo["project_id"]
        still_used = conn.execute(
            "SELECT 1 FROM decision_requests WHERE project_id = ? UNION ALL "
            "SELECT 1 FROM repositories WHERE project_id = ? LIMIT 1",
            (project_id, project_id),
        ).fetchone()
        if not still_used:
            conn.execute("UPDATE sessions SET project_id = NULL WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_aliases WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def _backfill_events(conn: sqlite3.Connection) -> None:
    """Rebuild the event history from the per-decision audit log and session rows,
    in time order, so Activity isn't empty on day one."""
    history = []
    for row in conn.execute(
        "SELECT audit_log.event_type, audit_log.detail, audit_log.created_at, decision_requests.id, "
        "decision_requests.title, decision_requests.kind, decision_requests.project_id, "
        "decision_requests.answer_source FROM audit_log "
        "JOIN decision_requests ON decision_requests.id = audit_log.decision_request_id"
    ):
        event = _decision_event(row["event_type"], dict(row), row["detail"], row["answer_source"])
        if event:
            history.append((row["created_at"], event))
    for row in conn.execute("SELECT * FROM sessions"):
        where = {"project_id": row["project_id"], "session_id": row["claude_session_id"]}
        history.append((row["started_at"], dict(where, verb="session.started", actor="agent", source="hook",
                                                 summary=_session_label(conn, row, "Session started"))))
        if row["ended_at"]:
            crashed = row["end_reason"] == "process_gone_mid_turn"
            history.append((row["ended_at"], dict(
                where, verb="session.crashed" if crashed else "session.ended", actor="system", source="hook",
                summary=_session_label(conn, row, "Session crashed" if crashed else "Session ended"),
                importance=2 if crashed else 1,
            )))
    for at, event in sorted(history, key=lambda pair: pair[0]):
        _insert_event(conn, at=at, **event)


def _square_linear_closings(conn: sqlite3.Connection) -> None:
    """From 18 a cancel, achieve or drop here reaches Linear, not just a done. What was
    already closed before then counts as squared, so upgrading closes nothing in Linear."""
    if "done_synced_at" in [r[1] for r in conn.execute("PRAGMA table_info(linear_steps)")]:
        conn.execute("ALTER TABLE linear_steps RENAME COLUMN done_synced_at TO closed_synced_at")
    conn.execute(
        "UPDATE outcomes SET linear_status_synced = status "
        "WHERE linear_issue_id IS NOT NULL AND status IN ('achieved', 'dropped')"
    )
    conn.execute(
        "UPDATE linear_steps SET closed_synced_at = ? WHERE closed_synced_at IS NULL "
        "AND step_id IN (SELECT id FROM decision_requests WHERE status = 'cancelled')",
        (_now(),),
    )


_DATA_MIGRATIONS = {
    2: _backfill_active_at,
    4: _collapse_duplicate_sync_errors,
    5: _rescale_ten_point_confidences,
    6: _drop_home_directory_registrations,
    7: _backfill_events,
    12: lambda conn: conn.execute("UPDATE decision_requests SET assignee = 'human' WHERE assignee IS NULL"),
    14: lambda conn: rebuild_search_index(conn, commit=False),
    18: _square_linear_closings,
    9: lambda conn: conn.execute(
        "UPDATE decision_requests SET delivery = CASE "
        "WHEN status = 'answered' THEN 'pending' "
        "WHEN status = 'resolved' AND COALESCE(answer_source, '') != 'archive' THEN 'applied' "
        "ELSE 'none' END"
    ),
}


def _normalize_confidence(confidence: int | None) -> int | None:
    """Stored as a percentage. Agents also file on a 0-10 scale (6, 7, 8 in the live
    DB), which read as "7%"; 1-10 can't plausibly mean a percentage that low for a
    recommendation worth filing, so it's taken as out of 10."""
    if confidence is None:
        return None
    confidence = int(confidence)
    if 0 < confidence <= 10:
        confidence *= 10
    return max(0, min(100, confidence))


def _add_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc):
            raise


_TABLES = """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            local_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS decision_requests (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            repository_id INTEGER REFERENCES repositories(id),
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            context TEXT,
            why_it_matters TEXT,
            options TEXT,
            blocked_work TEXT,
            safe_work TEXT,
            recommendation TEXT,
            confidence INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            answer_text TEXT,
            answer_source TEXT,
            created_at TEXT NOT NULL,
            answered_at TEXT,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS resume_plans (
            id INTEGER PRIMARY KEY,
            decision_request_id INTEGER UNIQUE NOT NULL REFERENCES decision_requests(id),
            objective TEXT,
            files TEXT,
            previous_reasoning TEXT,
            remaining_steps TEXT,
            expected_outcome TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY,
            decision_request_id INTEGER NOT NULL REFERENCES decision_requests(id),
            event_type TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_errors (
            id INTEGER PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            source TEXT NOT NULL,
            decision_request_id INTEGER,
            message TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            claude_session_id TEXT UNIQUE NOT NULL,
            project_id INTEGER REFERENCES projects(id),
            repository_id INTEGER REFERENCES repositories(id),
            cwd TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            current_blocker_id INTEGER REFERENCES decision_requests(id),
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_at TEXT,
            end_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS checklist_items (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            claude_task_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            completed_by TEXT,
            completion_notes TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS connector_settings (
            name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS integration_credentials (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL UNIQUE,
            encrypted_secret BLOB NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_aliases (
            alias TEXT PRIMARY KEY COLLATE NOCASE,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS item_notes (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES decision_requests(id),
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            verb TEXT NOT NULL,
            actor TEXT NOT NULL,
            source TEXT,
            project_id INTEGER,
            session_id TEXT,
            decision_id INTEGER,
            summary TEXT NOT NULL,
            data TEXT,
            importance INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            done_when TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by TEXT NOT NULL DEFAULT 'human',
            created_by_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            achieved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS standing_rules (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            text TEXT NOT NULL,
            source_decision_id INTEGER REFERENCES decision_requests(id),
            created_at TEXT NOT NULL,
            retired_at TEXT
        );

        CREATE TABLE IF NOT EXISTS work_item_deps (
            blocked_id INTEGER NOT NULL REFERENCES decision_requests(id),
            blocker_id INTEGER NOT NULL REFERENCES decision_requests(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY (blocked_id, blocker_id)
        );

        -- A step imported from a Linear sub-issue. Kept off decision_requests'
        -- linear_* columns, which mean "a request pushed to Linear for an answer":
        -- LinearSource polls those for comments and closes them when answered.
        -- closed_synced_at: when the step being closed (done or cancelled) was squared
        -- with Linear (Linear said so, or it was told), after which neither side's
        -- close is sent to the other.
        CREATE TABLE IF NOT EXISTS linear_steps (
            step_id INTEGER PRIMARY KEY REFERENCES decision_requests(id),
            outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
            linear_issue_id TEXT UNIQUE NOT NULL,
            linear_identifier TEXT NOT NULL,
            linear_url TEXT NOT NULL,
            linear_state TEXT,
            closed_synced_at TEXT
        );

        -- Every claim, completion, release and retry of an agent task, in order.
        -- decision_requests only holds the current state, this is how it got there.
        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES decision_requests(id),
            event TEXT NOT NULL,
            session_id TEXT,
            actor TEXT,
            at TEXT NOT NULL,
            outcome TEXT,
            note TEXT,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts(task_id, id);

        CREATE TABLE IF NOT EXISTS ui_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY,
            item_key TEXT NOT NULL,
            project_id INTEGER,
            kind TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            retracted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY,
            decision_id INTEGER NOT NULL REFERENCES decision_requests(id),
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS saved_views (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            query TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value) -> str | None:
    return json.dumps(value) if value is not None else None


def _loads(value):
    return json.loads(value) if value is not None else None


def _project_key(name: str) -> str:
    """"Widget Exporter", "widget-exporter" and "widget_exporter"
    are one project: agents name projects in free text, sessions by folder name."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT projects.* FROM project_aliases JOIN projects ON projects.id = project_aliases.project_id "
        "WHERE project_aliases.alias = ?",
        (name,),
    ).fetchone()
    if row:
        return row
    key = _project_key(name)
    if not key:
        return None
    for candidate in conn.execute("SELECT * FROM projects ORDER BY id"):
        if _project_key(candidate["name"]) == key:
            return candidate
    for alias in conn.execute("SELECT alias, project_id FROM project_aliases"):
        if _project_key(alias["alias"]) == key:
            return conn.execute("SELECT * FROM projects WHERE id = ?", (alias["project_id"],)).fetchone()
    return None


def get_or_create_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = find_project(conn, name)
    if row:
        return row
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?)", (name, _now())
    )
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def add_project_alias(conn: sqlite3.Connection, alias: str, project_id: int) -> None:
    """Remember another name for a project, so filing under it again lands there."""
    existing = conn.execute("SELECT id FROM projects WHERE name = ?", (alias,)).fetchone()
    if existing is not None or not alias.strip():
        return
    conn.execute(
        "INSERT INTO project_aliases (alias, project_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(alias) DO UPDATE SET project_id = excluded.project_id",
        (alias, project_id, _now()),
    )
    conn.commit()


def resolve_project_for_filing(
    conn: sqlite3.Connection, name: str, cwd: str | None,
) -> tuple[sqlite3.Row, int | None]:
    """Which project and repository a request filed under `name` from `cwd` belongs to.

    A folder is one project. A name that matches no known project, filed from a
    registered folder, is that folder's project under another name (a rename, or an
    agent's own wording) and is remembered as an alias, so a rename doesn't leave a
    second project at the same path. A name that does match a known
    project wins over the folder: a session in one repo can file about another."""
    known = find_project(conn, name)
    repo = find_repository_by_path(conn, cwd) if cwd else None
    if known is None and repo is not None:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (repo["project_id"],)).fetchone()
        add_project_alias(conn, name, project["id"])
        return project, repo["id"]
    project = known or get_or_create_project(conn, name)
    if repo is not None and repo["project_id"] == project["id"]:
        return project, repo["id"]
    repository_id = ensure_repository_for_project(conn, project["id"], cwd) if cwd and repo is None else None
    if repository_id is None:
        only = get_repository_for_project(conn, project["id"])
        repository_id = only["id"] if only else None
    return project, repository_id


def get_project_name(conn: sqlite3.Connection, project_id: int) -> str | None:
    row = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row["name"] if row else None


def archive_project(
    conn: sqlite3.Connection, project_id: int, close_note: str = "Closed — project archived."
) -> dict:
    """Mark a project archived (hidden from the dashboard's default view, but every
    row stays in the DB for audit/history) and force-close any decision that isn't
    already resolved, so an archived project never leaves something dangling that
    looks like it's still waiting on a human. Records are never deleted."""
    closed_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM decision_requests WHERE project_id = ? AND status != 'resolved'",
            (project_id,),
        ).fetchall()
        if close_decision_request(conn, row["id"], close_note)
    ]
    cur = conn.execute(
        "UPDATE projects SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
        (_now(), project_id),
    )
    conn.commit()
    return {"archived": cur.rowcount == 1, "closed_decision_ids": closed_ids}


def unarchive_project(conn: sqlite3.Connection, project_id: int) -> bool:
    cur = conn.execute(
        "UPDATE projects SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
        (project_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def _search_terms(query: str) -> list[str]:
    terms = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query or ""):
        term = (phrase or word).strip('"').strip()
        if term:
            terms.append(term)
    return terms


def _snippet(text: str, terms: list[str], width: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    lowered = text.lower()
    positions = [lowered.find(t.lower()) for t in terms if lowered.find(t.lower()) >= 0]
    if not positions:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    start = max(0, min(positions) - width)
    end = min(len(text), min(positions) + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _search_hit(conn: sqlite3.Connection, kind: str, rowid: int, terms: list[str], text: str) -> dict | None:
    if kind == "item":
        row = conn.execute("SELECT id, title, kind, status, project_id, labels, assignee FROM decision_requests WHERE id = ?", (rowid,)).fetchone()
        if row is None:
            return None
        hit = {"title": row["title"], "status": row["status"], "subkind": row["kind"], "href": f"/decisions/{row['id']}",
               "labels": _loads(row["labels"]) or [], "owner": row["assignee"] or "human"}
    elif kind == "session":
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (rowid,)).fetchone()
        if row is None:
            return None
        s = dict(row)
        s["project_name"] = get_project_name(conn, s["project_id"]) if s["project_id"] else None
        hit = {"title": _session_name(s), "status": s["status"], "subkind": "session",
               "href": f"/sessions#session-card-{s['id']}", "resume_command": session_resume_command(s)}
    else:
        row = conn.execute("SELECT id, title, status, project_id FROM outcomes WHERE id = ?", (rowid,)).fetchone()
        if row is None:
            return None
        hit = {"title": row["title"], "status": row["status"], "subkind": "outcome", "href": f"/outcomes/{row['id']}"}
    return {"kind": kind, "ref": rowid, "project_name": get_project_name(conn, row["project_id"]) if row["project_id"] else None,
            "snippet": _snippet(text, terms), **hit}


_SEARCH_OWNERS = {"me": "human", "agent": "agent", "external": "external"}


def _item_filter(label: str, owner: str, status: str):
    label = label.strip().lower()

    def keep(hit: dict) -> bool:
        return ((not label or label in hit["labels"]) and (not owner or hit["owner"] == _SEARCH_OWNERS.get(owner))
                and (not status or hit["status"] == status))
    return keep


def search(conn: sqlite3.Connection, query: str, limit: int = 50, label: str = "", owner: str = "",
           status: str = "") -> list[dict]:
    """Every word has to match, anywhere in the title or text. Words of 3+
    characters go through the trigram index; shorter ones (which trigrams can't
    express) filter what that returns, or fall back to LIKE when they're all short."""
    terms = _search_terms(query)
    filtered = bool(label or owner or status)
    keep = _item_filter(label, owner, status)
    if not terms and filtered:
        # Filters alone list the matching items, newest first.
        rows = conn.execute("SELECT id FROM decision_requests ORDER BY id DESC").fetchall()
        hits = [_search_hit(conn, "item", r["id"], [], "") for r in rows]
        return [h for h in hits if h and keep(h)][:limit]
    if not terms:
        return []
    long = [t for t in terms if len(t) >= 3]
    short = [t.lower() for t in terms if len(t) < 3]
    hits = []
    for kind, fts in (("item", "search_items"), ("session", "search_sessions"), ("outcome", "search_outcomes")):
        if filtered and kind != "item":
            continue  # sessions and outcomes have no labels, owner or item status
        if long:
            match = " ".join('"' + t.replace('"', '""') + '"' for t in long)
            rows = conn.execute(f"SELECT rowid, title, body FROM {fts} WHERE {fts} MATCH ? ORDER BY rank LIMIT ?",
                                (match, limit * 4 if short or filtered else limit)).fetchall()
        else:
            where = " AND ".join("(title || ' ' || body) LIKE ? ESCAPE '\\'" for _ in short)
            params = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in short]
            rows = conn.execute(f"SELECT rowid, title, body FROM {fts} WHERE {where} ORDER BY rowid DESC LIMIT ?",
                                (*params, limit)).fetchall()
        for r in rows:
            text = f"{r['title']} {r['body']}"
            if any(t not in text.lower() for t in short):
                continue
            hit = _search_hit(conn, kind, r["rowid"], terms, plain_text(r["body"]) or r["title"])
            if hit and (kind != "item" or keep(hit)):
                hits.append(hit)
            if len([h for h in hits if h["kind"] == kind]) >= limit:
                break
    return hits


def _filter_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"A date is YYYY-MM-DD, not {value!r}") from None


_SEARCH_TABLES = {"item": ("decision_requests", "created_at"), "session": ("sessions", "started_at"),
                  "outcome": ("outcomes", "created_at")}


def search_filtered(conn: sqlite3.Connection, query: str, *, status: str | None = None, project: str | None = None,
                    owner: str | None = None, since: str | None = None, until: str | None = None,
                    label: str | None = None, limit: int = 50) -> list[dict]:
    """search() narrowed by status, project name, owner (me/agent/external, items only),
    label and a created-date range. With no filter it is exactly search(); with filters
    but no words it lists the newest matching rows."""
    since, until = _filter_date(since), _filter_date(until)
    if owner and owner not in _OWNER_KIND:
        raise ValueError(f"owner must be one of me, agent, external, got {owner!r}")
    if not any((status, project, owner, since, until, label)):
        return search(conn, query, limit=limit)
    if label:
        hits = search(conn, query, limit=limit * 4, label=label.strip().lower())
    elif _search_terms(query):
        hits = search(conn, query, limit=limit * 4)
    else:
        hits = []
        for kind, (table, _) in _SEARCH_TABLES.items():
            for r in conn.execute(f"SELECT id, title FROM {table} ORDER BY id DESC LIMIT ?", (limit * 4,)):
                hit = _search_hit(conn, kind, r["id"], [], r["title"])
                if hit:
                    hits.append(hit)
    counts: dict[str, int] = {}
    out = []
    for hit in hits:
        if status and hit["status"] != status:
            continue
        if project and (hit["project_name"] or "").lower() != project.lower():
            continue
        table, created_col = _SEARCH_TABLES[hit["kind"]]
        if owner:
            if hit["kind"] != "item":
                continue
            assignee = conn.execute("SELECT COALESCE(assignee, 'human') FROM decision_requests WHERE id = ?",
                                    (hit["ref"],)).fetchone()[0]
            if assignee != _ASSIGNEE[_OWNER_KIND[owner]]:
                continue
        if since or until:
            created = (conn.execute(f"SELECT {created_col} FROM {table} WHERE id = ?", (hit["ref"],)).fetchone()[0] or "")[:10]
            if (since and created < since) or (until and created > until):
                continue
        counts[hit["kind"]] = counts.get(hit["kind"], 0) + 1
        if counts[hit["kind"]] <= limit:
            out.append(hit)
    return out


_RG_META = set("\\.+*?()|[]{}^$")


def search_transcripts(conn: sqlite3.Connection, query: str, timeout: float = 10.0, limit: int = 30) -> list[dict]:
    """Transcripts stay on disk (they're large and private); this greps them with
    ripgrep and keeps the files that belong to a session apo knows about."""
    import shutil
    import subprocess

    from apo import claude_runtime

    query = (query or "").strip()
    folders = [str(d / "projects") for d in claude_runtime.claude_config_dirs() if (d / "projects").is_dir()]
    if len(query) < 3 or not folders or not shutil.which("rg"):
        return []
    pattern = ".{0,80}" + "".join("\\" + ch if ch in _RG_META else ch for ch in query) + ".{0,80}"
    try:
        result = subprocess.run(
            ["rg", "--ignore-case", "--only-matching", "--max-count", "1", "--with-filename", "--no-heading",
             "--glob", "*.jsonl", "-e", pattern, *folders],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        path, sep, excerpt = line.partition(".jsonl:")
        if sep:
            found.setdefault(Path(path).name, excerpt)
    hits = []
    for sid, excerpt in found.items():
        row = conn.execute("SELECT * FROM sessions WHERE claude_session_id = ?", (sid,)).fetchone()
        if row is None:
            continue
        s = dict(row)
        s["project_name"] = get_project_name(conn, s["project_id"]) if s["project_id"] else None
        clean = excerpt.replace("\\n", " ").replace('\\"', '"')
        hits.append({"kind": "transcript", "ref": sid, "title": _session_name(s), "status": s["status"],
                     "subkind": "transcript", "project_name": s["project_name"], "snippet": _snippet(clean, [query]),
                     "href": f"/sessions#session-card-{s['id']}", "resume_command": session_resume_command(s),
                     "last_seen_at": s["last_seen_at"]})
    hits.sort(key=lambda h: h["last_seen_at"] or "", reverse=True)
    return hits[:limit]


def waiting_for_agents(conn: sqlite3.Connection) -> list[dict]:
    """Answered-but-unapplied items and READY agent tasks across every project, each
    with its repo folder and the command that works it there."""
    import shlex

    from apo import repo_writer

    items = []
    rows = conn.execute(
        "SELECT decision_requests.id, decision_requests.status, decision_requests.kind FROM decision_requests "
        "JOIN projects ON projects.id = decision_requests.project_id WHERE projects.archived_at IS NULL AND "
        "(decision_requests.status = 'answered' OR (decision_requests.kind = 'task' AND decision_requests.status = 'open')) "
        "ORDER BY projects.name COLLATE NOCASE, decision_requests.id"
    ).fetchall()
    for row in rows:
        d = get_decision_request(conn, row["id"])
        if row["kind"] == "task" and open_blockers(conn, d["id"]):
            continue
        repository = repo_writer._resolve_repository(conn, d["repository_id"], d["project_id"])
        path = repository["local_path"] if repository else None
        items.append({
            "id": d["id"], "title": d["title"], "project": get_project_name(conn, d["project_id"]), "path": path,
            "state": "answered" if row["status"] == "answered" else "ready task",
            "command": f'cd {shlex.quote(path)} && claude "/decisions"' if path else None,
            "folders": [] if path else [f["local_path"] for f in project_repositories(conn, d["project_id"])],
        })
    return items


def rename_project(conn: sqlite3.Connection, project_id: int, new_name: str) -> None:
    """The old name stays as an alias, so anything still filing under it lands here."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("Give it a name.")
    old = get_project_name(conn, project_id)
    if old is None:
        raise ValueError("No such project.")
    if new_name == old:
        return
    clash = conn.execute("SELECT 1 FROM projects WHERE name = ? AND id != ?", (new_name, project_id)).fetchone()
    if clash:
        raise ValueError(f"There's already a project called {new_name} — merge into it instead.")
    conn.execute("UPDATE projects SET name = ? WHERE id = ?", (new_name, project_id))
    conn.execute("DELETE FROM project_aliases WHERE alias = ?", (new_name,))
    conn.commit()
    add_project_alias(conn, old, project_id)
    record_event(conn, verb="project.renamed", actor="human", source="dashboard", project_id=project_id,
                 summary=f"Renamed project '{old}' to '{new_name}'")


def _folder_inside(root: Path, folder: str) -> Path | None:
    """folder as a real directory strictly inside root, else None. Resolved, so an
    absolute path, `..` or a symlink can't reach outside root."""
    candidate = root / folder
    if not candidate.is_dir():
        return None
    resolved = candidate.resolve()
    return resolved if root.resolve() in resolved.parents else None


def project_folder_choices(conn: sqlite3.Connection, root: Path) -> list[dict]:
    """The folders a new project can take: root's immediate subfolders, each marked as
    a git repository or not and with the project that already has it, if any."""
    if not root.is_dir():
        return []
    choices = []
    for child in sorted(root.iterdir(), key=lambda c: c.name.lower()):
        path = None if child.name.startswith(".") else _folder_inside(root, child.name)
        if path is None:
            continue
        owner = find_repository_by_path(conn, str(path))
        choices.append({"name": child.name, "git": (path / ".git").exists(),
                        "project": get_project_name(conn, owner["project_id"]) if owner else None})
    return choices


def create_project(conn: sqlite3.Connection, name: str, folder: str | None, root: Path) -> sqlite3.Row:
    """A project made on the dashboard, with a folder under root if one was picked.
    The folder is checked here, not just offered by the form: root bounds what can be
    attached however the request was made."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give it a name.")
    existing = find_project(conn, name)
    if existing is not None:
        raise ValueError(f"There's already a project called {existing['name']}.")
    path = None
    if folder:
        path = _folder_inside(root, folder)
        if path is None:
            raise ValueError(f"{folder} isn't a folder in {root}.")
        owner = find_repository_by_path(conn, str(path))
        if owner is not None:
            raise ValueError(f"{path.name} already belongs to {get_project_name(conn, owner['project_id'])}.")
    project = get_or_create_project(conn, name)
    if path is not None:
        create_repository(conn, project["id"], name, str(path))
    record_event(conn, verb="project.created", actor="human", source="dashboard", project_id=project["id"],
                 summary=f"Created project '{name}'" + (f" in {path}" if path else ""))
    return project


# --- The project as a place: brief, status, work, decisions ---

PROJECT_STATUSES = ("on_track", "at_risk", "blocked", "paused", "done", "quiet")
BLOCKED_AFTER_HOURS = 48
AT_RISK_AFTER_DAYS = 5


def _parse_links(text: str | None) -> list[dict]:
    """One link per line: "Repo https://github.com/me/acme". Lines without a URL are dropped."""
    links = []
    for line in (text or "").splitlines():
        m = re.search(r"https?://\S+", line)
        if m:
            links.append({"label": line[:m.start()].strip(" -:|·") or m.group(0), "url": m.group(0)})
    return links


def get_project_brief(conn: sqlite3.Connection, project_id: int) -> dict:
    row = conn.execute("SELECT goal, done_when, links FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return {"goal": None, "done_when": None, "links": []}
    return {"goal": row["goal"], "done_when": row["done_when"], "links": _loads(row["links"]) or []}


def set_project_brief(conn: sqlite3.Connection, project_id: int, goal: str | None = None,
                      done_when: str | None = None, links: str | None = None) -> None:
    conn.execute(
        "UPDATE projects SET goal = ?, done_when = ?, links = ? WHERE id = ?",
        ((goal or "").strip() or None, (done_when or "").strip() or None, json.dumps(_parse_links(links)), project_id),
    )
    conn.commit()
    record_event(conn, verb="project.brief_updated", actor="human", source="dashboard", project_id=project_id,
                 summary="Updated the project brief")


PROJECT_PRIORITIES = ("hot", "normal", "background")
# How much a project's items weigh in Now's ranking.
_PRIORITY_WEIGHT = {"hot": 2.0, "normal": 1.0, "background": 0.5}


def project_priority(conn: sqlite3.Connection, project_id: int) -> str:
    row = conn.execute("SELECT priority FROM projects WHERE id = ?", (project_id,)).fetchone()
    return (row["priority"] if row else None) or "normal"


def set_project_priority(conn: sqlite3.Connection, project_id: int, priority: str) -> None:
    if priority not in PROJECT_PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PROJECT_PRIORITIES)}")
    conn.execute("UPDATE projects SET priority = ? WHERE id = ?", (None if priority == "normal" else priority, project_id))
    conn.commit()
    record_event(conn, verb="project.priority", actor="human", project_id=project_id, importance=1,
                 summary=f"Priority set to {priority}")


# Autonomy → the claude --permission-mode it runs background sessions with.
AUTONOMY_MODES = {"ask": None, "edits": "acceptEdits", "plan": "plan"}
AUTONOMY_LABELS = {"ask": "ask first", "edits": "accept file edits", "plan": "plan only"}


def project_autonomy(conn: sqlite3.Connection, project_id: int) -> str:
    row = conn.execute("SELECT autonomy FROM projects WHERE id = ?", (project_id,)).fetchone()
    return (row["autonomy"] if row else None) or "ask"


def set_project_autonomy(conn: sqlite3.Connection, project_id: int, autonomy: str) -> None:
    if autonomy not in AUTONOMY_MODES:
        raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY_MODES)}")
    conn.execute("UPDATE projects SET autonomy = ? WHERE id = ?", (None if autonomy == "ask" else autonomy, project_id))
    conn.commit()
    record_event(conn, verb="project.autonomy", actor="human", project_id=project_id, importance=1,
                 summary=f"Background agents: {AUTONOMY_LABELS[autonomy]}")


def permission_mode_for_path(conn: sqlite3.Connection, path: str) -> str | None:
    """The permission mode a background session started in `path` gets: its project's."""
    repository = find_repository_by_path(conn, path)
    return AUTONOMY_MODES[project_autonomy(conn, repository["project_id"])] if repository else None


def list_standing_rules(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM standing_rules WHERE project_id = ? AND retired_at IS NULL ORDER BY id", (project_id,))]


def add_standing_rule(conn: sqlite3.Connection, project_id: int, text: str,
                      source_decision_id: int | None = None) -> int:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Write the rule first.")
    cur = conn.execute("INSERT INTO standing_rules (project_id, text, source_decision_id, created_at) VALUES (?, ?, ?, ?)",
                       (project_id, text, source_decision_id, _now()))
    conn.commit()
    record_event(conn, verb="rule.added", actor="human", project_id=project_id, decision_id=source_decision_id,
                 importance=1, summary=f"Standing rule: {_one_line(text, 160)}")
    _write_policies(conn, project_id)
    return cur.lastrowid


def retire_standing_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    row = conn.execute("SELECT project_id, text FROM standing_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        return
    conn.execute("UPDATE standing_rules SET retired_at = ? WHERE id = ? AND retired_at IS NULL", (_now(), rule_id))
    conn.commit()
    record_event(conn, verb="rule.retired", actor="human", project_id=row["project_id"], importance=1,
                 summary=f"Retired the standing rule: {_one_line(row['text'], 160)}")
    _write_policies(conn, row["project_id"])


def _write_policies(conn: sqlite3.Connection, project_id: int) -> None:
    from apo import repo_writer

    rules = list_standing_rules(conn, project_id)
    name = get_project_name(conn, project_id)
    for repository in project_repositories(conn, project_id):
        try:
            repo_writer.write_policy(repository["local_path"], name, rules)
        except OSError as exc:
            record_sync_error(conn, "repo_writer", None, f"POLICY.md in {repository['local_path']}: {exc}")


RULE_SUGGEST_MIN = 5
RULE_SUGGEST_RATE = 0.8
RULE_SUGGESTION = ("When you're at least 80% confident and the choice is easy to reverse, go with your "
                   "recommendation and record your reasoning instead of filing a decision. File one for anything "
                   "that's hard to undo.")


def rule_suggestion(conn: sqlite3.Connection, project_id: int, days: int = 60) -> dict | None:
    """A rule worth making: you accepted the agent's recommendation (or told it to use its
    judgment) on most of this project's recent decisions. None once that rule exists."""
    if any(r["text"] == RULE_SUGGESTION for r in list_standing_rules(conn, project_id)):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS answered, SUM(answer_kind IN ('recommendation', 'delegate')) AS accepted "
        "FROM decision_requests WHERE project_id = ? AND kind = 'decision' AND requested_by = 'agent' "
        "AND answered_at >= ? AND COALESCE(answer_source, '') != 'archive'", (project_id, since),
    ).fetchone()
    answered, accepted = row["answered"] or 0, row["accepted"] or 0
    if answered < RULE_SUGGEST_MIN or accepted / answered < RULE_SUGGEST_RATE:
        return None
    return {"answered": answered, "accepted": accepted, "days": days, "text": RULE_SUGGESTION}


def _due_date(due_on: str | None) -> str | None:
    """YYYY-MM-DD, or None to clear. Anything else is refused rather than guessed."""
    if due_on is None or not str(due_on).strip():
        return None
    try:
        return date.fromisoformat(str(due_on).strip()).isoformat()
    except ValueError:
        raise ValueError(f"A due date is YYYY-MM-DD, not {due_on!r}") from None


def set_due(conn: sqlite3.Connection, item_id: int, due_on: str | None) -> None:
    conn.execute("UPDATE decision_requests SET due_on = ? WHERE id = ?", (_due_date(due_on), item_id))
    conn.commit()


def add_note(conn: sqlite3.Connection, item_id: int, body: str, author: str = "human") -> int:
    """A comment on an item. author is "human", "agent" or an agent's session id. The
    repo file is rewritten so the next session working the item reads it."""
    body = (body or "").strip()
    if not body:
        raise ValueError("Write the note first.")
    if conn.execute("SELECT 1 FROM decision_requests WHERE id = ?", (item_id,)).fetchone() is None:
        raise ValueError(f"No item #{item_id}.")
    author = (author or "human").strip() or "human"
    cursor = conn.execute("INSERT INTO item_notes (item_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                          (item_id, author, body, _now()))
    conn.commit()
    _item_event(conn, item_id, "item.noted", "Note added", actor="human" if author == "human" else "agent",
                detail=body, importance=1 if author == "human" else 0)
    _sync_repo_files(conn, item_id)
    return cursor.lastrowid


def list_notes(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM item_notes WHERE item_id = ? ORDER BY id", (item_id,))]


LABEL_MAX = 32


def normalize_label(label: str | None) -> str:
    label = re.sub(r"\s+", "-", (label or "").strip().lower())
    if not label or "," in label:
        raise ValueError("A label is a word or two, without commas.")
    if len(label) > LABEL_MAX:
        raise ValueError(f"A label is at most {LABEL_MAX} characters.")
    return label


def parse_labels(raw: str | list | None) -> list[str]:
    """Comma-separated text (the New work field) or a list, normalized and de-duplicated."""
    parts = raw if isinstance(raw, list) else (raw or "").split(",")
    out: list[str] = []
    for part in parts:
        if str(part).strip():
            label = normalize_label(part)
            if label not in out:
                out.append(label)
    return out


def _item_labels(conn: sqlite3.Connection, item_id: int) -> list[str]:
    row = conn.execute("SELECT labels FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"No item #{item_id}.")
    return _loads(row["labels"]) or []


def add_label(conn: sqlite3.Connection, item_id: int, label: str, event: bool = True) -> bool:
    label = normalize_label(label)
    labels = _item_labels(conn, item_id)
    if label in labels:
        return False
    conn.execute("UPDATE decision_requests SET labels = ? WHERE id = ?", (json.dumps([*labels, label]), item_id))
    conn.commit()
    if event:
        _item_event(conn, item_id, "item.labelled", "Labelled", actor="human", detail=label, importance=0)
    return True


def remove_label(conn: sqlite3.Connection, item_id: int, label: str) -> bool:
    label = normalize_label(label)
    labels = _item_labels(conn, item_id)
    if label not in labels:
        return False
    conn.execute("UPDATE decision_requests SET labels = ? WHERE id = ?",
                 (json.dumps([x for x in labels if x != label]), item_id))
    conn.commit()
    _item_event(conn, item_id, "item.unlabelled", "Label removed", actor="human", detail=label, importance=0)
    return True


def known_labels(conn: sqlite3.Connection, project_id: int | None = None) -> list[str]:
    """Every label in use, for the filter bar."""
    where, params = ("WHERE project_id = ?", (project_id,)) if project_id is not None else ("", ())
    seen: set[str] = set()
    for row in conn.execute(f"SELECT labels FROM decision_requests {where}", params):
        seen.update(_loads(row["labels"]) or [])
    return sorted(seen)


ITEM_STATUSES = ("open", "in_progress", "answered", "resolved", "cancelled")
WORK_STATUSES = {"needs_you": "Needs you", "ready": "Ready for an agent", "in_progress": "In progress",
                 "waiting": "Waiting", "done": "Done"}
OWNER_FILTERS = {"me": "you", "agent": "agent", "external": "someone else"}


def filter_work(work: dict, label: str = "", owner: str = "", status: str = "") -> dict:
    """project_work narrowed to a label, an owner (me/agent/external) and a work status
    (one of WORK_STATUSES). Blank means no filter; an unknown value matches nothing."""
    if not (label or owner or status):
        return work
    label = label.strip().lower()

    def keep(i: dict) -> bool:
        return (not label or label in i["labels"]) and (not owner or i["owner"] == OWNER_FILTERS.get(owner))

    out = {key: [i for i in work[key] if keep(i)] if not status or status == key else [] for key in WORK_STATUSES}
    live = [i for key in ("needs_you", "ready", "in_progress", "waiting") for i in out[key]]
    out["overdue"] = [i for i in live if i["due_bucket"] == "overdue"]
    out["due_soon"] = [i for i in live if i["due_bucket"] == "soon"]
    out["stuck"] = [i for i in live if i["stuck"]]
    return out


def set_outcome_due(conn: sqlite3.Connection, outcome_id: int, due_on: str | None) -> None:
    conn.execute("UPDATE outcomes SET due_on = ?, updated_at = ? WHERE id = ?", (_due_date(due_on), _now(), outcome_id))
    conn.commit()


def _overdue_count(conn: sqlite3.Connection, project_id: int, today: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM decision_requests WHERE project_id = ? AND due_on < ? "
        "AND status IN ('open', 'in_progress', 'answered')", (project_id, today),
    ).fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE project_id = ? AND due_on < ? AND status IN ('active', 'paused')",
        (project_id, today),
    ).fetchone()[0]


def set_project_status(conn: sqlite3.Connection, project_id: int, status: str | None, note: str | None = None) -> None:
    """None goes back to the suggested status."""
    if status is not None and status not in PROJECT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(PROJECT_STATUSES)}")
    conn.execute(
        "UPDATE projects SET status_override = ?, status_note = ?, status_set_at = ? WHERE id = ?",
        (status, ((note or "").strip() or None) if status else None, _now() if status else None, project_id),
    )
    conn.commit()
    record_event(conn, verb="project.status", actor="human", source="dashboard", project_id=project_id,
                 summary=f"Status set to {status.replace('_', ' ')}" if status else "Status back to automatic")


def _age_words(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours < 1:
        return f"{max(1, int(seconds // 60))} minutes"
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _suggest_status(conn: sqlite3.Connection, project_id: int, now: datetime) -> tuple[str, str]:
    oldest = conn.execute(
        "SELECT id, created_at FROM decision_requests WHERE project_id = ? AND status = 'open' "
        "AND COALESCE(assignee, 'human') = 'human' AND kind IN ('decision', 'action', 'review') "
        "ORDER BY created_at LIMIT 1",
        (project_id,),
    ).fetchone()
    if oldest:
        waited = (now - datetime.fromisoformat(oldest["created_at"])).total_seconds()
        if waited >= BLOCKED_AFTER_HOURS * 3600:
            return "blocked", f"#{oldest['id']} has waited on you {_age_words(waited)}"
    overdue = _overdue_count(conn, project_id, now.astimezone().date().isoformat())
    if overdue:
        return "at_risk", f"{overdue} overdue"
    open_work = conn.execute(
        "SELECT COUNT(*) FROM decision_requests WHERE project_id = ? AND status IN ('open', 'in_progress', 'answered')",
        (project_id,),
    ).fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE project_id = ? AND status = 'active'", (project_id,),
    ).fetchone()[0]
    if not open_work:
        return "quiet", "Nothing open"
    last = max(filter(None, (
        conn.execute("SELECT MAX(occurred_at) FROM events WHERE project_id = ?", (project_id,)).fetchone()[0],
        conn.execute("SELECT MAX(last_seen_at) FROM sessions WHERE project_id = ?", (project_id,)).fetchone()[0],
        conn.execute("SELECT created_at FROM projects WHERE id = ?", (project_id,)).fetchone()[0],
    )))
    idle = (now - datetime.fromisoformat(last)).total_seconds()
    if idle >= AT_RISK_AFTER_DAYS * 86400:
        return "at_risk", f"Nothing has moved for {_age_words(idle)} with {open_work} open"
    return "on_track", f"{open_work} open · last activity {_age_words(idle)} ago"


def project_status(conn: sqlite3.Connection, project_id: int, now: datetime | None = None) -> dict:
    """Suggested from what's going on; yours instead when you've set one."""
    suggested, reason = _suggest_status(conn, project_id, now or datetime.now(timezone.utc))
    row = conn.execute(
        "SELECT status_override, status_note, status_set_at FROM projects WHERE id = ?", (project_id,),
    ).fetchone()
    if row is not None and row["status_override"]:
        return {"value": row["status_override"], "overridden": True, "note": row["status_note"],
                "set_at": row["status_set_at"], "suggested": suggested, "reason": reason}
    return {"value": suggested, "overridden": False, "note": None, "set_at": None, "suggested": suggested,
            "reason": reason}


DUE_SOON_DAYS = 2
STUCK_ITEM_DAYS = 3  # days-scale cousin of STALE_MINUTES: work this old with no movement is flagged


def due_bucket(due_on: str | None, today: date | None = None) -> str | None:
    """'overdue' once the day has passed, 'soon' from today through DUE_SOON_DAYS out."""
    if not due_on:
        return None
    days = (date.fromisoformat(due_on) - (today or datetime.now().astimezone().date())).days
    return "overdue" if days < 0 else "soon" if days <= DUE_SOON_DAYS else None


def stuck_info(since: str | None, reason: str, now: datetime | None = None) -> dict | None:
    if not since:
        return None
    days = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(since)).days
    return {"days": days, "reason": reason} if days >= STUCK_ITEM_DAYS else None


def sort_work_by_due(work: dict) -> dict:
    """Each group by due date, soonest first, undated last (stable, so the rest keeps its order)."""
    return {key: sorted(items, key=lambda i: (i.get("due_on") is None, i.get("due_on") or ""))
            if key in _WORK_GROUPS else items for key, items in work.items()}


_WORK_GROUPS = ("needs_you", "ready", "in_progress", "waiting", "done")
_OWNER_WORDS = {"human": "you", "agent": "agent", "external": "someone else"}


def project_work(conn: sqlite3.Connection, project_id: int | None) -> dict:
    """Everything in the project (None: every active project), whoever holds it, by
    where it stands: needs_you, ready (an agent's to pick up), in_progress, waiting
    (on something else), done (this week)."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    groups: dict[str, list] = {"needs_you": [], "ready": [], "in_progress": [], "waiting": [], "done": []}
    scope, params = ("decision_requests.project_id = ?", [project_id]) if project_id is not None else ("projects.archived_at IS NULL", [])
    rows = conn.execute(
        "SELECT decision_requests.id, projects.name AS project_name FROM decision_requests "
        "JOIN projects ON projects.id = decision_requests.project_id "
        f"WHERE {scope} AND (decision_requests.status IN ('open', 'in_progress', 'answered') "
        "OR (decision_requests.status IN ('resolved', 'cancelled') "
        "AND COALESCE(decision_requests.resolved_at, decision_requests.answered_at, decision_requests.created_at) >= ?)) "
        "ORDER BY decision_requests.work_rank IS NULL, decision_requests.work_rank, "
        "COALESCE(decision_requests.sort_order, 0), decision_requests.id",
        (*params, week_ago),
    ).fetchall()
    for row in rows:
        d = get_decision_request(conn, row["id"])
        owner = _OWNER_WORDS.get(d.get("assignee") or "human", "you")
        item = {"id": d["id"], "kind": d["kind"], "title": d["title"], "owner": owner, "labels": d.get("labels") or [],
                "note_count": d.get("note_count") or 0, "since": d["created_at"], "href": f"/decisions/{d['id']}", "summary": d.get("summary"),
                "project_id": d["project_id"], "project_name": row["project_name"], "due_on": d.get("due_on")}
        status = d["status"]
        item["due_bucket"] = None if status in ("resolved", "cancelled") else due_bucket(d.get("due_on"))
        item["stuck"] = None
        if status in ("resolved", "cancelled"):
            partial = (d.get("completion") or {}).get("outcome") == "partial"
            effort = task_effort(conn, d["id"], tokens=False) if status == "resolved" else None
            groups["done"].append({**item, "state": "Cancelled" if status == "cancelled" else ("Partly done" if partial else "Done"),
                                   "since": d.get("resolved_at") or d.get("answered_at") or d["created_at"],
                                   "effort": effort, "effort_text": effort_text(effort)})
        elif status == "answered":
            groups["in_progress"].append({**item, "owner": "agent", "state": "Answered",
                                          "since": d.get("answered_at")})
        elif status == "in_progress":
            since = d.get("claimed_at") or d["created_at"]
            groups["in_progress"].append({**item, "state": "Claimed", "since": since,
                                          "stuck": stuck_info(since, "claimed with no activity")})
        elif open_blockers(conn, d["id"]):
            groups["waiting"].append({**item, "state": "Waits on " + ", ".join(f"#{b}" for b in open_blockers(conn, d["id"]))})
        elif d["kind"] == "wait":
            due = d.get("follow_up_at") and d["follow_up_at"] <= _now()
            target = "needs_you" if due else "waiting"
            groups[target].append({**item, "state": "Check on it" if due else f"Waiting on {d.get('waiting_on') or 'someone else'}"})
        elif owner == "agent":
            groups["ready"].append({**item, "state": "Ready", "stuck": stuck_info(
                d["created_at"], "ready, but no agent has picked it up")})
        else:
            state = {"decision": "Decide", "action": "Do it", "review": "Review"}.get(d["kind"], "Yours")
            if d.get("snoozed_until") or d.get("snooze_condition"):
                state = "Snoozed"
            groups["needs_you"].append({**item, "state": state, "stuck": None if state == "Snoozed" else stuck_info(
                d["created_at"], "waiting on you")})
    groups["done"].sort(key=lambda i: i["since"] or "", reverse=True)
    live = [i for key in ("needs_you", "ready", "in_progress", "waiting") for i in groups[key]]
    groups["overdue"] = [i for i in live if i["due_bucket"] == "overdue"]
    groups["due_soon"] = [i for i in live if i["due_bucket"] == "soon"]
    groups["stuck"] = [i for i in live if i["stuck"]]
    return groups


def add_token_usage(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Fill in the tokens on project_work's done rows. Reading a transcript is the slow
    part of an item's effort, so only the pages that show it ask."""
    for item in items:
        if item.get("effort"):
            item["effort"] = task_effort(conn, item["id"]) or item["effort"]
            item["effort_text"] = effort_text(item["effort"])


def decision_log(conn: sqlite3.Connection, project_id: int | None, limit: int = 200) -> list[dict]:
    """What was decided in the project (None: every active project), newest first —
    so nobody decides it twice."""
    scope, params = ("d.project_id = ?", [project_id]) if project_id is not None else ("projects.archived_at IS NULL", [])
    rows = conn.execute(
        "SELECT d.id, d.kind, d.title, d.question, d.summary, d.answer_text, d.answer_source, d.answered_at, d.status, "
        "d.project_id, projects.name AS project_name FROM decision_requests AS d JOIN projects ON projects.id = d.project_id "
        f"WHERE {scope} AND d.kind IN ('decision', 'action') AND d.answer_text IS NOT NULL "
        "ORDER BY d.answered_at DESC, d.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def set_project_pinned(conn: sqlite3.Connection, project_id: int, pinned: bool) -> None:
    conn.execute("UPDATE projects SET pinned_at = ? WHERE id = ?", (_now() if pinned else None, project_id))
    conn.commit()


DORMANT_DAYS = 14
# Worst first: a project's health is the first of these that any of its items is in.
PROJECT_HEALTH = ("problem", "your_move", "review", "in_transit", "working", "at_rest", "idle", "dormant")
_HEALTH_LANES = (("problems", "problems"), ("your_move", "you"), ("asks", "asks"), ("in_transit", "transit"),
                 ("review", "review"))


def project_overview(conn: sqlite3.Connection, include_archived: bool = False, queue: dict | None = None) -> list[dict]:
    """One row per project: health, counts, oldest open item, last activity.
    Dormant is computed, never set: nothing open, nothing running, and no activity
    for DORMANT_DAYS — anything new wakes it. Pinned first, then worst health."""
    queue = queue if queue is not None else now_queue(conn)
    where = "" if include_archived else " WHERE archived_at IS NULL"
    rows = conn.execute(
        "SELECT projects.*, (SELECT local_path FROM repositories WHERE project_id = projects.id ORDER BY id LIMIT 1) AS path, "
        "(SELECT MAX(occurred_at) FROM events WHERE project_id = projects.id) AS last_event_at, "
        "(SELECT MAX(last_seen_at) FROM sessions WHERE project_id = projects.id) AS last_session_at, "
        "(SELECT COUNT(*) FROM outcomes WHERE project_id = projects.id AND status = 'active') AS active_outcomes "
        f"FROM projects{where}"
    ).fetchall()
    by_name: dict[str, dict] = {}
    for r in rows:
        by_name[r["name"]] = {
            "id": r["id"], "name": r["name"], "path": r["path"], "pinned": bool(r["pinned_at"]),
            "archived": bool(r["archived_at"]), "active_outcomes": r["active_outcomes"],
            "counts": {"problems": 0, "you": 0, "asks": 0, "transit": 0, "review": 0, "working": 0, "at_rest": 0},
            "oldest_open": None,
            "last_activity": max(filter(None, (r["last_event_at"], r["last_session_at"], r["created_at"]))),
        }
    for lane, key in _HEALTH_LANES:
        for item in queue[lane]:
            project = by_name.get(item.get("project_name"))
            if project is None:
                continue
            project["counts"][key] += 1
            if lane in ("your_move", "asks") and item.get("since"):
                project["oldest_open"] = min(filter(None, (project["oldest_open"], item["since"])))
    ids = {p["id"]: p for p in by_name.values()}
    for s in list_sessions(conn):
        project = ids.get(s.get("project_id"))
        if project is None or s["ghost"] or s["status"] == "ended":
            continue
        project["counts"]["working" if s["exec_state"] == "working" else "at_rest"] += 1

    dormant_before = (datetime.now(timezone.utc) - timedelta(days=DORMANT_DAYS)).isoformat()
    for project in by_name.values():
        c = project["counts"]
        if c["problems"]:
            health = "problem"
        elif c["you"]:
            health = "your_move"
        elif c["review"]:
            health = "review"
        elif c["transit"] or c["asks"]:
            health = "in_transit"
        elif c["working"]:
            health = "working"
        elif c["at_rest"]:
            health = "at_rest"
        elif project["last_activity"] < dormant_before and not project["active_outcomes"]:
            health = "dormant"
        else:
            health = "idle"
        project["health"] = health
        parts = [(c["problems"], "problem", "problems"), (c["you"], "you", "you"), (c["review"], "review", "review"),
                 (c["transit"], "in transit", "in transit"), (c["asks"], "ask pending", "asks pending"),
                 (c["working"], "working", "working")]
        project["counts_line"] = " · ".join(f"{n} {one if n == 1 else many}" for n, one, many in parts if n)
    return sorted(by_name.values(), key=lambda p: (
        not p["pinned"], PROJECT_HEALTH.index(p["health"]), _desc(p["last_activity"]),
    ))


def _desc(iso_ts: str) -> tuple:
    return tuple(-ord(ch) for ch in iso_ts)


def create_repository(conn: sqlite3.Connection, project_id: int, name: str, local_path: str) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO repositories (project_id, name, local_path, created_at) VALUES (?, ?, ?, ?)",
        (project_id, name, local_path, _now()),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM repositories WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone()


def create_decision_request(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    title: str,
    question: str,
    repository_id: int | None = None,
    summary: str | None = None,
    context: str | None = None,
    why_it_matters: str | None = None,
    options: list | None = None,
    blocked_work: list | None = None,
    safe_work: list | None = None,
    recommendation: str | None = None,
    confidence: int | None = None,
    resume_plan: dict | None = None,
    origin_session_id: str | None = None,
    kind: str = "decision",
    recommended_option: int | None = None,
    requested_by: str = "agent",
    outcome_id: int | None = None,
    waiting_on: str | None = None,
    follow_up_at: str | None = None,
    supersedes_id: int | None = None,
    blocked_by: list[int] | None = None,
    action_spec: dict | None = None,
    est_minutes: int | None = None,
) -> int:
    if kind not in _ASSIGNEE:
        raise ValueError(f"kind must be one of {', '.join(_ASSIGNEE)}, got {kind!r}")
    if recommended_option is None:
        recommended_option = _recommended_from_labels(options)
    summary_derived = 0
    if not (summary and summary.strip()):
        summary, summary_derived = _first_sentence(question), 1
    sort_order = None
    if outcome_id is not None:
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM decision_requests WHERE outcome_id = ?", (outcome_id,)
        ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO decision_requests
            (project_id, repository_id, title, question, summary, context, why_it_matters,
             options, blocked_work, safe_work, recommendation, confidence,
             origin_session_id, kind, recommended_option, summary_derived, assignee, requested_by,
             outcome_id, sort_order, waiting_on, follow_up_at, supersedes_id, action_spec, est_minutes,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            project_id, repository_id, title, question, summary, context, why_it_matters,
            _dumps(options), _dumps(blocked_work), _dumps(safe_work),
            recommendation, _normalize_confidence(confidence), origin_session_id, kind,
            recommended_option, summary_derived, _ASSIGNEE[kind], requested_by,
            outcome_id, sort_order, waiting_on, follow_up_at, supersedes_id, _dumps(action_spec), est_minutes, _now(),
        ),
    )
    decision_id = cur.lastrowid
    for blocker_id in blocked_by or []:
        add_dependency(conn, decision_id, blocker_id, commit=False)

    if resume_plan:
        conn.execute(
            """
            INSERT INTO resume_plans
                (decision_request_id, objective, files, previous_reasoning,
                 remaining_steps, expected_outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                resume_plan.get("objective"),
                _dumps(resume_plan.get("files")),
                resume_plan.get("previous_reasoning"),
                _dumps(resume_plan.get("remaining_steps")),
                resume_plan.get("expected_outcome"),
                _now(),
            ),
        )

    conn.commit()
    _record_event(conn, decision_id, "created")
    if origin_session_id and _ASSIGNEE[kind] == "human":
        _mark_session_blocked(conn, origin_session_id, decision_id)
    # Linear mirrors what agents ask of the human; outcome steps, agent tasks and
    # the human's own reminders would only be noise there.
    if kind in ("decision", "action") and requested_by == "agent":
        _push_to_sources(conn, decision_id)
    if supersedes_id:
        cancel_request(conn, supersedes_id, f"Superseded by #{decision_id}")
    _sync_repo_files(conn, decision_id)
    return decision_id


# Who owns the move on each kind of work item.
_ASSIGNEE = {"decision": "human", "action": "human", "review": "human", "task": "agent", "wait": "external"}
_OWNER_KIND = {"me": "action", "agent": "task", "external": "wait"}
_KIND_OWNER = {"action": "me", "decision": "me", "review": "me", "task": "agent", "wait": "external"}
_DONE_STATUSES = ("answered", "resolved", "cancelled")


def create_task(
    conn: sqlite3.Connection, *, project_id: int, title: str, detail: str | None = None,
    repository_id: int | None = None, outcome_id: int | None = None, requested_by: str = "human",
    origin_session_id: str | None = None, blocked_by: list[int] | None = None,
) -> int:
    """Work for an agent. It reaches one through the repo projection (a READY row) and
    the MCP list_tasks/claim_task/complete_task tools."""
    return create_decision_request(
        conn, project_id=project_id, title=title, question=detail or title, summary=detail or title,
        repository_id=repository_id, kind="task", requested_by=requested_by, outcome_id=outcome_id,
        origin_session_id=origin_session_id, blocked_by=blocked_by,
    )


BUG_SEVERITIES = ("low", "normal", "urgent")


def report_bug(
    conn: sqlite3.Connection, project_id: int, title: str, happened: str | None = None,
    expected: str | None = None, steps: str | None = None, severity: str = "normal", reported_by: str = "human",
    repository_id: int | None = None, due_on: str | None = None, outcome_id: int | None = None,
) -> int:
    """A bug is work for the project's agents: a task (READY in the repo's INDEX.md,
    so the next session or /decisions picks it up) labelled "bug"."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Say what's wrong first.")
    severity = severity if severity in BUG_SEVERITIES else "normal"
    parts = []
    if happened and happened.strip():
        parts.append(f"What happened: {happened.strip()}")
    if expected and expected.strip():
        parts.append(f"Expected: {expected.strip()}")
    if steps and steps.strip():
        parts.append("Steps to reproduce:\n" + steps.strip())
    summary = ((happened or "").strip().splitlines() or [title])[0][:300]
    if repository_id is None:
        repository = get_repository_for_project(conn, project_id)
        repository_id = repository["id"] if repository else None
    bug_id = create_decision_request(
        conn, project_id=project_id, title=title, question="\n\n".join(parts) or title, summary=summary,
        repository_id=repository_id, kind="task", requested_by=reported_by, outcome_id=outcome_id,
    )
    labels = ["bug"] + ([severity] if severity != "normal" else [])
    conn.execute("UPDATE decision_requests SET labels = ?, due_on = ? WHERE id = ?",
                 (json.dumps(labels), _due_date(due_on), bug_id))
    conn.commit()
    return bug_id


WORK_KINDS = ("task", "bug", "me")


def add_work(
    conn: sqlite3.Connection, project_id: int, kind: str, title: str, detail: str | None = None,
    expected: str | None = None, steps: str | None = None, severity: str = "normal",
    outcome_id: int | None = None, due_on: str | None = None, repository_id: int | None = None,
    labels: str | list | None = None,
) -> int:
    """The Work tab's New work: a task for an agent, a bug (a task with its report), or
    an action for you. Everything is checked before anything is written."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Say what needs doing first.")
    if kind not in WORK_KINDS:
        raise ValueError(f"kind must be one of {', '.join(WORK_KINDS)}, got {kind!r}")
    due = _due_date(due_on)
    extra_labels = parse_labels(labels)
    if outcome_id is not None:
        outcome = get_outcome(conn, outcome_id)
        if outcome is None or outcome["project_id"] != project_id:
            raise ValueError("That goal isn't in this project.")
    detail = (detail or "").strip() or None
    if kind == "bug":
        bug_id = report_bug(conn, project_id, title, happened=detail, expected=expected, steps=steps, severity=severity,
                            repository_id=repository_id, due_on=due, outcome_id=outcome_id)
        for label in extra_labels:
            add_label(conn, bug_id, label, event=False)
        return bug_id
    if kind == "me":
        item_id = create_decision_request(conn, project_id=project_id, title=title, question=detail or title,
                                          repository_id=repository_id, kind="action", requested_by="human",
                                          outcome_id=outcome_id)
    else:
        item_id = create_task(conn, project_id=project_id, title=title, detail=detail, repository_id=repository_id,
                              outcome_id=outcome_id)
    if due:
        set_due(conn, item_id, due)
    for label in extra_labels:
        add_label(conn, item_id, label, event=False)
    return item_id


def _would_cycle(conn: sqlite3.Connection, blocked_id: int, blocker_id: int) -> bool:
    """True if blocker already (transitively) waits on blocked."""
    frontier, seen = [blocker_id], set()
    while frontier:
        current = frontier.pop()
        if current == blocked_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(r[0] for r in conn.execute(
            "SELECT blocker_id FROM work_item_deps WHERE blocked_id = ?", (current,)
        ))
    return False


def add_dependency(conn: sqlite3.Connection, blocked_id: int, blocker_id: int, commit: bool = True) -> None:
    if blocked_id == blocker_id or _would_cycle(conn, blocked_id, blocker_id):
        raise ValueError(f"#{blocked_id} can't wait on #{blocker_id}: that would make a cycle")
    conn.execute(
        "INSERT OR IGNORE INTO work_item_deps (blocked_id, blocker_id, created_at) VALUES (?, ?, ?)",
        (blocked_id, blocker_id, _now()),
    )
    if commit:
        conn.commit()


def open_blockers(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """Items this one waits on that aren't done. A human's answer counts as done for
    this: the human's part is finished even before an agent applies it."""
    placeholders = ",".join("?" * len(_DONE_STATUSES))
    return [r[0] for r in conn.execute(
        f"SELECT work_item_deps.blocker_id FROM work_item_deps JOIN decision_requests "
        f"ON decision_requests.id = work_item_deps.blocker_id WHERE work_item_deps.blocked_id = ? "
        f"AND decision_requests.status NOT IN ({placeholders}) ORDER BY work_item_deps.blocker_id",
        (item_id, *_DONE_STATUSES),
    )]


_RANKABLE_GROUPS = ("needs_you", "ready", "in_progress", "waiting")


def move_rank(conn: sqlite3.Connection, item_id: int, direction: str) -> None:
    """Reorder an item within the work group it sits in (up, down, or top = "do this
    next"). The group's current order is written out as ranks 1..n first, so items that
    had no rank keep their place around the one that moved."""
    if direction not in ("up", "down", "top"):
        raise ValueError(f"Unknown direction: {direction}")
    item = get_decision_request(conn, item_id)
    if item is None:
        raise ValueError(f"No item #{item_id}")
    work = project_work(conn, item["project_id"])
    group = next(([i["id"] for i in work[key]] for key in _RANKABLE_GROUPS
                  if any(i["id"] == item_id for i in work[key])), None)
    if group is None:
        raise ValueError("Only work that is still open can be reordered.")
    order = list(group)
    at = order.index(item_id)
    if direction == "top":
        order.insert(0, order.pop(at))
    else:
        to = at - 1 if direction == "up" else at + 1
        if 0 <= to < len(order):
            order[at], order[to] = order[to], order[at]
    if order == group:
        return
    for rank, member in enumerate(order, 1):
        conn.execute("UPDATE decision_requests SET work_rank = ? WHERE id = ?", (rank, member))
    conn.commit()
    synced = set()
    for member in order:
        repository_id = get_decision_request(conn, member)["repository_id"]
        if repository_id not in synced:
            synced.add(repository_id)
            _sync_repo_files(conn, member)


def item_dependencies(conn: sqlite3.Connection, item_id: int) -> dict:
    """What this item is blocked by and what it blocks, each with whether it is done."""
    def rows(sql: str) -> list[dict]:
        return [{"id": r["id"], "title": r["title"], "status": r["status"], "done": r["status"] in _DONE_STATUSES}
                for r in conn.execute(sql, (item_id,))]
    return {
        "blockers": rows("SELECT d.id, d.title, d.status FROM work_item_deps w JOIN decision_requests d "
                         "ON d.id = w.blocker_id WHERE w.blocked_id = ? ORDER BY d.id"),
        "dependents": rows("SELECT d.id, d.title, d.status FROM work_item_deps w JOIN decision_requests d "
                           "ON d.id = w.blocked_id WHERE w.blocker_id = ? ORDER BY d.id"),
    }


def dependency_candidates(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """Unfinished items in the same project this one could wait on: not itself, not
    already a blocker, and nothing that would make a cycle."""
    item = get_decision_request(conn, item_id)
    if item is None:
        return []
    linked = {b["id"] for b in item_dependencies(conn, item_id)["blockers"]}
    placeholders = ",".join("?" * len(_DONE_STATUSES))
    rows = conn.execute(
        f"SELECT id, title FROM decision_requests WHERE project_id = ? AND id != ? "
        f"AND status NOT IN ({placeholders}) ORDER BY id", (item["project_id"], item_id, *_DONE_STATUSES),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"]} for r in rows
            if r["id"] not in linked and not _would_cycle(conn, item_id, r["id"])]


def link_blocker(conn: sqlite3.Connection, blocked_id: int, blocker_id: int, by: str = "human") -> None:
    """Make blocked_id wait on blocker_id. WAITING/READY still follow from
    open_blockers; this only edits the link and refreshes the repo projection."""
    blocked, blocker = get_decision_request(conn, blocked_id), get_decision_request(conn, blocker_id)
    if blocked is None or blocker is None:
        raise ValueError("No such item.")
    if blocked["project_id"] != blocker["project_id"]:
        raise ValueError("Items can only wait on others in the same project.")
    if blocked["status"] in ("resolved", "cancelled"):
        raise ValueError(f"#{blocked_id} is finished.")
    add_dependency(conn, blocked_id, blocker_id)
    _item_event(conn, blocked_id, "item.blocked", f"Now waits on #{blocker_id}", actor=by, importance=0)
    _sync_repo_files(conn, blocked_id)


def remove_dependency(conn: sqlite3.Connection, blocked_id: int, blocker_id: int, by: str = "human") -> None:
    cur = conn.execute("DELETE FROM work_item_deps WHERE blocked_id = ? AND blocker_id = ?", (blocked_id, blocker_id))
    conn.commit()
    if cur.rowcount:
        _item_event(conn, blocked_id, "item.unblocked", f"No longer waits on #{blocker_id}", actor=by, importance=0)
        _sync_repo_files(conn, blocked_id)


def _record_attempt(conn: sqlite3.Connection, task_id: int, event: str, *, session_id: str | None = None,
                    actor: str | None = None, outcome: str | None = None, note: str | None = None,
                    detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO task_attempts (task_id, event, session_id, actor, at, outcome, note, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, event, session_id, actor, _now(), outcome, note, json.dumps(detail) if detail else None),
    )
    conn.commit()


def list_attempts(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """A task's claims, completions, releases and retries, oldest first."""
    attempts = []
    for row in conn.execute("SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id", (task_id,)):
        attempt = dict(row)
        attempt["detail"] = _loads(attempt["detail"])
        attempts.append(attempt)
    return attempts


def task_effort(conn: sqlite3.Connection, task_id: int, tokens: bool = True) -> dict | None:
    """How long a finished task took, last claim to final completion, and (tokens=True)
    what its session spent in that span, read from the session's transcript when it
    can be found. None for anything unclaimed or unfinished; tokens None when unknown."""
    row = conn.execute("SELECT status, claimed_at, resolved_at FROM decision_requests WHERE id = ? AND kind = 'task'",
                       (task_id,)).fetchone()
    if row is None or row["status"] != "resolved":
        return None
    attempts = list_attempts(conn, task_id)
    done = next((a for a in reversed(attempts) if a["event"] == "completed"), None)
    claim = next((a for a in reversed(attempts) if a["event"] == "claimed" and (done is None or a["id"] < done["id"])), None)
    started = claim["at"] if claim else row["claimed_at"]
    ended = done["at"] if done else row["resolved_at"]
    if not started or not ended:
        return None
    seconds = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    if seconds < 0:
        return None
    effort = {"elapsed_seconds": int(seconds), "claimed_at": started, "completed_at": ended,
              "session_id": claim["session_id"] if claim else None, "tokens": None}
    if tokens and effort["session_id"]:
        from apo import claude_runtime
        from apo import transcripts

        path = transcripts.find(effort["session_id"], claude_runtime.claude_config_dirs())
        effort["tokens"] = transcripts.usage_between(str(path) if path else None, started, ended)
    return effort


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def format_tokens(count: int) -> str:
    if count < 1000:
        return f"{count} tokens"
    if count < 1_000_000:
        return f"{count / 1000:.1f}".rstrip("0").rstrip(".") + "k tokens"
    return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M tokens"


def effort_text(effort: dict | None) -> str | None:
    """'1h 5m · 1.5k tokens', or None when there is nothing to show."""
    if not effort:
        return None
    parts = [format_duration(effort["elapsed_seconds"])]
    if effort.get("tokens"):
        parts.append(format_tokens(effort["tokens"]["total"]))
    return " · ".join(parts)


def claim_task(conn: sqlite3.Connection, task_id: int, claude_session_id: str) -> bool:
    """An agent takes a task. False if it's not an open task (someone else has it)."""
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'in_progress', claimed_by_session_id = ?, claimed_at = ? "
        "WHERE id = ? AND kind = 'task' AND status = 'open'",
        (claude_session_id, _now(), task_id),
    )
    conn.commit()
    if cur.rowcount:
        _record_attempt(conn, task_id, "claimed", session_id=claude_session_id, actor="agent")
        _item_event(conn, task_id, "item.claimed", "Claimed by an agent", actor="agent", importance=0)
        conn.execute("UPDATE sessions SET outcome_id = COALESCE(outcome_id, (SELECT outcome_id FROM decision_requests "
                     "WHERE id = ?)) WHERE claude_session_id = ?", (task_id, claude_session_id))
        conn.commit()
        _link_dispatched_child(conn, claude_session_id, task_id=task_id)
        _sync_repo_files(conn, task_id)
    return bool(cur.rowcount)


COMPLETION_OUTCOMES = ("done", "partial")


def complete_task(
    conn: sqlite3.Connection, task_id: int, note: str | None = None, by: str = "agent", cwd: str | None = None,
    outcome: str | None = None, done: str | None = None, not_done: str | None = None,
    left_for_human: str | None = None, branch: str | None = None,
) -> bool:
    """cwd is the folder the closing agent works in, for _work_location. outcome
    'partial' closes the task but marks it as not fully done; the rest says what was
    and wasn't, so the next reader needn't reconstruct it from the note."""
    if outcome is not None and outcome not in COMPLETION_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(COMPLETION_OUTCOMES)}, got {outcome!r}")
    completion = {k: v.strip() for k, v in (("outcome", outcome), ("done", done), ("not_done", not_done),
                                            ("left_for_human", left_for_human), ("branch", branch))
                  if v and v.strip()}
    location = _work_location(conn, task_id, cwd)
    session = conn.execute("SELECT claimed_by_session_id FROM decision_requests WHERE id = ?", (task_id,)).fetchone()
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'resolved', resolved_at = ?, answer_text = COALESCE(?, answer_text), "
        "answer_source = COALESCE(answer_source, ?), work_location = ?, completion = ? WHERE id = ? "
        "AND kind IN ('task', 'wait') AND status IN ('open', 'in_progress')",
        (_now(), note, by, location, json.dumps(completion) if completion else None, task_id),
    )
    conn.commit()
    if cur.rowcount:
        _record_attempt(conn, task_id, "completed", session_id=session["claimed_by_session_id"] if session else None,
                        actor=by, outcome=outcome or "done", note=note, detail=completion or None)
        _item_event(conn, task_id, "item.completed", "Done" if outcome != "partial" else "Partly done",
                    actor=by, detail=note)
        _sync_repo_files(conn, task_id)
    return bool(cur.rowcount)


def retry_task(conn: sqlite3.Connection, task_id: int, note: str | None = None, by: str = "human") -> bool:
    """Put a task that failed, was abandoned, was cancelled, or is held by a session that
    will never finish it back to READY. The claim is released and the finish cleared from
    the task, but every claim and finish stays in its attempt history. False for anything
    that is not an agent task, or is already waiting for an agent."""
    row = conn.execute("SELECT status, claimed_by_session_id FROM decision_requests WHERE id = ? AND kind = 'task'",
                       (task_id,)).fetchone()
    if row is None or row["status"] not in ("in_progress", "cancelled", "resolved"):
        return False
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'open', claimed_by_session_id = NULL, claimed_at = NULL, "
        "resolved_at = NULL, cancel_reason = NULL, answer_text = NULL, answer_source = NULL, "
        "work_location = NULL, completion = NULL WHERE id = ? AND kind = 'task' "
        "AND status IN ('in_progress', 'cancelled', 'resolved')",
        (task_id,),
    )
    conn.commit()
    if cur.rowcount:
        _record_attempt(conn, task_id, "retried", session_id=row["claimed_by_session_id"], actor=by, note=note)
        _item_event(conn, task_id, "item.retried", "Back to ready", actor=by, detail=note)
        _sync_repo_files(conn, task_id)
    return bool(cur.rowcount)


def stuck_claims(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    """Claimed tasks whose claiming session has ended. A session_end normally releases
    them; this catches the ones it missed (a crash, a claim made after the end)."""
    scope, params = ("AND decision_requests.project_id = ?", (project_id,)) if project_id is not None else ("", ())
    rows = conn.execute(
        "SELECT decision_requests.id FROM decision_requests JOIN sessions "
        "ON sessions.claude_session_id = decision_requests.claimed_by_session_id "
        f"WHERE decision_requests.kind = 'task' AND decision_requests.status = 'in_progress' "
        f"AND sessions.status = 'ended' {scope} ORDER BY decision_requests.claimed_at",
        params,
    ).fetchall()
    return [get_decision_request(conn, r["id"]) for r in rows]


def _work_location(conn: sqlite3.Connection, item_id: int, cwd: str | None) -> str | None:
    """Where the closing agent left the work (changelog.work_location), as JSON. Only
    from a checkout of the item's own repository, a worktree included: a session can
    close an item while running in some other repo, whose branch says nothing about it."""
    if not cwd:
        return None
    from apo import changelog
    from apo import repo_writer

    row = conn.execute("SELECT project_id, repository_id FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
    repository = repo_writer._resolve_repository(conn, row["repository_id"], row["project_id"]) if row else None
    root = git_root(cwd)
    if repository is None or root is None:
        return None
    item_root = git_root(repository["local_path"])
    if item_root is None or os.path.realpath(root) != os.path.realpath(item_root):
        return None
    location = changelog.work_location(cwd)
    return json.dumps(location) if location else None


def current_work_location(conn: sqlite3.Connection, item: dict) -> dict | None:
    """The recorded location, re-checked for a merge since: work is usually merged
    after the item closes, and a page still saying "not merged" would mislead."""
    location = item.get("work_location")
    if not location or location.get("merged") is not False or not location.get("default_branch"):
        return location
    from apo import changelog
    from apo import repo_writer

    repository = repo_writer._resolve_repository(conn, item["repository_id"], item["project_id"])
    if repository and changelog.merged_into(repository["local_path"], location["commit"], location["default_branch"]):
        return {**location, "merged": True}
    return location


def item_branches(item: dict) -> list[str]:
    """Branches an item's work may be on, most trusted first: the one the agent named when
    it closed the item, then the one checked out where it closed it."""
    named = ((item.get("completion") or {}).get("branch") or "").strip()
    recorded = (item.get("work_location") or {}).get("branch")
    return [b for b in dict.fromkeys((named, recorded)) if b]


def item_merge(conn: sqlite3.Connection, item: dict) -> dict | None:
    """A closed item's branch measured against the default branch (changelog.branch_state),
    with the folder it's in, for the item page's Merge button. None when the item isn't
    closed or none of its branches exists locally."""
    if item.get("status") != "resolved":
        return None
    from apo import changelog
    from apo import repo_writer

    repository = repo_writer._resolve_repository(conn, item["repository_id"], item["project_id"])
    if repository is None:
        return None
    for branch in item_branches(item):
        state = changelog.branch_state(repository["local_path"], branch)
        if state:
            return {**state, "repository_id": repository["id"]}
    return None


def items_by_branch(conn: sqlite3.Connection, project_id: int, branches: Iterable[str]) -> dict[str, dict]:
    """Closed items of a project by which of these branches their work is on (the first
    of item_branches that is one of them); the latest item wins a branch."""
    wanted = set(branches)
    if not wanted:
        return {}
    rows = conn.execute(
        "SELECT id, title, completion, work_location FROM decision_requests WHERE project_id = ? "
        "AND status = 'resolved' AND (completion LIKE '%\"branch\"%' OR work_location IS NOT NULL) "
        "ORDER BY resolved_at DESC LIMIT 300",
        (project_id,),
    ).fetchall()
    found: dict[str, dict] = {}
    for row in rows:
        item = {"completion": _loads(row["completion"]), "work_location": _loads(row["work_location"])}
        branch = next((b for b in item_branches(item) if b in wanted), None)
        if branch:
            found.setdefault(branch, {"id": row["id"], "title": row["title"]})
    return found


def work_location_phrase(location: dict | None) -> str | None:
    """Where the work is, in words, e.g. "on branch `x` (commit abc1234) — not merged into `master`"."""
    if not location or not location.get("commit"):
        return None
    commit, branch, default = location["commit"][:7], location.get("branch"), location.get("default_branch")
    where = f"on branch `{branch}` (commit {commit})" if branch else f"at commit {commit}, not on any branch"
    if not default or branch == default or location.get("merged") is None:
        return where
    return f"{where} — {'merged' if location['merged'] else 'not merged'} into `{default}`"


def list_tasks(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """Agent tasks not done yet, ready ones first."""
    rows = conn.execute(
        "SELECT id FROM decision_requests WHERE project_id = ? AND kind = 'task' AND status IN ('open', 'in_progress') "
        "ORDER BY status = 'in_progress', work_rank IS NULL, work_rank, COALESCE(outcome_id, 0), COALESCE(sort_order, 0), id",
        (project_id,),
    ).fetchall()
    tasks = []
    for row in rows:
        task = get_decision_request(conn, row["id"])
        task["blocked_by"] = open_blockers(conn, task["id"])
        tasks.append(task)
    return tasks


def cancel_request(conn: sqlite3.Connection, item_id: int, reason: str) -> bool:
    """Withdrawn, superseded, declined or dropped: not done, and no longer wanted."""
    row = conn.execute("SELECT origin_session_id FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'cancelled', cancel_reason = ?, resolved_at = ?, delivery = 'none' "
        "WHERE id = ? AND status IN ('open', 'in_progress')",
        (reason, _now(), item_id),
    )
    conn.commit()
    if not cur.rowcount:
        return False
    _item_event(conn, item_id, "item.cancelled", "Cancelled", actor="agent", detail=reason)
    if row and row["origin_session_id"]:
        _refresh_session_blocked(conn, row["origin_session_id"])
    _sync_repo_files(conn, item_id)
    return True


# --- Edit, undo, reopen ---

_UNDO_FIELDS = (
    "status", "answer_text", "answer_source", "answer_kind", "answer_option", "answered_at", "resolved_at",
    "delivery", "cancel_reason", "snoozed_until", "snooze_condition", "work_location", "claimed_by_session_id",
    "claimed_at",
)
_FINAL = ("resolved", "cancelled")


def _push_issue_update(conn: sqlite3.Connection, item_id: int, text: bool = False, state: str | None = None) -> None:
    """Same contract as _push_to_sources: the row is already saved, so a source being
    down is recorded and retried, never raised."""
    try:
        from apo.integrations import sources
        for src in sources.SOURCES:
            if src.is_configured() and is_connector_enabled(conn, src.name):
                src.push_edit(conn, item_id, text=text, state=state)
    except Exception as exc:  # noqa: BLE001
        record_sync_error(conn, "sources", item_id, f"edit push failed: {exc!r}")


def edit_item(
    conn: sqlite3.Connection, item_id: int, *, title: str | None = None, description: str | None = None,
    details: str | None = None, actor: str = "human",
) -> bool:
    """Change the words on a filed item. description is the short summary, details the
    full ask (the question/brief). False when nothing differs. A task's summary and
    brief are one text until they are told apart, so editing one keeps the other level."""
    item = get_decision_request(conn, item_id)
    if item is None:
        return False
    changes: dict[str, str | None] = {}
    if title is not None:
        title = " ".join(title.split())
        if not title:
            raise ValueError("A title can't be empty")
        changes["title"] = title
    if details is not None:
        if not details.strip():
            raise ValueError("The details can't be empty")
        changes["question"] = details.strip()
    shown = item["summary"] or item["question"]
    if description is not None and description.strip() != shown:
        changes["summary"] = description.strip() or None
        if details is None and item["summary"] == item["question"] and description.strip():
            changes["question"] = description.strip()
    changes = {k: v for k, v in changes.items() if item[k] != v}
    if not changes:
        return False
    conn.execute(
        f"UPDATE decision_requests SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
        (*changes.values(), item_id),
    )
    conn.execute(
        "INSERT INTO audit_log (decision_request_id, event_type, detail, created_at) VALUES (?, 'edited', ?, ?)",
        (item_id, json.dumps({k: {"from": item[k], "to": v} for k, v in changes.items()}), _now()),
    )
    conn.commit()
    labels = {"title": "title", "summary": "description", "question": "details"}
    _item_event(conn, item_id, "item.edited", "Edited", actor=actor,
                detail=", ".join(labels[k] for k in changes), importance=0)
    _sync_repo_files(conn, item_id)
    _push_issue_update(conn, item_id, text=True)
    return True


def snapshot_for_undo(conn: sqlite3.Connection, item_id: int, action: str) -> None:
    """Call just before a Done/Cancel/Snooze so undo_last can put the row back."""
    row = conn.execute(f"SELECT {', '.join(_UNDO_FIELDS)} FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
    if row:
        conn.execute("UPDATE decision_requests SET undo_snapshot = ? WHERE id = ?",
                     (json.dumps({"action": action, "fields": dict(row)}), item_id))
        conn.commit()


def clear_undo(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE decision_requests SET undo_snapshot = NULL WHERE id = ?", (item_id,))
    conn.commit()


def can_undo(item: dict) -> bool:
    """The snapshot only counts while the item is still in the state its action left it in."""
    snap = _loads(item.get("undo_snapshot"))
    if not snap:
        return False
    if snap["action"] == "done":
        return item["status"] in ("answered", "resolved")
    if snap["action"] == "cancel":
        return item["status"] == "cancelled"
    if snap["action"] == "snooze":
        return bool(item.get("snoozed_until") or item.get("snooze_condition"))
    return False


def undo_last(conn: sqlite3.Connection, item_id: int, actor: str = "human") -> bool:
    item = get_decision_request(conn, item_id)
    if item is None or not can_undo(item):
        return False
    snap = _loads(item["undo_snapshot"])
    fields = snap["fields"]
    conn.execute(
        f"UPDATE decision_requests SET {', '.join(f'{k} = ?' for k in fields)}, undo_snapshot = NULL WHERE id = ?",
        (*fields.values(), item_id),
    )
    conn.execute(
        "INSERT INTO audit_log (decision_request_id, event_type, detail, created_at) VALUES (?, 'undone', ?, ?)",
        (item_id, f"{snap['action']}: {item['status']} -> {fields['status']}", _now()),
    )
    conn.commit()
    verb = {"done": "Done", "cancel": "Cancel", "snooze": "Snooze"}[snap["action"]]
    _item_event(conn, item_id, "item.undone", f"Undid {verb}", actor=actor, importance=0)
    _after_reopen(conn, item, item["status"], fields["status"])
    return True


def _after_reopen(conn: sqlite3.Connection, item: dict, before: str, after: str) -> None:
    if item.get("origin_session_id"):
        _refresh_session_blocked(conn, item["origin_session_id"])
    _sync_repo_files(conn, item["id"])
    # Linear is closed by an answer or completion, never by a cancel or a snooze.
    if before in ("answered", "resolved") and after not in ("answered", "resolved"):
        _push_issue_update(conn, item["id"], state="started" if after == "in_progress" else "unstarted")


def reopen_item(conn: sqlite3.Connection, item_id: int, actor: str = "human") -> str | None:
    """Put a resolved or cancelled item back to a live status and return it, or None if
    it isn't closed. A decision an agent was answering returns to 'answered' with its
    answer intact; a task keeps its completion note; anything that can be answered
    afresh sets the old answer aside in previous_answer."""
    item = get_decision_request(conn, item_id)
    if item is None or item["status"] not in _FINAL:
        return None
    keeps_answer = bool(item["answer_text"]) and item["kind"] == "decision" and item["requested_by"] != "human"
    sets: dict[str, str | None] = {
        "status": "answered" if keeps_answer else "open", "resolved_at": None, "cancel_reason": None,
        "work_location": None, "claimed_by_session_id": None, "claimed_at": None, "undo_snapshot": None,
        "delivery": "pending" if keeps_answer else "none",
    }
    if not keeps_answer and item["answer_text"] and item["kind"] not in ("task", "wait"):
        sets.update(previous_answer=item["answer_text"], answer_text=None, answer_source=None, answer_kind=None,
                    answer_option=None, answered_at=None)
    conn.execute(f"UPDATE decision_requests SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                 (*sets.values(), item_id))
    conn.execute(
        "INSERT INTO audit_log (decision_request_id, event_type, detail, created_at) VALUES (?, 'reopened', ?, ?)",
        (item_id, f"{item['status']} -> {sets['status']}", _now()),
    )
    conn.commit()
    _item_event(conn, item_id, "item.reopened", "Reopened", actor=actor, detail=f"back to {sets['status']}")
    _after_reopen(conn, item, item["status"], sets["status"])
    return sets["status"]


def reassign_step(conn: sqlite3.Connection, item_id: int, owner: str) -> None:
    """Hand a step between the human and an agent (me <-> agent)."""
    if owner not in ("me", "agent"):
        raise ValueError(f"owner must be 'me' or 'agent', got {owner!r}")
    kind = _OWNER_KIND[owner]
    cur = conn.execute(
        "UPDATE decision_requests SET kind = ?, assignee = ?, status = 'open', claimed_by_session_id = NULL "
        "WHERE id = ? AND kind IN ('action', 'task') AND status IN ('open', 'in_progress')",
        (kind, _ASSIGNEE[kind], item_id),
    )
    conn.commit()
    if cur.rowcount:
        _item_event(conn, item_id, "item.handed_off", "Handed to you" if owner == "me" else "Handed to an agent",
                    actor="human")
        _sync_repo_files(conn, item_id)


def _item_event(conn, item_id: int, verb: str, prefix: str, *, actor: str, detail: str | None = None,
                importance: int = 1) -> None:
    row = conn.execute("SELECT title, project_id FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
    if row:
        suffix = f": {_one_line(detail, 120)}" if detail else ""
        record_event(conn, verb=verb, actor=actor, project_id=row["project_id"], decision_id=item_id,
                     importance=importance, summary=f"{prefix} — #{item_id} {row['title']}{suffix}")


# --- Outcomes ---
#
# A result the human wants, with a "done when" and ordered steps owned by the human,
# an agent, or someone external. A checklist is an outcome's steps in order. Only the
# human marks one achieved; when every step is done it's flagged ready to close.

OUTCOME_STATUSES = ("active", "paused", "achieved", "dropped")


def create_outcome(
    conn: sqlite3.Connection, project_id: int, title: str, done_when: str | None = None,
    created_by: str = "human", session_id: str | None = None, description: str | None = None,
    linear_issue: dict | None = None, due_on: str | None = None,
) -> int:
    """linear_issue ({id, identifier, url, state}) is the issue a goal came from. It's
    saved in the same insert, so the unique index refuses a second import of it."""
    now = _now()
    issue = linear_issue or {}
    cur = conn.execute(
        "INSERT INTO outcomes (project_id, title, done_when, description, status, created_by, created_by_session_id, "
        "linear_issue_id, linear_identifier, linear_url, linear_state, due_on, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, title, done_when, description, created_by, session_id, issue.get("id"), issue.get("identifier"),
         issue.get("url"), issue.get("state"), _due_date(due_on), now, now),
    )
    conn.commit()
    if session_id:
        conn.execute("UPDATE sessions SET outcome_id = ? WHERE claude_session_id = ?", (cur.lastrowid, session_id))
        conn.commit()
    actor = "agent" if created_by == "agent" else "integration" if linear_issue else "human"
    record_event(conn, verb="outcome.created", actor=actor, source="linear" if linear_issue else None, project_id=project_id,
                 importance=2 if created_by == "agent" or linear_issue else 1,
                 summary=f"Goal from Linear {issue['identifier']}: {title}" if linear_issue else f"Outcome started: {title}")
    return cur.lastrowid


def get_outcome(conn: sqlite3.Connection, outcome_id: int) -> dict | None:
    row = conn.execute(
        "SELECT outcomes.*, projects.name AS project_name FROM outcomes "
        "JOIN projects ON projects.id = outcomes.project_id WHERE outcomes.id = ?",
        (outcome_id,),
    ).fetchone()
    return dict(row) if row else None


def add_step(
    conn: sqlite3.Connection, outcome_id: int, title: str, owner: str, detail: str | None = None,
    waiting_on: str | None = None, follow_up_at: str | None = None, blocked_by: list[int] | None = None,
    requested_by: str = "human", origin_session_id: str | None = None, repository_id: int | None = None,
) -> int:
    if owner not in _OWNER_KIND:
        raise ValueError(f"owner must be one of me, agent, external — got {owner!r}")
    outcome = get_outcome(conn, outcome_id)
    if outcome is None:
        raise ValueError(f"No outcome #{outcome_id}")
    if repository_id is not None:
        repository_id = _project_repository(conn, outcome["project_id"], repository_id)["id"]
    else:
        repository = get_repository_for_project(conn, outcome["project_id"])
        repository_id = repository["id"] if repository else None
    question = detail or (f"Waiting on {waiting_on}" if waiting_on else title)
    return create_decision_request(
        conn, project_id=outcome["project_id"], repository_id=repository_id,
        title=title, question=question, summary=detail or title, kind=_OWNER_KIND[owner],
        requested_by=requested_by, outcome_id=outcome_id, waiting_on=waiting_on, follow_up_at=follow_up_at,
        blocked_by=blocked_by, origin_session_id=origin_session_id if owner == "me" else None,
    )


def complete_step(conn: sqlite3.Connection, item_id: int, by: str = "human", note: str | None = None) -> bool:
    item = get_decision_request(conn, item_id)
    if item is None:
        return False
    if item["kind"] in ("task", "wait"):
        return complete_task(conn, item_id, note=note or ("Arrived." if item["kind"] == "wait" else None), by=by)
    text = "Done." + (f" {note}" if note else "")
    return answer_decision_request(conn, item_id, text, source="dashboard" if by == "human" else by,
                                   answer_kind="done")


def outcome_progress(conn: sqlite3.Connection, outcome_id: int) -> dict:
    linear_steps = {r["step_id"]: {"identifier": r["linear_identifier"], "url": r["linear_url"]} for r in conn.execute(
        "SELECT step_id, linear_identifier, linear_url FROM linear_steps WHERE outcome_id = ?", (outcome_id,))}
    steps = []
    for row in conn.execute(
        "SELECT id FROM decision_requests WHERE outcome_id = ? ORDER BY sort_order, id", (outcome_id,)
    ).fetchall():
        step = get_decision_request(conn, row["id"])
        step["owner"] = _KIND_OWNER.get(step["kind"], "me")
        step["done"] = step["status"] in ("answered", "resolved")
        step["blocked_by"] = open_blockers(conn, step["id"])
        step["linear_step"] = linear_steps.get(step["id"])
        steps.append(step)
    live = [s for s in steps if s["status"] != "cancelled"]
    done = sum(1 for s in live if s["done"])
    upcoming = [s for s in live if not s["done"]]
    next_step = next((s for s in upcoming if not s["blocked_by"]), upcoming[0] if upcoming else None)
    return {
        "steps": steps, "done": done, "total": len(live), "next": next_step,
        "next_owner": next_step["owner"] if next_step else None,
        "ready_to_close": bool(live) and done == len(live),
    }


def list_outcomes(conn: sqlite3.Connection, project_id: int | None = None, statuses=("active", "paused")) -> list[dict]:
    clauses, params = [f"outcomes.status IN ({','.join('?' * len(statuses))})"], list(statuses)
    if project_id is not None:
        clauses.append("outcomes.project_id = ?")
        params.append(project_id)
    rows = conn.execute(
        "SELECT outcomes.*, projects.name AS project_name FROM outcomes JOIN projects ON projects.id = outcomes.project_id "
        f"WHERE {' AND '.join(clauses)} AND projects.archived_at IS NULL ORDER BY outcomes.id",
        params,
    ).fetchall()
    return [{**dict(r), "progress": outcome_progress(conn, r["id"]), "trend": outcome_trend(conn, r["id"])} for r in rows]


def set_outcome_status(
    conn: sqlite3.Connection, outcome_id: int, status: str, by: str = "human", cancel_open_steps: bool = True,
) -> None:
    """The human's call, made here or (by="linear") on the goal's Linear issue.
    Dropping cancels the steps still open, unless the caller cancels its own choice."""
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {', '.join(OUTCOME_STATUSES)}")
    now = _now()
    conn.execute(
        "UPDATE outcomes SET status = ?, updated_at = ?, achieved_at = CASE WHEN ? = 'achieved' THEN ? ELSE achieved_at END "
        "WHERE id = ?",
        (status, now, status, now, outcome_id),
    )
    conn.commit()
    if status == "dropped" and cancel_open_steps:
        for row in conn.execute(
            "SELECT id FROM decision_requests WHERE outcome_id = ? AND status IN ('open', 'in_progress')", (outcome_id,)
        ).fetchall():
            cancel_request(conn, row["id"], "Outcome dropped")
    outcome = get_outcome(conn, outcome_id)
    if outcome:
        record_event(conn, verb=f"outcome.{status}", actor=by, project_id=outcome["project_id"],
                     importance=2 if status == "achieved" else 1, summary=f"Outcome {status}: {outcome['title']}")


def _linear_timestamp(moment: datetime | None = None) -> str:
    """Linear's own createdAt format, so cursors compare as plain strings."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def set_linear_goals(conn: sqlite3.Connection, project_id: int, enabled: bool) -> None:
    """Issues created in the project's Linear project become goals (linear_goals.py).
    On starts the cursor at this moment, so existing history never arrives as goals.
    Off forgets it, so turning it back on doesn't deliver everything filed meanwhile
    as a surprise batch of agent work."""
    cur = conn.execute(
        "UPDATE projects SET linear_goals_since = CASE WHEN ? THEN ? END "
        "WHERE id = ? AND (linear_goals_since IS NULL) = ?",
        (enabled, _linear_timestamp(), project_id, enabled),
    )
    conn.commit()
    if cur.rowcount:
        record_event(conn, verb="project.linear_goals", actor="human", project_id=project_id,
                     summary=f"Goals from Linear turned {'on' if enabled else 'off'}")


def set_linear_project(conn: sqlite3.Connection, project_id: int, linear_project_id: str, name: str) -> None:
    """The Linear project this project's goals come from. Picking a different one starts
    its import from this moment, as turning it on does."""
    cur = conn.execute(
        "UPDATE projects SET linear_project_id = ?, linear_project_name = ?, "
        "linear_goals_since = CASE WHEN linear_goals_since IS NULL THEN NULL ELSE ? END "
        "WHERE id = ? AND linear_project_id IS NOT ?",
        (linear_project_id, name, _linear_timestamp(), project_id, linear_project_id),
    )
    conn.commit()
    if cur.rowcount:
        record_event(conn, verb="project.linear_project", actor="human", project_id=project_id,
                     summary=f"Goals from Linear now come from the Linear project {name}")


def forget_linear_projects(conn: sqlite3.Connection) -> None:
    """Another Linear workspace has other projects, so each project falls back to the
    one with its name there. The switch and its cursor stay as they are."""
    conn.execute(
        "UPDATE projects SET linear_project_id = NULL, linear_project_name = NULL WHERE linear_project_id IS NOT NULL"
    )
    conn.commit()


def get_linear_workspace(conn: sqlite3.Connection) -> dict | None:
    return _loads(get_ui_state(conn, "linear_workspace"))


def set_linear_workspace(conn: sqlite3.Connection, workspace: dict) -> None:
    """Kept apart from the credential, so a disconnect doesn't forget it."""
    set_ui_state(conn, "linear_workspace", json.dumps({"id": workspace["id"], "name": workspace["name"]}))


# Failures that only ever concern issues pushed to (or polled in) the connected workspace.
_LINEAR_LINK_ERRORS = ("linear_goals", "linear_poll", "linear_resolve", "linear_retry", "linear_push_retry")


def linear_connected(conn: sqlite3.Connection, workspace: dict | None) -> bool:
    """Linear was (re)connected to `workspace` ({id, name}; None if it couldn't say).
    The same workspace as before, or the first one seen, changes nothing stored. Another
    one makes everything stored under the old one meaningless:
    - projects forget their Linear project, and the import restarts from this moment,
      as turning it on does, so nothing older in the new workspace comes in;
    - goals imported from the old workspace, and requests pushed to it, keep everything
      here but lose their link, so nothing polls, closes or retries them against issues
      the new workspace doesn't have. An unlinked request is never pushed again: a push
      only happens when it's filed, or on retrying a push that failed.
    True if anything was unlinked."""
    if not workspace:
        return False
    previous = get_linear_workspace(conn)
    set_linear_workspace(conn, workspace)
    if previous is None or previous["id"] == workspace["id"]:
        return False
    forget_linear_projects(conn)
    conn.execute("UPDATE projects SET linear_goals_since = ? WHERE linear_goals_since IS NOT NULL", (_linear_timestamp(),))
    count = "SELECT project_id, COUNT(*) AS n FROM {} WHERE linear_issue_id IS NOT NULL GROUP BY project_id"
    goals = {r["project_id"]: r["n"] for r in conn.execute(count.format("outcomes"))}
    requests = {r["project_id"]: r["n"] for r in conn.execute(count.format("decision_requests"))}
    conn.execute("DELETE FROM linear_steps WHERE outcome_id IN (SELECT id FROM outcomes WHERE linear_issue_id IS NOT NULL)")
    conn.execute(
        "UPDATE outcomes SET linear_issue_id = NULL, linear_identifier = NULL, linear_url = NULL, linear_state = NULL, "
        "linear_status_synced = NULL WHERE linear_issue_id IS NOT NULL"
    )
    conn.execute(
        "UPDATE decision_requests SET linear_issue_id = NULL, linear_identifier = NULL, linear_url = NULL, "
        "linear_synced_comment_id = NULL, linear_resolve_error = NULL, linear_retry_attempts = 0, "
        "linear_next_retry_at = NULL WHERE linear_issue_id IS NOT NULL"
    )
    conn.commit()
    for project_id in sorted(set(goals) | set(requests)):
        counts = ((goals.get(project_id, 0), "goal"), (requests.get(project_id, 0), "request"))
        what = " and ".join(f"{n} {noun}{'s' if n != 1 else ''}" for n, noun in counts if n)
        record_event(conn, verb="project.linear_unlinked", actor="system", source="linear", project_id=project_id,
                     summary=f"{what} unlinked from Linear: the Linear workspace changed from "
                             f"{previous['name']} to {workspace['name']}. They stay here without syncing.")
    resolve_sync_errors(conn, _LINEAR_LINK_ERRORS, before=_now())
    return bool(goals or requests)


def linear_goal_projects(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM projects WHERE linear_goals_since IS NOT NULL AND archived_at IS NULL ORDER BY name COLLATE NOCASE"
    )]


def create_action_request(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    title: str,
    question: str,
    repository_id: int | None = None,
    summary: str | None = None,
    context: str | None = None,
    why_it_matters: str | None = None,
    blocked_work: list | None = None,
    safe_work: list | None = None,
    resume_plan: dict | None = None,
    origin_session_id: str | None = None,
    action_spec: dict | None = None,
    est_minutes: int | None = None,
) -> int:
    """Same row as create_decision_request (kind='action') — deliberately has no
    options/recommendation/confidence parameters, since an Action isn't a judgment
    call. Answering one (answer_decision_request, unchanged) is a "here's what I did"
    confirmation rather than a chosen option, but goes through the exact same write
    path, repo_writer projection, and MCP/CLI resolve call as a Decision.

    action_spec: {where, steps, values, inputs, done_when, verify} — see the MCP
    create_action_request tool for the shape."""
    return create_decision_request(
        conn, project_id=project_id, title=title, question=question, repository_id=repository_id,
        summary=summary, context=context, why_it_matters=why_it_matters, blocked_work=blocked_work,
        safe_work=safe_work, resume_plan=resume_plan, origin_session_id=origin_session_id, kind="action",
        action_spec=action_spec, est_minutes=est_minutes,
    )


def _secret_key(decision_id: int, name: str) -> str:
    return f"action-secret:{decision_id}:{name}"


def store_action_secret(conn: sqlite3.Connection, decision_id: int, name: str, value: str) -> None:
    """A secret the human handed over for an Action: encrypted in the local credential
    store, never written into the answer text or the repo."""
    from apo import credentials

    credentials.set_credentials(conn, _secret_key(decision_id, name), {"value": value}, display_name=name)


def get_action_secret(conn: sqlite3.Connection, decision_id: int, name: str) -> str | None:
    from apo import credentials

    stored = credentials.get_credentials(conn, _secret_key(decision_id, name))
    return stored.get("value") if stored else None


# Which requests block a session is derived from decision_requests.origin_session_id
# every time, never stored as a pointer on the session: agents batch-file several
# requests at once, and a single pointer could only ever hold the last one.
# sessions.current_blocker_id is vestigial and no longer written.


def _mark_session_blocked(conn: sqlite3.Connection, claude_session_id: str, decision_id: int) -> None:
    conn.execute("UPDATE sessions SET status = 'blocked' WHERE claude_session_id = ?", (claude_session_id,))
    conn.commit()


def _has_open_requests(conn: sqlite3.Connection, claude_session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM decision_requests WHERE origin_session_id = ? AND status = 'open' LIMIT 1",
        (claude_session_id,),
    ).fetchone() is not None


def _refresh_session_blocked(conn: sqlite3.Connection, claude_session_id: str) -> None:
    """A session marked blocked whose last open request just went away is idle."""
    if not _has_open_requests(conn, claude_session_id):
        conn.execute(
            "UPDATE sessions SET status = 'idle' WHERE claude_session_id = ? AND status = 'blocked'",
            (claude_session_id,),
        )
        conn.commit()


def _clear_session_blocker(conn: sqlite3.Connection, decision_id: int) -> None:
    row = conn.execute(
        "SELECT origin_session_id FROM decision_requests WHERE id = ?", (decision_id,)
    ).fetchone()
    if row and row["origin_session_id"]:
        _refresh_session_blocked(conn, row["origin_session_id"])


def bind_request_to_session(conn: sqlite3.Connection, request_id: int, claude_session_id: str) -> bool:
    """Re-point a request at the session that really filed it. The MCP server can
    only guess its own session (and a long-lived server guesses stale after /clear);
    the PostToolUse hook for the create call knows for certain. False if the request
    doesn't exist or was already bound to that session."""
    row = conn.execute(
        "SELECT origin_session_id FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None or row["origin_session_id"] == claude_session_id:
        return False
    conn.execute(
        "UPDATE decision_requests SET origin_session_id = ? WHERE id = ?", (claude_session_id, request_id),
    )
    conn.commit()
    if row["origin_session_id"]:
        _refresh_session_blocked(conn, row["origin_session_id"])
    return True


def is_connector_enabled(conn: sqlite3.Connection, name: str) -> bool:
    """A connector with no explicit row is on by default — toggling is opt-out, so
    existing setups (nothing but env-var configuration) keep working unchanged."""
    row = conn.execute("SELECT enabled FROM connector_settings WHERE name = ?", (name,)).fetchone()
    return bool(row["enabled"]) if row else True


def set_connector_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO connector_settings (name, enabled) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
        (name, 1 if enabled else 0),
    )
    conn.commit()


def get_connector_settings(conn: sqlite3.Connection) -> dict[str, bool]:
    """Only connectors someone has explicitly toggled — callers combine this with
    sources.SOURCES (which knows every connector's name and is_configured()) to get
    the full picture; this alone doesn't know what connectors exist."""
    return {row["name"]: bool(row["enabled"]) for row in conn.execute("SELECT name, enabled FROM connector_settings")}


def _push_to_sources(conn: sqlite3.Connection, decision_id: int) -> None:
    try:
        _push_to_sources_unsafe(conn, decision_id)
    except Exception as exc:  # noqa: BLE001
        record_sync_error(conn, "sources", decision_id, f"push failed: {exc!r}")


def _push_to_sources_unsafe(conn: sqlite3.Connection, decision_id: int) -> None:
    from apo.integrations import sources
    decision = get_decision_request(conn, decision_id)
    project = conn.execute(
        "SELECT name FROM projects WHERE id = ?", (decision["project_id"],)
    ).fetchone()
    for src in sources.SOURCES:
        if src.is_configured() and is_connector_enabled(conn, src.name):
            src.push_question(conn, decision_id, project["name"], decision)


def _sync_repo_files(conn: sqlite3.Connection, decision_id: int) -> None:
    """Refresh the in-repo markdown projection after a state change. Called from every
    transition rather than from each caller, so no answer channel can forget it.

    Deliberately non-fatal: the DB row is the source of truth and is already committed
    by the time we get here, so a repo that moved, a read-only checkout, or a decision
    with no repository at all must not fail the answer. Failures land in sync_errors
    (same as a Linear push failure) instead of vanishing — a projection that silently
    stopped updating would leave a session's nudge permanently stale."""
    from apo import repo_writer
    started = _now()
    try:
        written = repo_writer.sync_decision(conn, decision_id)
    except Exception as exc:  # noqa: BLE001
        record_sync_error(conn, "repo_writer", decision_id, str(exc))
        return
    if written is not None:
        # The whole project projection was just rewritten, so earlier failures for
        # any of its decisions have landed now too.
        resolve_sync_errors(
            conn, ["repo_writer"], before=started,
            decision_ids=[r["id"] for r in conn.execute(
                "SELECT id FROM decision_requests WHERE project_id = "
                "(SELECT project_id FROM decision_requests WHERE id = ?)", (decision_id,)
            )],
        )


def _recommended_from_labels(options: list | None) -> int | None:
    """Agents mark the recommended option in its label ("Drawer (RECOMMENDED)")
    rather than a field; the first such label is it."""
    for index, option in enumerate(options or []):
        label = option.get("label", "") if isinstance(option, dict) else str(option)
        if "recommended" in label.lower():
            return index
    return None


def _first_sentence(text: str, limit: int = 200) -> str:
    """A five-second summary when the agent didn't write one (1 of 21 live requests
    had one): the question's first sentence, clipped."""
    flat = " ".join(text.split())
    match = re.match(r"(.+?[.?!])(\s|$)", flat)
    sentence = match.group(1) if match else flat
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


ANSWER_KINDS = ("text", "option", "recommendation", "delegate", "clarify", "decline", "done")

# Answers are written verbatim into the repo's .claude/decisions/ (and some repos
# commit them), so a pasted credential would leak into the working tree.
_SECRET_PATTERNS = (
    ("Stripe secret key", re.compile(r"\b[rs]k_(live|test)_[A-Za-z0-9]{10,}")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}")),
)


def looks_like_secret(text: str | None) -> str | None:
    """The kind of credential text appears to contain, or None."""
    for kind, pattern in _SECRET_PATTERNS:
        if text and pattern.search(text):
            return kind
    return None


def compose_answer(
    decision: dict, kind: str, option: int | None = None, note: str | None = None,
) -> tuple[str, str, int | None]:
    """The answer text a quick choice records — what the resuming agent reads in the
    repo — plus the kind and chosen option to store alongside it. Raises ValueError
    when the choice isn't a complete answer."""
    note = (note or "").strip()
    with_note = f"\n\nNote: {note}" if note else ""
    if kind == "text":
        if not note:
            raise ValueError("Write an answer, or pick one of the quick answers.")
        return note, kind, None
    if kind == "option":
        options = decision.get("options") or []
        if option is None or not 0 <= option < len(options):
            raise ValueError("That option doesn't exist.")
        return f"Chose option {option + 1}: {options[option]['label']}{with_note}", kind, option
    if kind == "recommendation":
        if not decision.get("recommendation"):
            raise ValueError("There's no recommendation to accept.")
        return (f"Accepted your recommendation: {decision['recommendation']}{with_note}", kind,
                decision.get("recommended_option"))
    if kind == "delegate":
        return ("Use your judgment: go with what you'd recommend, prefer the option that's easiest to "
                "reverse, and record your reasoning in the commit. If it turns out to be a one-way door, "
                f"file a new decision instead.{with_note}"), kind, None
    if kind == "clarify":
        if not note:
            raise ValueError("Say what you need to know before deciding.")
        return (f"Not decided yet — I need more information first: {note}\n\n"
                "File a new decision with that information; it supersedes this one."), kind, None
    if kind == "decline":
        return "Declined — don't do this." + (f" {note}" if note else ""), kind, None
    if kind == "done":
        return "Done." + (f" {note}" if note else ""), kind, None
    raise ValueError(f"Unknown answer type: {kind!r}")


def _normalize_options(options: list | None) -> list[dict] | None:
    """Every real caller files options as a plain list of strings — a list of
    {label, detail} dicts is a possible but rare shape. Normalizing to dicts here,
    at the one read path every consumer (templates, linear_client, repo_writer)
    goes through, means none of them need their own type check."""
    if not options:
        return options
    return [
        {
            "label": o.get("label", ""), "detail": o.get("detail"),
            # What the agent will do if this is picked, and whether it can be undone.
            "consequence": o.get("consequence"), "reversible": o.get("reversible"),
        } if isinstance(o, dict)
        else {"label": str(o), "detail": None, "consequence": None, "reversible": None}
        for o in options
    ]


def get_decision_request(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM decision_requests WHERE id = ?", (decision_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["options"] = _normalize_options(_loads(result["options"]))
    result["blocked_work"] = _loads(result["blocked_work"])
    result["safe_work"] = _loads(result["safe_work"])
    result["action_spec"] = _loads(result.get("action_spec"))
    result["labels"] = _loads(result.get("labels")) or []
    result["note_count"] = conn.execute("SELECT COUNT(*) FROM item_notes WHERE item_id = ?", (decision_id,)).fetchone()[0]
    result["work_location"] = _loads(result.get("work_location"))
    result["completion"] = _loads(result.get("completion"))

    plan_row = conn.execute(
        "SELECT * FROM resume_plans WHERE decision_request_id = ?", (decision_id,)
    ).fetchone()
    if plan_row:
        plan = dict(plan_row)
        plan["files"] = _loads(plan["files"])
        plan["remaining_steps"] = _loads(plan["remaining_steps"])
        result["resume_plan"] = plan
    else:
        result["resume_plan"] = None

    return result


def list_decision_requests(
    conn: sqlite3.Connection,
    project_name: str | None = None,
    status: str | None = None,
    search: str | None = None,
):
    query = """
        SELECT decision_requests.*, projects.name AS project_name
        FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id
    """
    clauses, params = [], []
    if project_name:
        clauses.append("projects.name = ?")
        params.append(project_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append(
            "(title LIKE ? ESCAPE '\\' OR question LIKE ? ESCAPE '\\' OR context LIKE ? ESCAPE '\\')"
        )
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([pattern, pattern, pattern])
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY decision_requests.created_at ASC"
    return conn.execute(query, params).fetchall()


def list_decision_summaries(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    """Lightweight id/title/status/repository_id rows for one project — everything
    repo_writer's index rendering and per-decision ownership resolution need, without
    the second query + JSON-decode of 4 columns that get_decision_request pays per row."""
    return conn.execute(
        "SELECT id, title, status, repository_id, kind, work_rank FROM decision_requests "
        "WHERE project_id = ? ORDER BY id ASC",
        (project_id,),
    ).fetchall()


def project_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Per-project totals for the dashboard sidebar: total decisions, how many are
    still open (awaiting an answer) or answered (awaiting pickup), and whether the
    project is archived (see archive_project) — callers decide whether/how to surface
    archived projects. Projects with no decisions at all are absent."""
    rows = conn.execute(
        """
        SELECT projects.name AS project_name, projects.archived_at AS archived_at,
               COUNT(*) AS total,
               SUM(decision_requests.status = 'open') AS open,
               SUM(decision_requests.status = 'answered') AS answered
        FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id
        GROUP BY projects.id
        """
    ).fetchall()
    return {
        row["project_name"]: {
            "total": row["total"], "open": row["open"], "answered": row["answered"],
            "archived": row["archived_at"] is not None,
        }
        for row in rows
    }


def answer_decision_request(
    conn: sqlite3.Connection, decision_id: int, answer_text: str, source: str = "dashboard",
    answer_kind: str = "text", answer_option: int | None = None,
) -> bool:
    cur = conn.execute(
        """
        UPDATE decision_requests
        SET answer_text = ?, answer_source = ?, answer_kind = ?, answer_option = ?, answered_at = ?,
            -- The human's own step has no agent to deliver it to: done is done.
            status = CASE WHEN requested_by = 'human' THEN 'resolved' ELSE 'answered' END,
            delivery = CASE WHEN requested_by = 'human' THEN 'none' ELSE 'pending' END,
            resolved_at = CASE WHEN requested_by = 'human' THEN ? ELSE resolved_at END
        WHERE id = ? AND answer_text IS NULL AND status IN ('open', 'in_progress')
        """,
        (answer_text, source, answer_kind, answer_option, _now(), _now(), decision_id),
    )
    conn.commit()
    resolved = cur.rowcount == 1
    if resolved:
        _record_event(conn, decision_id, "answered", answer_text, source=source)
        _notify_resolved(conn, decision_id)
        _clear_session_blocker(conn, decision_id)
        _sync_repo_files(conn, decision_id)
    return resolved


def _notify_resolved(conn: sqlite3.Connection, decision_id: int) -> None:
    # The answer is already committed; a missing dependency (e.g. cryptography under
    # the wrong interpreter) must not surface as a failed answer.
    try:
        from apo.integrations import sources
        for src in sources.SOURCES:
            if src.is_configured() and is_connector_enabled(conn, src.name):
                src.mark_resolved(conn, decision_id)
    except Exception as exc:  # noqa: BLE001
        record_sync_error(conn, "sources", decision_id, f"resolve notify failed: {exc!r}")


def _record_event(
    conn: sqlite3.Connection, decision_id: int, event_type: str, detail: str | None = None,
    source: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (decision_request_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
        (decision_id, event_type, detail, _now()),
    )
    decision = conn.execute(
        "SELECT id, title, kind, project_id, requested_by FROM decision_requests WHERE id = ?", (decision_id,)
    ).fetchone()
    event = _decision_event(event_type, dict(decision), detail, source) if decision else None
    if event:
        _insert_event(conn, **event)
    conn.commit()


# --- Events ---
#
# One append-only log of state changes across every project: it drives the Activity
# page and "while you were away". Written only from the transition functions in this
# module, and only on a change — heartbeats, repeated waits and plan churn never write.
# importance: 0 trace (routine turn ends), 1 normal, 2 notable (needs the human, or
# something went wrong).

_KIND_LABEL = {"decision": "Decision", "action": "Action", "task": "Task", "review": "Review", "wait": "Wait"}


def _one_line(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _insert_event(
    conn: sqlite3.Connection, *, verb: str, summary: str, actor: str, source: str | None = None,
    project_id: int | None = None, session_id: str | None = None, decision_id: int | None = None,
    data=None, importance: int = 1, at: str | None = None,
) -> None:
    """No commit: callers inside a migration's write lock must stay in it."""
    conn.execute(
        "INSERT INTO events (occurred_at, verb, actor, source, project_id, session_id, decision_id, "
        "summary, data, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (at or _now(), verb, actor, source, project_id, session_id, decision_id, summary, _dumps(data), importance),
    )


def record_event(conn: sqlite3.Connection, **event) -> None:
    _insert_event(conn, **event)
    conn.commit()


def _decision_event(event_type: str, decision: dict, detail: str | None, source: str | None) -> dict | None:
    ref = f"#{decision['id']}"
    base = {"project_id": decision["project_id"], "decision_id": decision["id"]}
    if event_type == "created":
        label = _KIND_LABEL.get(decision["kind"], "Decision")
        by = decision.get("requested_by") or "agent"
        needs_human = by == "agent" and _ASSIGNEE.get(decision["kind"]) == "human"
        return dict(base, verb="item.created", actor=by, importance=2 if needs_human else 1,
                    summary=f"{label} {ref} {'filed' if by == 'agent' else 'added'}: {decision['title']}")
    if event_type == "answered":
        return dict(base, verb="item.answered", actor="human", source=source,
                    summary=f"You answered {ref} ({decision['title']}): {_one_line(detail, 120)}")
    if event_type == "resolved":
        return dict(base, verb="item.applied", actor="agent", summary=f"{ref} applied: {decision['title']}")
    if event_type == "closed":
        return dict(base, verb="item.closed", actor="system", summary=f"{ref} closed: {decision['title']}")
    return None


def _session_label(conn: sqlite3.Connection, row, prefix: str) -> str:
    name = row["title"] if "title" in row.keys() and row["title"] else None
    if not name and row["project_id"]:
        project = conn.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
        name = project["name"] if project else None
    if not name and row["cwd"] and _is_home_or_above(row["cwd"]):
        name = "~ (no project)"
    if not name:
        name = os.path.basename((row["cwd"] or "").rstrip("/")) or row["claude_session_id"][:8]
    return f"{prefix} — {name}"


def _session_event(
    conn: sqlite3.Connection, claude_session_id: str, verb: str, prefix: str, *,
    detail: str | None = None, importance: int = 1, actor: str = "agent", source: str = "hook",
) -> None:
    row = conn.execute("SELECT * FROM sessions WHERE claude_session_id = ?", (claude_session_id,)).fetchone()
    if row is None:
        return
    summary = _session_label(conn, row, prefix) + (f": {_one_line(detail, 160)}" if detail else "")
    record_event(conn, verb=verb, summary=summary, actor=actor, source=source,
                 project_id=row["project_id"], session_id=claude_session_id, importance=importance)


def list_events(
    conn: sqlite3.Connection, *, since_seq: int | None = None, project_id: int | None = None,
    session_id: str | None = None, decision_id: int | None = None, min_importance: int = 1,
    limit: int = 200, oldest_first: bool = False,
) -> list[dict]:
    clauses, params = ["importance >= ?"], [min_importance]
    for column, value in (("project_id", project_id), ("session_id", session_id), ("decision_id", decision_id)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if since_seq is not None:
        clauses.append("seq > ?")
        params.append(since_seq)
    order = "ASC" if oldest_first else "DESC"
    rows = conn.execute(
        f"SELECT events.*, projects.name AS project_name FROM events "
        f"LEFT JOIN projects ON projects.id = events.project_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY seq {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


_DIGEST_COUNTS = {
    "item.created": "filed", "item.answered": "answered", "item.applied": "done", "item.completed": "done",
    "outcome.achieved": "done", "session.turn_ended": "turns", "session.waiting": "waiting",
    "session.errored": "errored", "session.crashed": "crashed",
}
# Gone by the time you're back: anything still waiting on you is on Now already.
_DIGEST_SKIP = {"session.waiting", "session.started", "session.ended", "session.resumed", "session.stopped",
                "session.turn_ended", "item.claimed", "item.moved"}
DIGEST_EVENTS_PER_PROJECT = 3


def _first_sentence(text: str, limit: int = 160) -> str:
    text = re.split(r"\s+-\s", text, maxsplit=1)[0]
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def away_digest(conn: sqlite3.Connection, since_seq: int) -> dict:
    """What happened after since_seq: counts across everything, and per project its
    notable events plus the latest session recap — the story so far, from the event
    log and Claude Code's own recaps, with no LLM call."""
    events = list_events(conn, since_seq=since_seq, min_importance=0, limit=5000)
    counts = {group: 0 for group in set(_DIGEST_COUNTS.values())}
    by_project: dict = {}
    for event in events:
        group = _DIGEST_COUNTS.get(event["verb"])
        if group:
            counts[group] += 1
        if event["project_id"] is None:
            continue
        mine = by_project.setdefault(event["project_id"], {
            "name": event["project_name"], "events": [], "counts": {}, "latest": event["seq"]})
        mine["latest"] = max(mine["latest"], event["seq"])
        if group:
            mine["counts"][group] = mine["counts"].get(group, 0) + 1
        if event["importance"] >= 1 and event["verb"] not in _DIGEST_SKIP:
            mine["events"].append(event)
    projects = []
    for project_id, mine in by_project.items():
        recap = conn.execute(
            "SELECT last_summary FROM sessions WHERE project_id = ? AND last_summary IS NOT NULL "
            "ORDER BY last_summary_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if not mine["events"] and not mine["counts"].get("turns"):
            continue
        project_events = collapse_repeats(mine["events"])
        recap_text = plain_text(recap["last_summary"]) if recap else None
        projects.append({
            "project_id": project_id,
            "name": mine["name"],
            "counts": mine["counts"],
            "events": project_events[:DIGEST_EVENTS_PER_PROJECT],
            "more": max(0, len(project_events) - DIGEST_EVENTS_PER_PROJECT),
            "notable": sum(1 for e in project_events if e["importance"] >= 2),
            "recap": recap_text,
            "gist": _first_sentence(recap_text) if recap_text else None,
            "latest": mine["latest"],
        })
    projects.sort(key=lambda p: (p["counts"].get("crashed", 0) + p["counts"].get("errored", 0), p["notable"],
                                 p["latest"]), reverse=True)
    work = project_work(conn, None)
    due = {"overdue": len(work["overdue"]), "due_soon": len(work["due_soon"]), "stuck": len(work["stuck"])}
    return {"since_seq": since_seq, "counts": counts, "projects": projects, "due": due}


def latest_event_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]


LOOK_GAP_MINUTES = 30


def project_look(conn: sqlite3.Connection, project_id: int) -> str | None:
    """When you last looked at a project, for "new since you last looked": the visit
    before your latest break of LOOK_GAP_MINUTES. Visits within one sitting keep the
    same answer, so a refresh doesn't clear what's new. None on a first visit."""
    now = datetime.now(timezone.utc)
    seen_key, look_key = f"project_seen:{project_id}", f"project_look:{project_id}"
    last_seen = get_ui_state(conn, seen_key)
    look = get_ui_state(conn, look_key)
    if last_seen and (now - datetime.fromisoformat(last_seen)).total_seconds() >= LOOK_GAP_MINUTES * 60:
        look = last_seen
        set_ui_state(conn, look_key, look)
    set_ui_state(conn, seen_key, now.isoformat())
    return look


def get_ui_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ui_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_ui_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO ui_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def list_audit_log(conn: sqlite3.Connection, decision_id: int):
    return conn.execute(
        "SELECT * FROM audit_log WHERE decision_request_id = ? ORDER BY created_at ASC",
        (decision_id,),
    ).fetchall()


def record_sync_error(conn: sqlite3.Connection, source: str, decision_id: int | None, message: str) -> None:
    """A repeat of a still-unresolved failure bumps its count instead of adding a row:
    one outage retried every poll is one problem, not thousands."""
    now = _now()
    cur = conn.execute(
        "UPDATE sync_errors SET count = count + 1, occurred_at = ? WHERE id = ("
        "SELECT id FROM sync_errors WHERE source = ? AND decision_request_id IS ? AND message = ? "
        "AND resolved_at IS NULL ORDER BY id DESC LIMIT 1)",
        (now, source, decision_id, message),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO sync_errors (occurred_at, first_occurred_at, source, decision_request_id, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, now, source, decision_id, message),
        )
    conn.commit()


def resolve_sync_errors(
    conn: sqlite3.Connection,
    sources: Iterable[str],
    *,
    before: str,
    decision_ids: Iterable[int] | None = None,
) -> None:
    """Called when a later sync to the same service succeeded. Only rows older than
    `before` (the moment that sync started) — anything the sync itself recorded is
    still current. Marked, not deleted, so `apo sync-errors` keeps full history."""
    sources = list(sources)
    query = (
        f"UPDATE sync_errors SET resolved_at = ? WHERE resolved_at IS NULL AND occurred_at < ? "
        f"AND source IN ({','.join('?' * len(sources))})"
    )
    params: list = [_now(), before, *sources]
    if decision_ids is not None:
        decision_ids = list(decision_ids)
        query += f" AND decision_request_id IN ({','.join('?' * len(decision_ids))})"
        params.extend(decision_ids)
    conn.execute(query, params)
    conn.commit()


def recent_sync_errors(
    conn: sqlite3.Connection,
    limit: int = 5,
    since_hours: int | None = None,
    unresolved_only: bool = False,
):
    """since_hours=None (the default, used by `apo sync-errors`) returns full history
    for debugging. The dashboard banner passes a window and unresolved_only, so it
    shows only failures no later successful sync has superseded."""
    query = "SELECT * FROM sync_errors"
    clauses: list[str] = []
    params: list = []
    if since_hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        clauses.append("occurred_at >= ?")
        params.append(cutoff)
    if unresolved_only:
        clauses.append("resolved_at IS NULL")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def get_repository(conn: sqlite3.Connection, repository_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,)).fetchone()


def get_or_create_repository(conn: sqlite3.Connection, project_id: int, local_path: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM repositories WHERE project_id = ? AND local_path = ?",
        (project_id, local_path),
    ).fetchone()
    if row:
        return row
    name = os.path.basename(local_path.rstrip("/")) or local_path
    collision = conn.execute(
        "SELECT 1 FROM repositories WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone()
    if collision:
        name = local_path
    return create_repository(conn, project_id, name, local_path)


def get_repository_for_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    """Return the project's repository if it has exactly one registered, else None.
    Lets repo_writer still place a decision that was filed before any repository
    existed for its project — guessing among multiple would be worse than writing
    nothing, same reasoning as ensure_repository_for_project."""
    rows = conn.execute("SELECT * FROM repositories WHERE project_id = ?", (project_id,)).fetchall()
    return rows[0] if len(rows) == 1 else None


def project_repositories(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """The project's folders, each labelled by its path from just above the folder they
    share ("acme-shop", "acme-shop/dashboard") — short, and unambiguous when names clash."""
    folders = [dict(r) for r in conn.execute("SELECT * FROM repositories WHERE project_id = ? ORDER BY id", (project_id,))]
    if folders:
        base = os.path.dirname(os.path.commonpath([f["local_path"] for f in folders]))
        for f in folders:
            f["label"] = os.path.relpath(f["local_path"], base)
    return folders


def default_repository(conn: sqlite3.Connection, project_id: int) -> dict | None:
    """Where the human's new work for a project goes when they didn't pick a folder:
    the one a session last worked in, else the first registered."""
    row = conn.execute(
        "SELECT repositories.* FROM sessions JOIN repositories ON repositories.id = sessions.repository_id "
        "WHERE repositories.project_id = ? ORDER BY sessions.last_seen_at DESC LIMIT 1", (project_id,),
    ).fetchone() or conn.execute(
        "SELECT * FROM repositories WHERE project_id = ? ORDER BY id LIMIT 1", (project_id,),
    ).fetchone()
    return dict(row) if row else None


def _project_repository(conn: sqlite3.Connection, project_id: int, repository_id: int) -> dict:
    repository = get_repository(conn, repository_id)
    if repository is None or repository["project_id"] != project_id:
        raise ValueError("That folder isn't one of this project's.")
    return dict(repository)


def repository_for_new_item(conn: sqlite3.Connection, project_id: int, repository_id: int | None) -> int | None:
    """The folder the human picked, checked against the project, or the default. An
    agent only sees work through a folder's INDEX.md, so a project with folders never
    gets an item without one from the dashboard."""
    if repository_id:
        return _project_repository(conn, project_id, repository_id)["id"]
    default = default_repository(conn, project_id)
    return default["id"] if default else None


def move_item(conn: sqlite3.Connection, item_id: int, repository_id: int, by: str = "human") -> dict:
    """Point an item at another of its project's folders: its file and INDEX.md row
    leave the old folder and appear in the new one."""
    from apo import repo_writer

    item = get_decision_request(conn, item_id)
    if item is None:
        raise ValueError(f"No item #{item_id}")
    target = _project_repository(conn, item["project_id"], repository_id)
    old = repo_writer._resolve_repository(conn, item["repository_id"], item["project_id"])
    conn.execute("UPDATE decision_requests SET repository_id = ? WHERE id = ?", (target["id"], item_id))
    conn.commit()
    _item_event(conn, item_id, "item.moved", f"Moved to {target['name']}", actor=by, importance=0)
    if old is not None and old["id"] != target["id"]:
        try:
            repo_writer.sync_repository(conn, dict(old))
        except Exception as exc:  # noqa: BLE001
            record_sync_error(conn, "repo_writer", item_id, str(exc))
    _sync_repo_files(conn, item_id)
    return target


def ensure_repository_for_project(conn: sqlite3.Connection, project_id: int, hint_path: str) -> int | None:
    """The repository a request filed from hint_path should write its markdown into.
    If hint_path is inside one of the project's registered repositories, that one —
    on every filing, not just the first. If the project has none yet, register
    hint_path. Otherwise None: guessing which of the project's repos was meant would
    be worse than requiring an explicit choice. Never registers a folder that already
    belongs to another project (a folder is one project)."""
    owner = find_repository_by_path(conn, hint_path)
    if owner is not None:
        return owner["id"] if owner["project_id"] == project_id else None
    if _is_home_or_above(hint_path):
        return None
    has_any_repository = conn.execute(
        "SELECT 1 FROM repositories WHERE project_id = ?", (project_id,)
    ).fetchone()
    if has_any_repository:
        return None
    return get_or_create_repository(conn, project_id, git_root(hint_path) or hint_path)["id"]


def _normalized_path(path: str) -> str:
    return path.rstrip("/") or "/"


def projects_sharing_a_folder(conn: sqlite3.Connection) -> list[dict]:
    """Folders registered under more than one project — each one a candidate for
    `apo project-merge`."""
    by_path: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT repositories.local_path, projects.name, projects.created_at FROM repositories "
        "JOIN projects ON projects.id = repositories.project_id"
    ):
        by_path.setdefault(_normalized_path(row["local_path"]), {})[row["name"]] = row["created_at"]
    return [
        {
            "path": path, "projects": sorted(names),
            # A rename creates the newer project, so that's the likelier name to keep.
            "newest": max(names, key=names.get),
        }
        for path, names in sorted(by_path.items()) if len(names) > 1
    ]


def merge_projects(conn: sqlite3.Connection, from_project_id: int, into_project_id: int) -> dict:
    """Fold one project into another: decisions, sessions, repositories and aliases
    move; the same folder registered under both collapses to one repository; the
    merged project's name becomes an alias of the survivor. Records are never
    deleted except the emptied project row and duplicate repository rows. Rewrites
    the survivor's in-repo markdown, which also restores any decision files a
    path-sharing sibling had pruned."""
    if from_project_id == into_project_id:
        raise ValueError("Can't merge a project into itself")
    source = conn.execute("SELECT * FROM projects WHERE id = ?", (from_project_id,)).fetchone()
    target = conn.execute("SELECT * FROM projects WHERE id = ?", (into_project_id,)).fetchone()
    if source is None or target is None:
        raise ValueError("Both projects must exist")

    target_repos = {
        _normalized_path(r["local_path"]): r
        for r in conn.execute("SELECT * FROM repositories WHERE project_id = ?", (into_project_id,))
    }
    moved_repos = 0
    for repo in conn.execute("SELECT * FROM repositories WHERE project_id = ?", (from_project_id,)).fetchall():
        twin = target_repos.get(_normalized_path(repo["local_path"]))
        if twin is not None:
            for table, column in (("decision_requests", "repository_id"), ("sessions", "repository_id")):
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (twin["id"], repo["id"]))
            conn.execute("DELETE FROM repositories WHERE id = ?", (repo["id"],))
            continue
        clash = conn.execute(
            "SELECT 1 FROM repositories WHERE project_id = ? AND name = ?", (into_project_id, repo["name"])
        ).fetchone()
        conn.execute(
            "UPDATE repositories SET project_id = ?, name = ? WHERE id = ?",
            (into_project_id, repo["local_path"] if clash else repo["name"], repo["id"]),
        )
        moved_repos += 1

    decisions = conn.execute(
        "UPDATE decision_requests SET project_id = ? WHERE project_id = ?", (into_project_id, from_project_id)
    ).rowcount
    sessions = conn.execute(
        "UPDATE sessions SET project_id = ? WHERE project_id = ?", (into_project_id, from_project_id)
    ).rowcount
    for table in ("project_aliases", "outcomes", "events"):
        conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id = ?", (into_project_id, from_project_id))
    conn.execute("DELETE FROM projects WHERE id = ?", (from_project_id,))
    conn.commit()
    add_project_alias(conn, source["name"], into_project_id)
    record_event(conn, verb="project.merged", actor="human", source="cli", project_id=into_project_id,
                 summary=f"Merged project '{source['name']}' into '{target['name']}'")

    from apo import repo_writer
    for repo in conn.execute("SELECT * FROM repositories WHERE project_id = ?", (into_project_id,)).fetchall():
        try:
            repo_writer.sync_repository(conn, dict(repo))
        except Exception as exc:  # noqa: BLE001
            record_sync_error(conn, "repo_writer", None, str(exc))
    return {"decisions": decisions, "sessions": sessions, "repositories": moved_repos}


def resolve_decision_request(conn: sqlite3.Connection, decision_id: int, cwd: str | None = None) -> bool:
    """cwd is the folder the closing agent works in, for _work_location."""
    location = _work_location(conn, decision_id, cwd)
    cur = conn.execute(
        "UPDATE decision_requests SET status = 'resolved', delivery = 'applied', resolved_at = ?, work_location = ? "
        "WHERE id = ? AND status = 'answered'",
        (_now(), location, decision_id),
    )
    conn.commit()
    resolved = cur.rowcount == 1
    if resolved:
        _record_event(conn, decision_id, "resolved")
        _sync_repo_files(conn, decision_id)
    return resolved


# --- Delivery: getting an answer to an agent ---
#
# Answering writes the answer into the repo (repo_writer). Whether an agent ever
# reads it is tracked separately: the session that asked is told on its next prompt
# (take_answers_for_session, via the UserPromptSubmit hook); an answer with no live
# session in its repo for a while comes back to the human as something to start.

STUCK_AFTER_HOURS = 1


def _record_path(conn: sqlite3.Connection, decision: dict) -> str | None:
    from apo import repo_writer

    repository = repo_writer._resolve_repository(conn, decision["repository_id"], decision["project_id"])
    if repository is None:
        return None
    return str(repo_writer.decisions_dir(repository["local_path"]) / repo_writer._decision_filename(decision))


def take_answers_for_session(conn: sqlite3.Connection, claude_session_id: str) -> list[dict]:
    """Answered requests this session filed that it hasn't been told about, marked
    delivered as they're handed over — so each is handed over once."""
    rows = conn.execute(
        "SELECT id FROM decision_requests WHERE origin_session_id = ? AND status = 'answered' "
        "AND delivery = 'pending' ORDER BY id",
        (claude_session_id,),
    ).fetchall()
    taken = []
    for row in rows:
        cur = conn.execute(
            "UPDATE decision_requests SET delivery = 'delivered', delivered_at = ?, delivered_to_session = ? "
            "WHERE id = ? AND delivery = 'pending'",
            (_now(), claude_session_id, row["id"]),
        )
        if cur.rowcount:
            decision = get_decision_request(conn, row["id"])
            decision["record_path"] = _record_path(conn, decision)
            taken.append(decision)
    conn.commit()
    for decision in taken:
        record_event(
            conn, verb="item.delivered", actor="system", source="hook", project_id=decision["project_id"],
            decision_id=decision["id"], session_id=claude_session_id,
            summary=f"Answer to #{decision['id']} delivered to the session that asked: {decision['title']}",
        )
    return taken


def list_in_transit(conn: sqlite3.Connection) -> list[dict]:
    """Answered but not applied yet. stuck: answered over STUCK_AFTER_HOURS ago with no
    live session in its repo — nothing will pick it up until the human starts one,
    which is what resume_command is for."""
    import shlex

    from apo import repo_writer

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STUCK_AFTER_HOURS)).isoformat()
    items = []
    for row in conn.execute(
        "SELECT decision_requests.id FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id "
        "WHERE status = 'answered' AND projects.archived_at IS NULL ORDER BY answered_at"
    ).fetchall():
        decision = get_decision_request(conn, row["id"])
        decision["project_name"] = get_project_name(conn, decision["project_id"])
        repository = repo_writer._resolve_repository(conn, decision["repository_id"], decision["project_id"])
        live = None
        if repository is not None:
            live_row = conn.execute(
                "SELECT claude_session_id FROM sessions WHERE status != 'ended' AND repository_id = ? "
                "ORDER BY (claude_session_id = ?) DESC, last_seen_at DESC LIMIT 1",
                (repository["id"], decision["origin_session_id"] or ""),
            ).fetchone()
            live = live_row["claude_session_id"] if live_row else None
        decision["live_session"] = live
        decision["dispatched"] = (decision["dispatched_at"] or "") > cutoff
        decision["stuck"] = live is None and not decision["dispatched"] and (decision["answered_at"] or "") < cutoff
        decision["needs_approval"] = (decision["answer_source"] or "") not in LOCAL_ANSWER_SOURCES
        decision["resume_command"] = None
        if repository is not None:
            origin_known = decision["origin_session_id"] and _session_exists(conn, decision["origin_session_id"])
            resume = f"--resume {shlex.quote(decision['origin_session_id'])} " if origin_known and not live else ""
            decision["resume_command"] = f'cd {shlex.quote(repository["local_path"])} && claude {resume}"/decisions"'
        items.append(decision)
    return items


# Answers given here, from the CLI or in chat. A Linear comment is text from outside
# this machine: it never becomes an agent's prompt without a click on the dashboard.
LOCAL_ANSWER_SOURCES = ("dashboard", "cli", "mcp")


def dispatch_prompt(decision_id: int) -> str:
    """What a background session started for one item is told. --here: the session
    exists only for this item, so the skill works it in place instead of paying for a
    second, cold context in a subagent."""
    return f"/decisions #{decision_id} --here"


def apply_plan(conn: sqlite3.Connection, decision_id: int) -> dict:
    """How "Apply in background" would run this answer. It resumes the session that
    asked only once that session has ended — resuming one still open in a terminal
    would interleave two conversations in one transcript — and otherwise starts a
    fresh background session in the repo."""
    from apo import repo_writer

    plan = {"cwd": None, "resume": None, "prompt": dispatch_prompt(decision_id), "needs_approval": False,
            "error": None}
    decision = get_decision_request(conn, decision_id)
    if decision is None:
        plan["error"] = "No such item."
        return plan
    if decision["status"] != "answered" or decision["delivery"] not in ("pending", "delivered"):
        plan["error"] = "Only an answer that hasn't been applied yet can be applied in the background."
        return plan
    recent = (datetime.now(timezone.utc) - timedelta(hours=STUCK_AFTER_HOURS)).isoformat()
    if (decision["dispatched_at"] or "") > recent:
        plan["error"] = f"Already applying in background (claude attach {decision['dispatch_agent_id']})."
        return plan
    repository = repo_writer._resolve_repository(conn, decision["repository_id"], decision["project_id"])
    if repository is None:
        plan["error"] = "No repository is registered for this project, so there's nowhere to run it."
        return plan
    plan["cwd"] = repository["local_path"]
    origin = decision["origin_session_id"]
    row = conn.execute("SELECT status, cwd FROM sessions WHERE claude_session_id = ?", (origin,)).fetchone() if origin else None
    if row is not None and row["status"] == "ended":
        plan["resume"] = origin
        if row["cwd"] and Path(row["cwd"]).is_dir():
            plan["cwd"] = row["cwd"]
    plan["needs_approval"] = (decision["answer_source"] or "") not in LOCAL_ANSWER_SOURCES
    return plan


def record_dispatch(conn: sqlite3.Connection, decision_id: int, agent_id: str | None, resumed: bool,
                    parent_session_id: str | None = None) -> None:
    """parent_session_id is the session this starts a new one from; a resume is the
    same session again, so it has no child to link."""
    decision = get_decision_request(conn, decision_id)
    conn.execute("UPDATE decision_requests SET dispatched_at = ?, dispatch_agent_id = ?, "
                 "dispatch_parent_session_id = ? WHERE id = ?",
                 (_now(), agent_id, None if resumed else parent_session_id, decision_id))
    how = "resumed the session that asked" if resumed else "new background session"
    record_event(conn, verb="delivery.dispatched", actor="human", source="dashboard",
                 project_id=decision["project_id"], decision_id=decision_id, session_id=decision["origin_session_id"],
                 summary=f"Applying #{decision_id} in background ({how}): {decision['title']}")


def dispatch_in_flight(conn: sqlite3.Connection, decision: dict) -> bool:
    """A background session was started on it recently and hasn't been seen to end.
    The item stays open until that session claims it, which can take a while."""
    recent = (datetime.now(timezone.utc) - timedelta(hours=STUCK_AFTER_HOURS)).isoformat()
    if (decision["dispatched_at"] or "") <= recent:
        return False
    agent_id = decision["dispatch_agent_id"]
    return not agent_id or conn.execute(
        "SELECT 1 FROM sessions WHERE agent_id = ? AND status = 'ended' AND ended_at >= ?",
        (agent_id, decision["dispatched_at"]),
    ).fetchone() is None


def get_session(conn: sqlite3.Connection, claude_session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE claude_session_id = ?", (claude_session_id,)).fetchone()
    return dict(row) if row else None


def close_decision_request(conn: sqlite3.Connection, decision_id: int, note: str) -> bool:
    """Force a decision (open or answered) to 'resolved' without it ever being
    answered normally — used by archive_project for decisions that no longer matter
    once a project is done. Leaves an already-answered decision's real answer_text
    alone; only fills it in with `note` when there wasn't one, so the record stays
    self-describing on its own (why it was closed, not just that it was)."""
    cur = conn.execute(
        """
        UPDATE decision_requests
        SET status = 'resolved', resolved_at = ?,
            answer_text = COALESCE(answer_text, ?),
            answer_source = COALESCE(answer_source, 'archive'),
            answered_at = COALESCE(answered_at, ?)
        WHERE id = ? AND status != 'resolved'
        """,
        (_now(), note, _now(), decision_id),
    )
    conn.commit()
    closed = cur.rowcount == 1
    if closed:
        _record_event(conn, decision_id, "closed", note)
        _clear_session_blocker(conn, decision_id)
        _sync_repo_files(conn, decision_id)
    return closed


def _is_home_or_above(path: str) -> bool:
    """The home directory, or a folder containing it, is never a project: registered
    folders match their subfolders, so registering ~ would swallow every project."""
    normalized = path.rstrip("/") or "/"
    home = str(Path.home()).rstrip("/")
    return normalized == "/" or home == normalized or home.startswith(normalized + "/")


def git_root(path: str) -> str | None:
    """The root of the git repository containing path, or None. A worktree (whose
    .git is a file pointing into the main repository's .git/worktrees/) belongs to its
    main repository. Filesystem checks only — this runs inside hooks."""
    for candidate in (Path(path), *Path(path).parents):
        if _is_home_or_above(str(candidate)):
            return None
        marker = candidate / ".git"
        if marker.is_dir():
            # A stray empty .git directory is not a repository; every real one has
            # HEAD.
            if (marker / "HEAD").is_file():
                return str(candidate)
            continue
        if marker.is_file():
            try:
                pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return str(candidate)
            gitdir = pointer[len("gitdir:"):].strip() if pointer.startswith("gitdir:") else ""
            if "/.git/worktrees/" in gitdir:
                return gitdir.split("/.git/worktrees/")[0]
            return str(candidate)
    return None


def find_repository_by_path(conn: sqlite3.Connection, path: str) -> dict | None:
    """Match a filesystem path against a registered repository's local_path, by exact
    match or by the repository being an ancestor directory of path — so starting a
    session in a subdirectory of a registered repo still resolves. Longest match wins
    when a path falls under more than one registered repo. An ancestor match doesn't
    reach into a separate git repository below it: a registered container folder must
    not swallow a new repo cloned inside it."""
    normalized = path.rstrip("/")
    best: sqlite3.Row | None = None
    for row in conn.execute("SELECT * FROM repositories"):
        repo_path = row["local_path"].rstrip("/")
        if normalized == repo_path or normalized.startswith(repo_path + "/"):
            if best is None or len(repo_path) > len(best["local_path"].rstrip("/")):
                best = row
    if best is None:
        return None
    root = git_root(normalized)
    if root is not None and len(root.rstrip("/")) > len(best["local_path"].rstrip("/")):
        return None
    return dict(best)


# --- Now: the human's queue across every project ---
#
# Derived at read time from sessions, requests and in-transit answers; nothing about
# it is stored except the human's own markers (reviewed_at, and later snooze/seen).
#
# Ranking (proposal G.3, without project priority or deadlines yet):
#   score = B * W / sqrt(E)
#   B  how blocked: agent stopped on this 1.0 · nobody waiting but work stalls 0.6 ·
#      ask while the agent keeps working 0.35 · answer nothing will pick up 0.3
#   W  how long it has waited: 1 + 0.25 * log2(1 + hours) — grows slowly
#   E  minutes of the human's effort. sqrt, not E: cost-of-delay inputs are rough, and
#      dividing by E would let a 30-second FYI outrank a blocked decision.

REVIEW_WINDOW_HOURS = 24
PROBLEM_WINDOW_HOURS = 24


def _hours_since(iso_ts: str | None) -> float:
    if not iso_ts:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds() / 3600)


def _score(blocked: float, waited_since: str | None, effort: float) -> float:
    import math

    return blocked * (1 + 0.25 * math.log2(1 + _hours_since(waited_since))) / math.sqrt(effort)


def _request_effort(decision: dict) -> float:
    if decision.get("est_minutes"):
        return max(0.5, float(decision["est_minutes"]))
    if decision["kind"] == "action":
        return 10
    if decision.get("recommendation"):
        return 1 if (decision.get("confidence") or 0) >= 80 else 2
    return 2 if decision.get("options") else 5


SNOOZE_PRESETS = ("2h", "tonight", "tomorrow", "week", "until_blocks")


def snooze_preset_until(preset: str, now: datetime | None = None) -> str | None:
    """When a snooze preset ends, in the human's local time, as UTC ISO. None for
    'until_blocks', which isn't a time."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    # Calendar arithmetic on the naive wall clock, so the offset is resolved for the
    # target day rather than carried over from today's.
    local = now.replace(tzinfo=None)
    morning = lambda day: day.replace(hour=9, minute=0, second=0, microsecond=0)  # noqa: E731
    if preset == "2h":
        until = now + timedelta(hours=2)
    elif preset == "tonight":
        evening = local.replace(hour=18, minute=0, second=0, microsecond=0)
        until = evening if local < evening else morning(local + timedelta(days=1))
    elif preset == "tomorrow":
        until = morning(local + timedelta(days=1))
    elif preset == "week":
        until = morning(local + timedelta(days=7 - local.weekday()))
    elif preset == "until_blocks":
        return None
    else:
        raise ValueError(f"Unknown snooze: {preset!r}")
    return until.astimezone(timezone.utc).isoformat()


def snooze_request(
    conn: sqlite3.Connection, decision_id: int, until: str | None = None, condition: str | None = None,
) -> None:
    """Snooze until a time and/or a condition; with neither, unsnooze."""
    conn.execute(
        "UPDATE decision_requests SET snoozed_until = ?, snooze_condition = ? WHERE id = ?",
        (until, condition, decision_id),
    )
    conn.commit()
    row = conn.execute("SELECT title, project_id FROM decision_requests WHERE id = ?", (decision_id,)).fetchone()
    if row:
        snoozed = until or condition
        record_event(
            conn, verb="item.snoozed" if snoozed else "item.unsnoozed", actor="human", source="dashboard",
            project_id=row["project_id"], decision_id=decision_id, importance=0,
            summary=f"{'Snoozed' if snoozed else 'Unsnoozed'} #{decision_id}: {row['title']}",
        )


def _snoozed(decision: dict, origin: dict | None) -> bool:
    if decision.get("snoozed_until") and decision["snoozed_until"] > _now():
        return True
    if decision.get("snooze_condition") == "until_blocks":
        return bool(origin and origin["status"] != "ended" and origin["exec_state"] == "working")
    return False


def mark_session_reviewed(conn: sqlite3.Connection, claude_session_id: str) -> None:
    conn.execute("UPDATE sessions SET reviewed_at = ? WHERE claude_session_id = ?", (_now(), claude_session_id))
    conn.commit()


def _session_name(s: dict) -> str:
    if s.get("title"):
        return s["title"]
    if s.get("cwd") and _is_home_or_above(s["cwd"]):
        return "~ (no project)"
    return s.get("project_name") or os.path.basename((s.get("cwd") or "").rstrip("/")) or s["claude_session_id"][:8]


def now_queue(conn: sqlite3.Connection, project_id: int | None = None) -> dict:
    """Lanes, each a list of items {key, kind, project_name, title, reason, since,
    effort, score, ...}: problems (faults only the human can recover), your_move
    (ranked), asks (requests whose agent keeps working), in_transit, review; plus
    quiet counts and the minutes your_move should take. project_id narrows all of
    it to one project (and drops the machine-wide problems)."""
    import math

    def mine(pid) -> bool:
        return project_id is None or pid == project_id

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(REVIEW_WINDOW_HOURS, PROBLEM_WINDOW_HOURS))).isoformat()
    sessions = [s for s in list_sessions(conn, ended_since=cutoff) if not s["ghost"]]
    by_sid = {s["claude_session_id"]: s for s in sessions}
    archived = {r["id"] for r in conn.execute("SELECT id FROM projects WHERE archived_at IS NOT NULL")}
    problems, your_move, asks, review, snoozed, scheduled = [], [], [], [], [], []
    quiet = {"working": 0, "at_rest": 0, "snoozed": 0, "usage_limit": 0}

    def session_item(s, kind, reason, **extra):
        return {
            "key": f"s:{s['claude_session_id']}", "kind": kind, "session": s,
            "project_name": s.get("project_name"), "title": _session_name(s), "reason": reason, **extra,
        }

    for s in sessions:
        if s.get("project_id") in archived or not mine(s.get("project_id")):
            continue
        status = s["display_status"]
        if status == "crashed" and _hours_since(s["ended_at"]) <= PROBLEM_WINDOW_HOURS:
            problems.append(session_item(s, "crashed", "The process died in the middle of a turn",
                                         since=s["ended_at"], resume_command=session_resume_command(s)))
        elif status == "usage_limit":
            quiet["usage_limit"] += 1
        elif status == "errored" and s["error_code"] == USAGE_LIMIT_STUCK:
            problems.append(session_item(
                s, "errored", "Hit the usage limit and won't continue on its own (auto-continue is off) — press Enter "
                "in its terminal once the limit resets, or turn on “Continue automatically at usage limit” in /config",
                since=s["last_stop_at"], resume_command=session_resume_command(s)))
        elif status == "errored":
            reason = api_error_reason(s["error_code"])
            problems.append(session_item(s, "errored", f"Stopped on an API error: {reason}" if reason
                                         else "Stopped on an API error (Claude Code didn't say which)",
                                         since=s["last_stop_at"], resume_command=session_resume_command(s)))
        elif status == "ended" and s["error_code"] in (USAGE_LIMIT_ERROR, USAGE_LIMIT_STUCK):
            if s.get("resume_scheduled_at"):
                at = datetime.fromisoformat(s["resume_scheduled_at"]).astimezone().strftime("%H:%M")
                scheduled.append(session_item(s, "limited", f"Resumes at {at} (scheduled)", since=s["ended_at"],
                                              resume_command=session_resume_command(s)))
            else:
                resets = s.get("limit_resets_at")
                your_move.append(session_item(
                    s, "limited", "Closed while waiting for the usage limit — resume it now, or when the limit resets"
                    + (f" ({datetime.fromisoformat(resets).astimezone().strftime('%H:%M')})" if resets else ""),
                    since=s["ended_at"], effort=0.5, score=_score(0.8, s["ended_at"], 0.5),
                    resume_command=session_resume_command(s),
                    resets_at_local=datetime.fromisoformat(resets).astimezone().strftime("%H:%M") if resets else None))
        elif status in ("waiting_permission", "waiting_input"):
            kind = "permission" if status == "waiting_permission" else "input"
            effort = 0.5 if kind == "permission" else 1
            prefix = "Needs permission" if kind == "permission" else "Asked you something"
            since = s["waiting_since"] or s["last_seen_at"]
            your_move.append(session_item(
                s, kind, f"{prefix}: {s['waiting_detail']}" if s["waiting_detail"] else prefix,
                since=since, effort=effort, score=_score(1.0, since, effort),
            ))
        elif s["status"] != "ended":
            if s["exec_state"] == "working":
                quiet["working"] += 1
            else:
                quiet["at_rest"] += 1
            finished = s.get("last_stop_at")
            if (s["exec_state"] == "at_rest" and not s["needs_you"] and finished
                    and _hours_since(finished) <= REVIEW_WINDOW_HOURS
                    and (not s.get("reviewed_at") or s["reviewed_at"] < finished)):
                review.append({**session_item(s, "review", s.get("last_summary") or "Finished its turn",
                                              since=finished), "key": f"v:{s['claude_session_id']}"})

    outcome_titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM outcomes")}
    for row in conn.execute(
        "SELECT decision_requests.id FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id "
        "WHERE decision_requests.status = 'open' AND projects.archived_at IS NULL "
        "AND decision_requests.kind IN ('decision', 'action', 'review', 'wait')"
    ).fetchall():
        d = get_decision_request(conn, row["id"])
        if not mine(d["project_id"]):
            continue
        if open_blockers(conn, d["id"]):
            continue  # not actionable until what it waits on is done
        origin = by_sid.get(d["origin_session_id"]) if d["origin_session_id"] else None
        effort = _request_effort(d)
        reason = d.get("summary") or d["question"]
        if d.get("outcome_id") in outcome_titles:
            reason = f"Step in “{outcome_titles[d['outcome_id']]}”: {reason}"
        item = {
            "key": f"r:{d['id']}", "kind": d["kind"], "decision": d, "project_name": get_project_name(conn, d["project_id"]),
            "title": d["title"], "reason": reason, "since": d["created_at"], "effort": effort,
            "due_on": d.get("due_on"), "due_bucket": due_bucket(d.get("due_on")),
            "stuck": stuck_info(d["created_at"], "waiting on you"),
        }
        if d["kind"] == "wait":
            # Someone else's move; it only becomes the human's when the follow-up date passes.
            if d.get("follow_up_at") and d["follow_up_at"] <= _now():
                your_move.append({**item, "effort": 2, "score": _score(0.5, d["follow_up_at"], 2),
                                  "reason": f"Check on this — waiting on {d.get('waiting_on') or 'someone else'}"})
            continue
        if _snoozed(d, origin):
            snoozed.append({**item, "snoozed_until": d["snoozed_until"], "snooze_condition": d["snooze_condition"]})
        elif origin and origin["status"] != "ended" and origin["exec_state"] == "working":
            asks.append({**item, "score": _score(0.35, d["created_at"], effort)})
        else:
            blocked = 1.0 if origin and origin["status"] != "ended" else 0.6
            your_move.append({**item, "score": _score(blocked, d["created_at"], effort)})

    in_transit = list(scheduled)
    for t in list_in_transit(conn):
        if not mine(t["project_id"]):
            continue
        item = {
            "key": f"t:{t['id']}", "kind": "transit", "decision": t, "project_name": t["project_name"],
            "title": t["title"], "since": t["answered_at"], "resume_command": t["resume_command"],
        }
        if t["stuck"]:
            your_move.append({**item, "effort": 1, "score": _score(0.3, t["answered_at"], 1),
                              "reason": "Answered, but nothing is running in that repo to pick it up"})
        else:
            reason = ("Applying in background" if t["dispatched"]
                      else "Delivered to the session that asked" if t["delivery"] == "delivered"
                      else "Waiting for a session in that repo to pick it up")
            in_transit.append({**item, "reason": reason})

    failing = recent_sync_errors(conn, limit=50, since_hours=PROBLEM_WINDOW_HOURS, unresolved_only=True)
    if failing and project_id is None:
        count = sum(e["count"] for e in failing)
        problems.append({"key": "sync", "kind": "sync", "project_name": None, "title": "Linear sync is failing",
                         "reason": f"{failing[0]['message'][:120]} (×{count})", "since": failing[0]["occurred_at"]})
    for shared in projects_sharing_a_folder(conn) if project_id is None else []:
        problems.append({"key": f"folder:{shared['path']}", "kind": "folder", "project_name": None,
                         "title": "Two projects share one folder",
                         "reason": f"{', '.join(shared['projects'])} at {shared['path']}", "since": None})

    for outcome in list_outcomes(conn, project_id=project_id, statuses=("active",)):
        if outcome["progress"]["ready_to_close"]:
            review.append({
                "key": f"o:{outcome['id']}", "kind": "outcome", "outcome": outcome, "project_name": outcome["project_name"],
                "title": outcome["title"], "since": outcome["updated_at"],
                "reason": "Every step is done — is it achieved?" + (f" Done when: {outcome['done_when']}" if outcome["done_when"] else ""),
            })

    weights = {r["name"]: _PRIORITY_WEIGHT.get(r["priority"] or "normal", 1.0)
               for r in conn.execute("SELECT name, priority FROM projects")}
    for item in (*your_move, *asks):
        item["score"] = item.get("score", 0) * weights.get(item.get("project_name"), 1.0)
    your_move.sort(key=lambda i: i["score"], reverse=True)
    asks.sort(key=lambda i: i["score"], reverse=True)
    review.sort(key=lambda i: i["since"] or "", reverse=True)
    quiet["snoozed"] = len(snoozed)
    return {
        "problems": problems, "your_move": your_move, "asks": asks, "in_transit": in_transit, "review": review,
        "snoozed": snoozed, "quiet": quiet,
        "your_move_minutes": math.ceil(sum(i.get("effort", 0) for i in your_move)),
    }


def time_boxed_plan(queue: dict, minutes: float | None) -> list[dict]:
    """"I have N minutes": your moves by score per minute, as many as fit — and
    never nothing, so the best one is in even if it alone overruns. None = all."""
    items = queue["your_move"]
    if minutes is None:
        return list(items)
    ranked = sorted(items, key=lambda i: i["score"] / max(i.get("effort") or 1, 0.5), reverse=True)
    plan, used = [], 0.0
    for item in ranked:
        effort = item.get("effort") or 1
        if used + effort <= minutes:
            plan.append(item)
            used += effort
    return plan or ranked[:1]


BULK_ACCEPT_MIN_CONFIDENCE = 85


def bulk_accept_candidates(conn: sqlite3.Connection, project_id: int | None = None,
                           min_confidence: int = BULK_ACCEPT_MIN_CONFIDENCE) -> list[dict]:
    """Open decisions whose agent picked an option, is at least min_confidence
    sure, and marked that option reversible. Unknown reversibility counts as a
    one-way door: those always get a real look."""
    params: list = [min_confidence]
    where = ""
    if project_id is not None:
        where = " AND decision_requests.project_id = ?"
        params.append(project_id)
    candidates = []
    for row in conn.execute(
        "SELECT decision_requests.id FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id "
        "WHERE decision_requests.status = 'open' AND decision_requests.kind = 'decision' AND projects.archived_at IS NULL "
        f"AND recommended_option IS NOT NULL AND confidence >= ?{where} ORDER BY decision_requests.id",
        params,
    ).fetchall():
        d = get_decision_request(conn, row["id"])
        options = d["options"] or []
        pick = d["recommended_option"]
        if not 0 <= pick < len(options) or options[pick].get("reversible") is not True:
            continue
        if open_blockers(conn, d["id"]):
            continue
        d["project_name"] = get_project_name(conn, d["project_id"])
        d["pick"] = options[pick]
        candidates.append(d)
    return candidates


def bulk_accept(conn: sqlite3.Connection, ids: Iterable[int]) -> list[int]:
    """Eligibility is checked again here, not trusted from the form."""
    eligible = {d["id"]: d for d in bulk_accept_candidates(conn)}
    accepted = []
    for decision_id in ids:
        d = eligible.get(decision_id)
        if d is None:
            continue
        kind = "recommendation" if d.get("recommendation") else "option"
        text, kind, chosen = compose_answer(d, kind, option=d["recommended_option"])
        if answer_decision_request(conn, decision_id, text, answer_kind=kind, answer_option=chosen):
            accepted.append(decision_id)
    return accepted


BULK_ACTIONS = ("snooze", "cancel", "reassign", "due", "done")
_BULK_WORDS = {"snooze": "snoozed", "cancel": "cancelled", "reassign": "reassigned", "due": "updated", "done": "done"}


def bulk_apply(conn: sqlite3.Connection, action: str, ids: Iterable[int], *, preset: str | None = None,
               owner: str | None = None, due_on: str | None = None) -> dict:
    """Run one single-item action over many rows. Arguments are validated up front so a
    typo fails the whole call; after that each row is judged on its own and rows the
    action doesn't apply to (closed, wrong kind, gone) are skipped, not errors."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"Unknown bulk action: {action!r}")
    if action == "snooze" and preset not in SNOOZE_PRESETS:
        raise ValueError(f"Unknown snooze: {preset!r}")
    if action == "reassign" and owner not in ("me", "agent"):
        raise ValueError(f"owner must be 'me' or 'agent', got {owner!r}")
    if action == "due":
        due_on = _due_date(due_on)
    applied, skipped = [], 0
    for item_id in dict.fromkeys(ids):
        row = conn.execute("SELECT kind, status FROM decision_requests WHERE id = ?", (item_id,)).fetchone()
        if row is None or row["status"] not in ("open", "in_progress"):
            skipped += 1
            continue
        ok = True
        if action == "done":
            if row["kind"] not in ("action", "task", "wait"):
                ok = False
            else:
                snapshot_for_undo(conn, item_id, "done")
                ok = complete_step(conn, item_id, by="human")
                if not ok:
                    clear_undo(conn, item_id)
        elif action == "cancel":
            snapshot_for_undo(conn, item_id, "cancel")
            ok = cancel_request(conn, item_id, "Cancelled by the user")
            if not ok:
                clear_undo(conn, item_id)
        elif action == "snooze":
            snapshot_for_undo(conn, item_id, "snooze")
            snooze_request(conn, item_id, until=snooze_preset_until(preset),
                           condition="until_blocks" if preset == "until_blocks" else None)
        elif action == "reassign":
            ok = row["kind"] in ("action", "task")
            if ok:
                reassign_step(conn, item_id, owner)
        else:
            set_due(conn, item_id, due_on)
        if ok:
            applied.append(item_id)
        else:
            skipped += 1
    summary = f"{len(applied)} {_BULK_WORDS[action]}" + (f", {skipped} skipped" if skipped else "")
    return {"action": action, "applied": len(applied), "skipped": skipped, "ids": applied, "summary": summary}


# --- Saved views ---

_VIEW_PATH = re.compile(r"^/(search|projects/(work|decisions)|projects/\d+/(work|decisions))$")


def save_view(conn: sqlite3.Connection, name: str, path: str, query: str) -> int:
    """Remember a page's filter (its query string) under a name. Only this app's own
    list pages are accepted, so a view can never point somewhere else."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A saved view needs a name")
    if not _VIEW_PATH.match(path or ""):
        raise ValueError(f"Not a page that can be saved: {path!r}")
    query = (query or "").lstrip("?")
    conn.execute(
        "INSERT INTO saved_views (name, path, query, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET path = excluded.path, query = excluded.query",
        (name, path, query, _now()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM saved_views WHERE name = ?", (name,)).fetchone()["id"]


def list_saved_views(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, path, query FROM saved_views ORDER BY name COLLATE NOCASE").fetchall()
    return [{**dict(r), "href": r["path"] + (f"?{r['query']}" if r["query"] else "")} for r in rows]


def delete_saved_view(conn: sqlite3.Connection, view_id: int) -> bool:
    cur = conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
    conn.commit()
    return bool(cur.rowcount)


# Interruption budget (browser notifications). kind: push | reminder | held.
NOTIFY_AFTER_MINUTES = 30
NOTIFY_DAILY_LIMIT = 6
NOTIFY_GAP_MINUTES = 20
NOTIFY_PROJECT_GAP_MINUTES = 60
NOTIFY_REMIND_AFTER_HOURS = 4
NOTIFY_FLOW_MINUTES = 2
NOTIFY_DEFAULT_HOURS = "8-22"


def _within_active_hours(conn: sqlite3.Connection, now: datetime) -> bool:
    try:
        start, end = (int(x) for x in (get_ui_state(conn, "notify_hours") or NOTIFY_DEFAULT_HOURS).split("-"))
    except ValueError:
        start, end = (int(x) for x in NOTIFY_DEFAULT_HOURS.split("-"))
    hour = now.astimezone().hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def _in_flow(conn: sqlite3.Connection, now: datetime) -> bool:
    """You prompted a session a moment ago and it's still working on it: you're
    watching your own agent, and anything else can wait for its turn to end."""
    since = (now - timedelta(minutes=NOTIFY_FLOW_MINUTES)).isoformat()
    return conn.execute(
        "SELECT 1 FROM sessions WHERE status != 'ended' AND exec_state = 'working' AND reviewed_at >= ? LIMIT 1",
        (since,),
    ).fetchone() is not None


def _notify_href(item: dict) -> str:
    if item.get("decision"):
        return f"/decisions/{item['decision']['id']}"
    if item.get("session"):
        return f"/sessions#session-card-{item['session']['id']}"
    return "/"


def notifications_due(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """What to show as browser notifications right now, recorded as sent so a
    second open tab doesn't repeat them; plus moves that are gone (retract) and how
    many were held back today by the budget."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    queue = now_queue(conn)
    live = {i["key"]: i for i in queue["your_move"]}
    out: dict = {"notify": [], "retract": [], "held": 0}

    for row in conn.execute(
        "SELECT DISTINCT item_key FROM notifications WHERE kind != 'held' AND retracted_at IS NULL"
    ).fetchall():
        if row["item_key"] not in live:
            conn.execute("UPDATE notifications SET retracted_at = ? WHERE item_key = ? AND retracted_at IS NULL",
                         (now_iso, row["item_key"]))
            out["retract"].append(row["item_key"])

    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    if _within_active_hours(conn, now) and not _in_flow(conn, now):
        sent = conn.execute(
            "SELECT item_key, project_id, sent_at FROM notifications WHERE kind != 'held' ORDER BY sent_at"
        ).fetchall()
        today = [r for r in sent if r["sent_at"] >= local_midnight]
        last_sent = sent[-1]["sent_at"] if sent else None
        pinned = {r["id"] for r in conn.execute("SELECT id FROM projects WHERE pinned_at IS NOT NULL OR priority = 'hot'")}
        background = {r["id"] for r in conn.execute("SELECT id FROM projects WHERE priority = 'background'")}
        project_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM projects")}
        due_after = (now - timedelta(minutes=NOTIFY_AFTER_MINUTES)).isoformat()
        for key, item in live.items():
            history = [r for r in sent if r["item_key"] == key]
            project_id = project_ids.get(item.get("project_name"))
            if project_id in background:
                continue
            if history:
                if len(history) >= 2 or history[0]["sent_at"] > (now - timedelta(hours=NOTIFY_REMIND_AFTER_HOURS)).isoformat():
                    continue
                kind = "reminder"
            elif project_id in pinned or (item.get("since") or now_iso) <= due_after:
                kind = "push"
            else:
                continue
            gap = (now - timedelta(minutes=NOTIFY_GAP_MINUTES)).isoformat()
            project_gap = (now - timedelta(minutes=NOTIFY_PROJECT_GAP_MINUTES)).isoformat()
            over = (len(today) >= NOTIFY_DAILY_LIMIT or (last_sent and last_sent > gap)
                    or any(r["project_id"] == project_id and r["sent_at"] > project_gap for r in sent if project_id))
            if over:
                if not conn.execute("SELECT 1 FROM notifications WHERE item_key = ? AND kind = 'held' AND sent_at >= ?",
                                    (key, local_midnight)).fetchone():
                    conn.execute("INSERT INTO notifications (item_key, project_id, kind, sent_at) VALUES (?, ?, 'held', ?)",
                                 (key, project_id, now_iso))
                continue
            conn.execute("INSERT INTO notifications (item_key, project_id, kind, sent_at) VALUES (?, ?, ?, ?)",
                         (key, project_id, kind, now_iso))
            record = {"item_key": key, "project_id": project_id, "sent_at": now_iso}
            sent.append(record)
            today.append(record)
            last_sent = now_iso
            out["notify"].append({
                "key": key, "href": _notify_href(item),
                "title": item["title"] + (f" — {item['project_name']}" if item.get("project_name") else ""),
                "body": ("Reminder: " if kind == "reminder" else "") + (item.get("reason") or ""),
            })
    out["held"] = conn.execute(
        "SELECT COUNT(DISTINCT item_key) FROM notifications AS h WHERE kind = 'held' AND sent_at >= ? "
        "AND NOT EXISTS (SELECT 1 FROM notifications AS p WHERE p.item_key = h.item_key AND p.kind != 'held')",
        (local_midnight,),
    ).fetchone()[0]
    conn.commit()
    return out


def weekly_report(conn: sqlite3.Connection, project_id: int, start: datetime, end: datetime) -> dict:
    """One project's week: what got done and decided, goals achieved, what's blocked on
    you and overdue at the end, how long agents waited on you, what was filed, and a
    4-week trend of work done. From the database alone, so a week reads the same every
    time it's opened."""
    import statistics

    s, e = start.isoformat(), end.isoformat()
    until = min(end, datetime.now(timezone.utc))
    done_at = "CASE WHEN kind IN ('task', 'wait') THEN resolved_at ELSE answered_at END"
    not_archive = "COALESCE(answer_source, '') != 'archive'"

    def items(where: str, params: tuple, order: str) -> list[dict]:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, title, answer_text, labels, created_at, answered_at, resolved_at, due_on, "
            f"{done_at} AS done_at FROM decision_requests WHERE project_id = ? AND {where} ORDER BY {order}",
            (project_id, *params),
        )]

    done = items(f"kind != 'decision' AND status IN ('resolved', 'answered') AND {not_archive} "
                 f"AND {done_at} >= ? AND {done_at} < ?", (s, e), "done_at")
    decided = items(f"kind = 'decision' AND answered_at >= ? AND answered_at < ? AND {not_archive}", (s, e), "answered_at")
    blocked = items("status = 'open' AND COALESCE(assignee, 'human') = 'human' "
                    "AND kind IN ('decision', 'action', 'review') AND created_at < ?", (e,), "created_at")
    overdue = items("status IN ('open', 'in_progress', 'answered') AND due_on < ?",
                    (end.astimezone().date().isoformat(),), "due_on")
    for item in blocked:
        item["waited_hours"] = int((until - datetime.fromisoformat(item["created_at"])).total_seconds() // 3600)
    goals = [dict(r) for r in conn.execute(
        "SELECT id, title, achieved_at FROM outcomes WHERE project_id = ? AND status = 'achieved' "
        "AND achieved_at >= ? AND achieved_at < ? ORDER BY achieved_at", (project_id, s, e))]

    waited = 0.0
    latencies = []
    for r in conn.execute(
        "SELECT created_at, answered_at, resolved_at, status FROM decision_requests WHERE project_id = ? "
        "AND kind IN ('decision', 'action') AND requested_by = 'agent' AND status != 'cancelled' AND created_at < ? "
        "AND (status IN ('open', 'in_progress') OR COALESCE(answered_at, resolved_at) >= ?)", (project_id, e, s),
    ):
        asked = datetime.fromisoformat(r["created_at"])
        answered = r["answered_at"] or r["resolved_at"]
        ended = min(until, datetime.fromisoformat(answered)) if answered else until
        waited += max(0.0, (ended - max(asked, start)).total_seconds())
        if r["answered_at"] and s <= r["answered_at"] < e:
            latencies.append((datetime.fromisoformat(r["answered_at"]) - asked).total_seconds() / 60)

    trend = []
    for weeks_back in range(3, -1, -1):
        w_end = end - timedelta(days=7 * weeks_back)
        w_start = w_end - timedelta(days=7)
        trend.append(conn.execute(
            f"SELECT COUNT(*) FROM decision_requests WHERE project_id = ? AND kind != 'decision' "
            f"AND status IN ('resolved', 'answered') AND {not_archive} AND {done_at} >= ? AND {done_at} < ?",
            (project_id, w_start.isoformat(), w_end.isoformat()),
        ).fetchone()[0])

    return {
        "start": s, "end": e, "done": done, "decided": decided, "goals_achieved": goals, "blocked": blocked,
        "overdue": overdue,
        "filed": conn.execute("SELECT COUNT(*) FROM decision_requests WHERE project_id = ? AND created_at >= ? "
                              "AND created_at < ?", (project_id, s, e)).fetchone()[0],
        "waited_hours": round(waited / 3600),
        "median_answer_minutes": round(statistics.median(latencies)) if latencies else None,
        "trend": trend,
    }


def week_overview(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict:
    """Every live project's week side by side: finished (done work, achieved goals,
    sessions that ended done or partial), slipped (overdue, abandoned or crashed
    sessions, items reopened or retried), and stuck (waiting on the human). From
    recorded data only, no model."""
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    s, e = start.isoformat(), end.isoformat()

    def sessions(project_id: int, verdicts: tuple) -> list[dict]:
        return [dict(r) for r in conn.execute(
            "SELECT id, claude_session_id, title, verdict, verdict_reason, ended_at FROM sessions WHERE project_id = ? "
            f"AND verdict IN ({','.join('?' * len(verdicts))}) AND ended_at >= ? AND ended_at < ? ORDER BY ended_at",
            (project_id, *verdicts, s, e))]

    reopened_sql = ("SELECT DISTINCT d.id, d.title FROM audit_log a JOIN decision_requests d ON d.id = a.decision_request_id "
                    "WHERE d.project_id = ? AND a.event_type = 'reopened' AND a.created_at >= ? AND a.created_at < ?")
    retried_sql = ("SELECT DISTINCT d.id, d.title FROM task_attempts t JOIN decision_requests d ON d.id = t.task_id "
                   "WHERE d.project_id = ? AND t.event = 'retried' AND t.at >= ? AND t.at < ?")
    projects = []
    for row in conn.execute("SELECT id FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE").fetchall():
        report = weekly_report(conn, row["id"], start, end)
        entry = {
            "project": dict(conn.execute("SELECT id, name FROM projects WHERE id = ?", (row["id"],)).fetchone()),
            "finished": {"items": report["done"], "goals": report["goals_achieved"],
                         "sessions": sessions(row["id"], ("done", "partial"))},
            "slipped": {"overdue": report["overdue"], "sessions": sessions(row["id"], ("abandoned", "crashed")),
                        "reopened": [dict(r) for r in conn.execute(reopened_sql, (row["id"], s, e))],
                        "retried": [dict(r) for r in conn.execute(retried_sql, (row["id"], s, e))]},
            "stuck": report["blocked"],
        }
        entry["counts"] = {
            "finished": len(entry["finished"]["items"]) + len(entry["finished"]["goals"]) + len(entry["finished"]["sessions"]),
            "slipped": sum(len(v) for v in entry["slipped"].values()),
            "stuck": len(entry["stuck"]),
        }
        projects.append(entry)
    projects.sort(key=lambda p: -sum(p["counts"].values()))
    return {
        "start": s, "end": e, "projects": projects,
        "totals": {k: sum(p["counts"][k] for p in projects) for k in ("finished", "slipped", "stuck")},
    }


OUTCOME_STALL_DAYS = 7


def outcome_trend(conn: sqlite3.Connection, outcome_id: int, days: int = 30) -> dict:
    """Steps done and steps planned, per day, for a burn-up chart, and how long an
    active outcome with work left has gone without a step finishing (None when it
    hasn't gone OUTCOME_STALL_DAYS, or isn't active). Steps finish via answered_at
    (decisions, actions) or resolved_at (tasks, waits)."""
    outcome = get_outcome(conn, outcome_id)
    if outcome is None:
        return {"days": [], "done": [], "total": [], "stalled_days": None}
    rows = conn.execute(
        "SELECT created_at, status, CASE WHEN kind IN ('task', 'wait') THEN resolved_at ELSE answered_at END AS done_at "
        "FROM decision_requests WHERE outcome_id = ? AND status != 'cancelled'", (outcome_id,)).fetchall()
    today = datetime.now().astimezone().date()

    def day(stamp: str):
        return datetime.fromisoformat(stamp).astimezone().date()

    created = day(outcome["created_at"])
    first = max(created, today - timedelta(days=days - 1))
    span = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    planned = [day(r["created_at"]) for r in rows]
    finished = [day(r["done_at"]) for r in rows if r["status"] in ("answered", "resolved") and r["done_at"]]
    stalled = None
    if outcome["status"] == "active" and len(rows) > len(finished):
        gap = (today - max(finished, default=created)).days
        stalled = gap if gap >= OUTCOME_STALL_DAYS else None
    return {
        "days": [d.isoformat() for d in span],
        "done": [sum(1 for f in finished if f <= d) for d in span],
        "total": [sum(1 for c in planned if c <= d) for d in span],
        "stalled_days": stalled,
    }


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """'7d', '2w' or a local date, YYYY-MM-DD (from its midnight)."""
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d{1,4})([dw])", text.strip().lower())
    if m:
        return now - timedelta(days=int(m.group(1)) * (7 if m.group(2) == "w" else 1))
    try:
        return datetime.combine(date.fromisoformat(text.strip()), datetime.min.time()).astimezone()
    except ValueError:
        raise ValueError(f"since is a number of days like 7d, weeks like 2w, or a date YYYY-MM-DD, not {text!r}") from None


def _status_item(item: dict, effort: dict | None = None) -> dict:
    out = {"id": item["id"], "kind": item["kind"], "title": item["title"]}
    for key in ("state", "owner", "since", "due_on", "done_at"):
        if item.get(key):
            out[key] = item[key]
    if effort:
        out["elapsed_seconds"] = effort["elapsed_seconds"]
        if effort.get("tokens"):
            out["tokens"] = effort["tokens"]["total"]
    return out


def status_report(conn: sqlite3.Connection, project_id: int | None, since: datetime,
                  until: datetime | None = None) -> dict:
    """What a project (None: every active one) did and where it stands, for pasting:
    done and decided in [since, until), then right now what's in progress, waiting on
    you, waiting on others, stuck (claimed by a session that ended) and next (ready
    for an agent). Built on weekly_report and project_work, so it agrees with them."""
    until = until or datetime.now(timezone.utc)
    if project_id is None:
        rows = conn.execute("SELECT id, name FROM projects WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE").fetchall()
    else:
        rows = conn.execute("SELECT id, name FROM projects WHERE id = ?", (project_id,)).fetchall()
    projects = []
    for row in rows:
        week = weekly_report(conn, row["id"], since, until)
        work = project_work(conn, row["id"])
        stuck = stuck_claims(conn, row["id"])
        stuck_ids = {t["id"] for t in stuck}
        status = project_status(conn, row["id"])
        projects.append({
            "id": row["id"], "name": row["name"],
            "status": {"value": status["value"], "note": status["note"] or status["reason"]},
            "done": [_status_item(i, task_effort(conn, i["id"])) for i in week["done"]],
            "goals_achieved": [{"id": g["id"], "title": g["title"]} for g in week["goals_achieved"]],
            "decided": [{**_status_item(i), "answer": plain_text(i["answer_text"] or "")[:200]} for i in week["decided"]],
            "in_progress": [_status_item(i) for i in work["in_progress"] if i["id"] not in stuck_ids],
            "waiting_on_you": [_status_item(i) for i in work["needs_you"]],
            "waiting_on_others": [_status_item(i) for i in work["waiting"]],
            "stuck": [_status_item({**t, "since": t.get("claimed_at")}) for t in stuck],
            "next": [_status_item(i) for i in work["ready"]],
        })
    return {"since": since.isoformat(), "until": until.isoformat(), "generated_at": _now(), "projects": projects}


_STATUS_SECTIONS = (("done", "Done"), ("decided", "Decided"), ("in_progress", "In progress"),
                    ("waiting_on_you", "Waiting on you"), ("waiting_on_others", "Waiting on others"),
                    ("stuck", "Stuck"), ("next", "Next"))


def status_markdown(report: dict) -> str:
    since, until = datetime.fromisoformat(report["since"]).astimezone(), datetime.fromisoformat(report["until"]).astimezone()
    lines = [f"# Status — {since:%b} {since.day} to {until:%b} {until.day}", ""]
    for p in report["projects"]:
        lines += [f"## {p['name']}", f"Status: {p['status']['value'].replace('_', ' ')}"
                  + (f" ({p['status']['note']})" if p["status"]["note"] else ""), ""]
        sections = {key: [_status_line(i) for i in p[key]] for key, _ in _STATUS_SECTIONS}
        sections["done"] = [f"Goal achieved: {g['title']}" for g in p["goals_achieved"]] + sections["done"]
        shown = False
        for key, label in _STATUS_SECTIONS:
            if sections[key]:
                shown = True
                lines += [f"**{label} ({len(sections[key])})**", *[f"- {row}" for row in sections[key]], ""]
        if not shown:
            lines += ["Nothing to report.", ""]
    return "\n".join(lines).rstrip() + "\n"


def _status_line(item: dict) -> str:
    text = f"#{item['id']} {item['title']}"
    if item.get("answer"):
        text += f": {item['answer']}"
    took = effort_text({"elapsed_seconds": item["elapsed_seconds"], "tokens": {"total": item["tokens"]} if item.get("tokens") else None}) \
        if "elapsed_seconds" in item else None
    extras = [e for e in (took, f"due {item['due_on']}" if item.get("due_on") else None) if e]
    return text + (f" ({', '.join(extras)})" if extras else "")


def court_metrics(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Time in your court, counting only agents' requests (decisions and actions an
    agent filed and is waiting on): minutes they waited on you since local midnight,
    the oldest move you still hold, and your median answer time over 7 days."""
    import statistics

    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    rows = conn.execute(
        "SELECT decision_requests.id, title, status, decision_requests.created_at, answered_at, resolved_at FROM decision_requests "
        "JOIN projects ON projects.id = decision_requests.project_id "
        "WHERE kind IN ('decision', 'action') AND requested_by = 'agent' AND status != 'cancelled' "
        "AND projects.archived_at IS NULL AND (status IN ('open', 'in_progress') OR answered_at >= ? OR resolved_at >= ?)",
        (week_ago.isoformat(), midnight.isoformat()),
    ).fetchall()
    waited = 0.0
    moves = 0
    oldest = None
    latencies = []
    for r in rows:
        asked = datetime.fromisoformat(r["created_at"])
        still_open = r["status"] in ("open", "in_progress")
        ended = now if still_open else datetime.fromisoformat(r["answered_at"] or r["resolved_at"])
        overlap = max(0.0, (min(ended, now) - max(asked, midnight)).total_seconds() / 60)
        waited += overlap
        moves += overlap > 0
        if still_open and (oldest is None or asked < oldest[0]):
            oldest = (asked, r)
        if r["answered_at"] and datetime.fromisoformat(r["answered_at"]) >= week_ago:
            latencies.append((datetime.fromisoformat(r["answered_at"]) - asked).total_seconds() / 60)
    return {
        "waited_today_minutes": round(waited),
        "moves_waited_today": moves,
        "oldest": {"id": oldest[1]["id"], "title": oldest[1]["title"],
                   "hours": int((now - oldest[0]).total_seconds() // 3600)} if oldest else None,
        "median_answer_minutes": round(statistics.median(latencies)) if latencies else None,
        "answered_this_week": len(latencies),
    }


def session_resume_command(s: dict) -> str | None:
    import shlex

    folder = s.get("cwd")
    if not folder:
        return None
    return f"cd {shlex.quote(folder)} && claude --resume {shlex.quote(s['claude_session_id'])}"


# --- Session fleet tracking ---
#
# Every function here is called from a Claude Code hook (see cli.py's session-*
# subcommands) running as a plain shell/script process with zero LLM cost — no
# polling loop, no scheduled wake-up. This deliberately avoids the anti-pattern in
# Paperclip's heartbeat design (github.com/paperclipai/paperclip issue #3401), which
# wakes the agent's LLM on a timer just to check for work. None of these may ever
# raise: the hook wrapper calling them is fail-open by contract (see
# cli.py's _read_hook_payload/_cmd_session_*), but staying no-op-safe here too means
# the safety property doesn't depend on that wrapper alone.

# exec_state, what the session is doing right now:
#   starting            registered, no turn yet (also after /clear or a resume)
#   working             a prompt or a tool call since the last Stop
#   waiting_permission  a permission dialog is up — needs the human
#   waiting_input       a question/dialog (AskUserQuestion, elicitation) — needs the human
#   at_rest             the turn ended; sitting at the prompt. Normal, never an alarm.
#   errored             the turn ended on an API error (StopFailure) — needs the human
#   ended               SessionEnd, or the process was found gone
# Hooks write it as events happen; reconcile_live_sessions corrects it against
# Claude Code's own live view while someone is looking at the dashboard.

STALE_MINUTES = 10  # a *working* session with no hook activity for this long is
# flagged quiet: possibly a long tool call, possibly hung. Only working sessions — a
# session at rest or waiting on the human is supposed to be silent. Computed at read
# time only; whether the process is actually alive is reconcile_live_sessions's job.

GHOST_MINUTES = 5  # registered but never took a turn and never seen alive by
# reconcile (e.g. pre-spawned background workers): hidden once it's clearly not
# going to. When reconcile can run it ends these outright; this covers when it can't.

GONE_GRACE_MINUTES = 2  # a session missing from `claude agents` but seen this
# recently is a race (still starting up), not a disappearance.

_MID_TURN_STATES = ("working", "waiting_permission", "waiting_input")
_LEGACY_EXEC_STATE = {"running": "working", "idle": "at_rest", "blocked": "at_rest", "ended": "ended"}


def _turn_guard(event_at: str) -> tuple[str, tuple]:
    """Mid-turn writes come from async hooks, which can land after the same turn's
    synchronous Stop. An event that happened before the last Stop is stale."""
    return "(last_stop_at IS NULL OR last_stop_at <= ?)", (event_at,)


def _auto_register_repository_for_cwd(conn: sqlite3.Connection, cwd: str) -> sqlite3.Row | None:
    """A session whose cwd matches no registered repository gets one created on the
    spot, named from the folder's own basename — the same name a human would use to
    refer to it — rather than sitting in the fleet view's "Unregistered" bucket
    forever just because nothing's filed a decision from that project yet. The folder
    is the git repository root when there is one (a session started in src/api is the
    repo's, and a worktree is its main repo's). Attaches to an existing same-name
    project instead of making a duplicate. None for the home directory or above."""
    if _is_home_or_above(cwd):
        return None
    folder = git_root(cwd) or cwd.rstrip("/")
    repository = find_repository_by_path(conn, folder)
    if repository is not None:
        return repository
    name = os.path.basename(folder) or folder
    project = get_or_create_project(conn, name)
    return get_or_create_repository(conn, project["id"], folder)


def _repository_for_session_cwd(conn: sqlite3.Connection, cwd: str | None):
    if not cwd:
        return None
    return find_repository_by_path(conn, cwd) or _auto_register_repository_for_cwd(conn, cwd)


def _repository_for_transcript(conn: sqlite3.Connection, transcript_path: str | None):
    """Claude Code keeps a session's transcript under the folder it was created in,
    encoded (every non-alphanumeric character becomes "-"). A session resumed from ~
    still belongs to that folder."""
    if not transcript_path:
        return None
    encoded = Path(transcript_path).parent.name
    for repo in conn.execute("SELECT * FROM repositories ORDER BY id").fetchall():
        if re.sub(r"[^A-Za-z0-9]", "-", repo["local_path"].rstrip("/")) == encoded:
            return repo
    return None


def session_folder_hint(conn: sqlite3.Connection, claude_session_id: str, cwd: str | None) -> str | None:
    """For SessionStart: a session running outside its project's folder is told where
    the project is, so it looks for .claude/decisions/ (and the code) in the right place."""
    row = conn.execute(
        "SELECT repositories.local_path FROM sessions JOIN repositories ON repositories.id = sessions.repository_id "
        "WHERE sessions.claude_session_id = ?", (claude_session_id,),
    ).fetchone()
    if row is None or not cwd or _path_within(cwd, row["local_path"]):
        return None
    return (f"Note from apo: this session belongs to the project at {row['local_path']}, but it is "
            f"running in {cwd} (it was resumed from another folder). Its answers and tasks are in "
            f"{row['local_path']}/.claude/decisions/ — work there with absolute paths, and tell the user that "
            f"/decisions only works when the session runs in that folder (cd {row['local_path']} && claude -r "
            f"{claude_session_id}).")


def _path_within(path: str, folder: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


def _move_session(conn: sqlite3.Connection, claude_session_id: str, cwd: str) -> None:
    """The session is now working in cwd (a cd, a worktree). Same rule as
    register_session: a folder with no project keeps the session's existing one."""
    repository = _repository_for_session_cwd(conn, cwd)
    conn.execute(
        "UPDATE sessions SET cwd = ?, project_id = COALESCE(?, project_id), "
        "repository_id = COALESCE(?, repository_id) WHERE claude_session_id = ?",
        (cwd, repository["project_id"] if repository else None, repository["id"] if repository else None,
         claude_session_id),
    )
    conn.commit()


def register_session(
    conn: sqlite3.Connection, claude_session_id: str, cwd: str | None, transcript_path: str | None = None,
) -> int:
    """SessionStart (every source — startup/resume/clear/compact/fork all want
    last_seen_at/status refreshed, unlike the nudge hook which only cares about a
    genuine fresh start). Re-resolves project/repository from cwd on every call, so a
    repository registered mid-session (e.g. auto-registered by the first decision
    filed from it) self-heals on the next resume with no migration needed.
    current_blocker_id is deliberately left alone by the upsert — a resumed session
    that was genuinely blocked shouldn't lose that fact; the next Stop recomputes it."""
    repository = _repository_for_session_cwd(conn, cwd) or _repository_for_transcript(conn, transcript_path)
    project_id = repository["project_id"] if repository else None
    repository_id = repository["id"] if repository else None
    now = _now()
    prior = conn.execute(
        "SELECT status FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    # A folder that says nothing about the project (~, or no cwd) never erases the
    # project a session already had: after a reboot, sessions get resumed from ~.
    conn.execute(
        """
        INSERT INTO sessions
            (claude_session_id, project_id, repository_id, cwd, status, exec_state, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, 'running', 'starting', ?, ?)
        ON CONFLICT(claude_session_id) DO UPDATE SET
            project_id = COALESCE(excluded.project_id, sessions.project_id),
            repository_id = COALESCE(excluded.repository_id, sessions.repository_id),
            cwd = COALESCE(excluded.cwd, sessions.cwd), status = 'running', exec_state = 'starting',
            waiting_detail = NULL, waiting_since = NULL,
            last_seen_at = excluded.last_seen_at, ended_at = NULL, end_reason = NULL,
            verdict = NULL, verdict_reason = NULL
        """,
        (claude_session_id, project_id, repository_id, cwd, now, now),
    )
    conn.commit()
    if prior is None:
        _session_event(conn, claude_session_id, "session.started", "Session started")
    elif prior["status"] == "ended":
        _session_event(conn, claude_session_id, "session.resumed", "Session resumed")
    return conn.execute(
        "SELECT id FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()["id"]


def _session_exists(conn: sqlite3.Connection, claude_session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone() is not None


def session_heartbeat(
    conn: sqlite3.Connection, claude_session_id: str, cwd: str | None = None, event_at: str | None = None,
) -> None:
    """PostToolUse: one cheap indexed UPDATE — the whole point of it is that it's
    free, piggybacked on a tool call that was happening anyway. A tool call finishing
    means the agent is working and not waiting on anyone; session_stop is what later
    decides blocked-vs-idle once the turn actually ends. Self-heals via
    register_session if the row doesn't exist yet (hooks installed mid-session, or a
    start/heartbeat ordering race)."""
    now = _now()
    guard, guard_params = _turn_guard(event_at or now)
    cur = conn.execute(
        "UPDATE sessions SET status = 'running', exec_state = 'working', waiting_detail = NULL, "
        f"waiting_since = NULL, last_seen_at = ?, active_at = ? WHERE claude_session_id = ? AND {guard}",
        (now, now, claude_session_id, *guard_params),
    )
    if cur.rowcount == 0 and not _session_exists(conn, claude_session_id):
        register_session(conn, claude_session_id, cwd)
        session_heartbeat(conn, claude_session_id, cwd)
    conn.commit()


def session_waiting(
    conn: sqlite3.Connection, claude_session_id: str, kind: str, detail: str | None,
    event_at: str | None = None,
) -> None:
    """Notification/PermissionRequest: the session is stopped mid-turn on the human —
    kind 'permission' (a permission dialog) or 'input' (a question or dialog)."""
    if kind not in ("permission", "input"):
        raise ValueError(f"kind must be 'permission' or 'input', got {kind!r}")
    now = _now()
    guard, guard_params = _turn_guard(event_at or now)
    prior = conn.execute(
        "SELECT exec_state FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    cur = conn.execute(
        "UPDATE sessions SET exec_state = ?, waiting_detail = ?, "
        "waiting_since = COALESCE(waiting_since, ?), last_seen_at = ?, active_at = ? "
        f"WHERE claude_session_id = ? AND status != 'ended' AND {guard}",
        (f"waiting_{kind}", detail, now, now, now, claude_session_id, *guard_params),
    )
    conn.commit()
    if cur.rowcount and prior and prior["exec_state"] != f"waiting_{kind}":
        _session_event(conn, claude_session_id, "session.waiting", _WAITING_PREFIX[kind], detail=detail, importance=2)


_WAITING_PREFIX = {"permission": "Waiting for your permission", "input": "Waiting for your input"}


def session_prompt_submitted(
    conn: sqlite3.Connection, claude_session_id: str, prompt: str | None, cwd: str | None = None,
    event_at: str | None = None,
) -> None:
    """UserPromptSubmit: a new turn starts. Also the freshest "You asked" there is —
    the Stop-time transcript read only lands once the turn is over. Guarded like the
    other async hooks: a very short turn's Stop can land before this does."""
    if not _session_exists(conn, claude_session_id):
        register_session(conn, claude_session_id, cwd)
    now = _now()
    prompt = _clip(prompt)
    guard, guard_params = _turn_guard(event_at or now)
    conn.execute(
        "UPDATE sessions SET status = 'running', exec_state = 'working', waiting_detail = NULL, "
        "waiting_since = NULL, last_error = NULL, last_prompt = COALESCE(?, last_prompt), "
        f"last_seen_at = ?, active_at = ?, reviewed_at = ? WHERE claude_session_id = ? AND {guard}",
        (prompt, now, now, now, claude_session_id, *guard_params),
    )
    conn.commit()


def _end_turn(conn: sqlite3.Connection, claude_session_id: str, exec_state: str, error: str | None) -> None:
    now = _now()
    blocked = _has_open_requests(conn, claude_session_id)
    cur = conn.execute(
        "UPDATE sessions SET status = ?, exec_state = ?, last_error = ?, waiting_detail = NULL, "
        "waiting_since = NULL, last_stop_at = ?, last_seen_at = ?, active_at = ?, pending_prompt = NULL, "
        "prompt_answer = NULL WHERE claude_session_id = ?",
        ("blocked" if blocked else "idle", exec_state, error, now, now, now, claude_session_id),
    )
    conn.commit()
    if not cur.rowcount:
        return
    if exec_state == "errored" and error == USAGE_LIMIT_ERROR:
        _session_event(conn, claude_session_id, "session.usage_limit", "Hit the usage limit — continues when it resets",
                       importance=1)
    elif exec_state == "errored":
        _session_event(conn, claude_session_id, "session.errored", "Stopped on an API error",
                       detail=api_error_reason(error), importance=2)
    else:
        _session_event(conn, claude_session_id, "session.turn_ended", "Turn finished", importance=0)


def session_stop(conn: sqlite3.Connection, claude_session_id: str) -> None:
    """Stop: the agent finished its turn and is about to wait on the user. status
    becomes 'blocked' if any request this session filed is still open, else 'idle'.
    No-op if the session row doesn't exist."""
    _end_turn(conn, claude_session_id, "at_rest", None)


# StopFailure's `error` for the usage limit. Claude Code continues an open session by
# itself when the limit resets ("Continue automatically at usage limit", on by
# default) and says so through Notification(quota_auto_resume_*).
USAGE_LIMIT_ERROR = "rate_limit"
USAGE_LIMIT_STUCK = "rate_limit_no_resume"
CONTINUE_AFTER_LIMIT_PROMPT = ("Your claude.ai usage limit has reset. Continue the task you were working on when "
                               "the limit was reached; do not repeat work that is already complete.")


def session_stop_failure(
    conn: sqlite3.Connection, claude_session_id: str, error: str | None, message: str | None = None,
) -> None:
    """StopFailure: the turn ended on an API error (usage limit, overload, auth,
    billing...) instead of a normal Stop. `message` is Claude Code's own text for it,
    which for the usage limit says when it resets."""
    _end_turn(conn, claude_session_id, "errored", error or "unknown")
    resets = parse_reset_time(message, datetime.now(timezone.utc)) if error == USAGE_LIMIT_ERROR else None
    conn.execute(
        "UPDATE sessions SET last_error_detail = ?, limit_resets_at = ? WHERE claude_session_id = ?",
        (_clip(message), resets.isoformat() if resets else None, claude_session_id),
    )
    conn.commit()


_RESET_UTC = re.compile(r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})\s*UTC", re.I)
_RESET_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\))?", re.I)


def parse_reset_time(text: str | None, now: datetime) -> datetime | None:
    """"resets 4pm (Europe/Berlin)", "resets 4:30pm", "resets at 9am",
    "resets 2026-10-16 00:00 UTC". A bare clock time is its next occurrence."""
    if not text:
        return None
    m = _RESET_UTC.search(text)
    if m:
        return datetime.fromisoformat(f"{m[1]}T{int(m[2]):02d}:{m[3]}:00+00:00")
    m = _RESET_CLOCK.search(text)
    if not m or not (m[2] or m[3]):
        return None
    hour, minute, meridiem = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(m[4]) if m[4] else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        return next_local_time(hour, minute, now)
    local_now = now.astimezone(zone)
    reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return reset if reset > local_now else reset + timedelta(days=1)


def next_local_time(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """The next time the local clock reads hour:minute, with that day's UTC offset
    (which differs from today's when the clocks change in between)."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    wall = now.replace(tzinfo=None, hour=hour, minute=minute, second=0, microsecond=0)
    when = wall.astimezone()
    return when if when > now else (wall + timedelta(days=1)).astimezone()


RESUME_ATTEMPTS = 3
RESUME_RETRY_MINUTES = 15


def schedule_resume(conn: sqlite3.Connection, claude_session_id: str, when: datetime | None) -> None:
    """None cancels."""
    conn.execute(
        "UPDATE sessions SET resume_scheduled_at = ?, resume_attempts = 0 WHERE claude_session_id = ?",
        (when.astimezone(timezone.utc).isoformat() if when else None, claude_session_id),
    )
    conn.commit()
    if when:
        _session_event(conn, claude_session_id, "session.resume_scheduled",
                       f"Will resume at {when.astimezone().strftime('%H:%M')}", importance=1, actor="human",
                       source="dashboard")


def due_resumes(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE resume_scheduled_at IS NOT NULL AND resume_scheduled_at <= ?",
        (now.isoformat(),),
    ).fetchall()]


def resume_finished(conn: sqlite3.Connection, claude_session_id: str, error: str | None = None) -> None:
    """After a scheduled resume ran: done, or retry in a while (still limited), or give up."""
    if error is None:
        conn.execute("UPDATE sessions SET resume_scheduled_at = NULL, resume_attempts = 0, last_error = NULL "
                     "WHERE claude_session_id = ?", (claude_session_id,))
        conn.commit()
        _session_event(conn, claude_session_id, "session.launched", "Resumed after the usage limit",
                       importance=1, actor="system", source="dashboard")
        return
    row = conn.execute("SELECT resume_attempts FROM sessions WHERE claude_session_id = ?", (claude_session_id,)).fetchone()
    attempts = (row["resume_attempts"] if row else 0) + 1
    if attempts >= RESUME_ATTEMPTS:
        conn.execute("UPDATE sessions SET resume_scheduled_at = NULL, resume_attempts = ? WHERE claude_session_id = ?",
                     (attempts, claude_session_id))
        conn.commit()
        _session_event(conn, claude_session_id, "session.errored",
                       f"Couldn't resume after {attempts} tries", detail=error, importance=2, actor="system",
                       source="dashboard")
        return
    retry = datetime.now(timezone.utc) + timedelta(minutes=RESUME_RETRY_MINUTES)
    conn.execute("UPDATE sessions SET resume_scheduled_at = ?, resume_attempts = ? WHERE claude_session_id = ?",
                 (retry.isoformat(), attempts, claude_session_id))
    conn.commit()


def session_quota_notification(conn: sqlite3.Connection, claude_session_id: str, notification_type: str) -> None:
    """Notification(quota_auto_resume_*): fired = the limit reset and Claude Code
    continued the turn; stale/disabled = it won't, so the session is yours again."""
    now = _now()
    if notification_type == "quota_auto_resume_fired":
        cur = conn.execute(
            "UPDATE sessions SET status = 'running', exec_state = 'working', last_error = NULL, last_seen_at = ?, "
            "active_at = ? WHERE claude_session_id = ? AND last_error IN (?, ?)",
            (now, now, claude_session_id, USAGE_LIMIT_ERROR, USAGE_LIMIT_STUCK),
        )
        conn.commit()
        if cur.rowcount:
            _session_event(conn, claude_session_id, "session.limit_reset", "Usage limit reset — continuing",
                           importance=1, actor="system")
    elif notification_type in ("quota_auto_resume_stale", "quota_auto_resume_disabled"):
        cur = conn.execute(
            "UPDATE sessions SET last_error = ? WHERE claude_session_id = ? AND last_error = ?",
            (USAGE_LIMIT_STUCK, claude_session_id, USAGE_LIMIT_ERROR),
        )
        conn.commit()
        if cur.rowcount:
            _session_event(conn, claude_session_id, "session.errored",
                           "Hit the usage limit and won't continue on its own", importance=2, actor="system")


def session_end(conn: sqlite3.Connection, claude_session_id: str, reason: str | None = None) -> None:
    """SessionEnd: docs confirm a shared 1.5s time budget across every SessionEnd
    hook for a session — a couple of indexed statements, no joins. No-op if unknown."""
    now = _now()
    # Judged before the claimed tasks below are released: that is what it left open.
    verdict, verdict_reason = compute_verdict(conn, claude_session_id, reason)
    cur = conn.execute(
        "UPDATE sessions SET status = 'ended', exec_state = 'ended', waiting_detail = NULL, pending_prompt = NULL, "
        "prompt_answer = NULL, ended_at = ?, end_reason = ?, last_seen_at = ?, verdict = ?, verdict_reason = ? "
        "WHERE claude_session_id = ? AND status != 'ended'",
        (now, reason, now, verdict, verdict_reason, claude_session_id),
    )
    # A task claimed by a session that's gone is free for the next one.
    released = [r["id"] for r in conn.execute(
        "SELECT id FROM decision_requests WHERE claimed_by_session_id = ? AND status = 'in_progress'",
        (claude_session_id,))]
    conn.execute(
        "UPDATE decision_requests SET status = 'open', claimed_by_session_id = NULL, claimed_at = NULL "
        "WHERE claimed_by_session_id = ? AND status = 'in_progress'",
        (claude_session_id,),
    )
    conn.commit()
    for task_id in released:
        _record_attempt(conn, task_id, "released", session_id=claude_session_id, actor="system", note=reason)
    if cur.rowcount:
        crashed = reason == "process_gone_mid_turn"
        _session_event(
            conn, claude_session_id, "session.crashed" if crashed else "session.ended",
            "Session crashed" if crashed else "Session ended", importance=2 if crashed else 1,
            actor="system" if reason in ("process_gone", "process_gone_mid_turn") else "agent",
        )


VERDICTS = ("done", "partial", "abandoned", "crashed", "unknown")


def compute_verdict(conn: sqlite3.Connection, claude_session_id: str, end_reason: str | None) -> tuple[str, str]:
    """How a session ended, from what is already recorded (no model is asked): what it
    claimed and left open, its checklist, a question still waiting on the human, and
    whether the work it finished reached the default branch."""
    if end_reason == "process_gone_mid_turn":
        return "crashed", "The process disappeared in the middle of a turn"
    open_reasons, done_reasons = [], []
    claimed = conn.execute(
        "SELECT id, title, status FROM decision_requests WHERE claimed_by_session_id = ? AND kind = 'task'",
        (claude_session_id,),
    ).fetchall()
    left = [t for t in claimed if t["status"] == "in_progress"]
    finished = [t for t in claimed if t["status"] == "resolved"]
    if left:
        open_reasons.append(f"left {_quoted_titles(left)} unfinished")
    if finished:
        done_reasons.append(f"finished {_quoted_titles(finished)}")
    items = conn.execute(
        "SELECT checklist_items.status FROM checklist_items JOIN sessions ON sessions.id = checklist_items.session_id "
        "WHERE sessions.claude_session_id = ?", (claude_session_id,),
    ).fetchall()
    unchecked = sum(1 for i in items if i["status"] != "done")
    if unchecked:
        open_reasons.append(f"{len(items) - unchecked} of {len(items)} checklist steps done")
        if unchecked < len(items):
            done_reasons.append("checked off some steps")
    elif items:
        done_reasons.append(f"all {len(items)} checklist steps done")
    blockers = conn.execute(
        "SELECT COUNT(*) FROM decision_requests WHERE origin_session_id = ? AND status = 'open'",
        (claude_session_id,),
    ).fetchone()[0]
    if blockers:
        open_reasons.append(f"a request for you is still waiting ({blockers} open)" if blockers > 1
                            else "a decision or action it asked for is still waiting on you")
    unmerged = [t for t in finished if _unmerged(conn, t["id"])]
    if open_reasons:
        verdict = "partial" if done_reasons else "abandoned"
        return verdict, _sentence("; ".join(open_reasons + done_reasons))
    if unmerged:
        return "partial", f"Finished {_quoted_titles(unmerged)}, but the work is not merged"
    if done_reasons:
        return "done", _sentence("; ".join(done_reasons))
    return "unknown", "Nothing recorded to tell how it went"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _quoted_titles(tasks) -> str:
    shown = ", ".join(f'"{t["title"]}"' for t in tasks[:2])
    return shown + (f" and {len(tasks) - 2} more" if len(tasks) > 2 else "")


def _unmerged(conn: sqlite3.Connection, item_id: int) -> bool:
    item = get_decision_request(conn, item_id)
    location = current_work_location(conn, item) if item else None
    return bool(location and location.get("merged") is False)


def backfill_verdict(conn: sqlite3.Connection, row: dict) -> None:
    """An ended session recorded before verdicts existed gets one the first time it is
    read. Its claimed tasks were released at the end, so only what remained is judged."""
    row["verdict"], row["verdict_reason"] = compute_verdict(conn, row["claude_session_id"], row["end_reason"])
    conn.execute("UPDATE sessions SET verdict = ?, verdict_reason = ? WHERE claude_session_id = ? AND verdict IS NULL",
                 (row["verdict"], row["verdict_reason"], row["claude_session_id"]))
    conn.commit()


def _link_dispatched_child(conn: sqlite3.Connection, claude_session_id: str, agent_id: str | None = None,
                           task_id: int | None = None) -> None:
    """The session an item was dispatched to learns who dispatched it: matched by the
    task it claimed, or by the short agent id `claude agents` reports for it."""
    if task_id is not None:
        parent = conn.execute("SELECT dispatch_parent_session_id FROM decision_requests WHERE id = ? "
                              "AND dispatched_at IS NOT NULL", (task_id,)).fetchone()
    elif agent_id:
        parent = conn.execute("SELECT dispatch_parent_session_id FROM decision_requests WHERE dispatch_agent_id = ? "
                              "AND dispatch_parent_session_id IS NOT NULL ORDER BY dispatched_at DESC LIMIT 1",
                              (agent_id,)).fetchone()
    else:
        return
    if parent and parent[0] and parent[0] != claude_session_id:
        conn.execute("UPDATE sessions SET parent_session_id = ? WHERE claude_session_id = ? "
                     "AND parent_session_id IS NULL", (parent[0], claude_session_id))
        conn.commit()


def reconcile_live_sessions(conn: sqlite3.Connection, agents: list[dict] | None) -> None:
    """Correct the hook-fed picture against Claude Code's own live view (`claude
    agents --json`, see claude_runtime.list_agents). Hooks miss things — a session
    started before the hooks were installed, a permission prompt approved in the
    terminal (nothing fires until the tool finishes), a process killed without
    SessionEnd — and this is how those stop lying on the dashboard.

    Runs only while the dashboard is being viewed (app.py), costs no LLM tokens,
    and does nothing with agents=None (the CLI wasn't available): no information is
    never treated as "no sessions"."""
    if agents is None:
        return
    now = _now()
    live: dict[str, dict] = {}
    jobs: dict[str, str] = {}
    for agent in agents:
        sid = agent.get("sessionId")
        if sid and agent.get("id") and agent.get("kind") == "background":
            jobs[sid] = agent["id"]
        # Some sessions are listed twice (a job record without a status, and the
        # process with one); the one with a live status is the one that matters.
        if sid and (sid not in live or (agent.get("status") and not live[sid].get("status"))):
            live[sid] = agent

    for sid, agent in live.items():
        row = conn.execute(
            "SELECT status, exec_state, cwd FROM sessions WHERE claude_session_id = ?", (sid,)
        ).fetchone()
        if row is None or row["status"] == "ended":
            register_session(conn, sid, agent.get("cwd"))
            row = conn.execute(
                "SELECT status, exec_state, cwd FROM sessions WHERE claude_session_id = ?", (sid,)
            ).fetchone()
        elif agent.get("cwd") and agent["cwd"] != row["cwd"]:
            _move_session(conn, sid, agent["cwd"])
        exec_state = row["exec_state"] or _LEGACY_EXEC_STATE.get(row["status"])
        live_status = agent.get("status")
        waiting_for = agent.get("waitingFor") or ""
        if live_status == "busy" and exec_state != "working":
            conn.execute(
                "UPDATE sessions SET status = 'running', exec_state = 'working', waiting_detail = NULL, "
                "waiting_since = NULL WHERE claude_session_id = ?",
                (sid,),
            )
        elif live_status == "waiting":
            kind = "permission" if "permission" in waiting_for.lower() else "input"
            if exec_state != f"waiting_{kind}":
                conn.execute(
                    "UPDATE sessions SET exec_state = ?, waiting_detail = ?, "
                    "waiting_since = COALESCE(waiting_since, ?) WHERE claude_session_id = ?",
                    (f"waiting_{kind}", waiting_for or None, now, sid),
                )
                conn.commit()
                _session_event(conn, sid, "session.waiting", _WAITING_PREFIX[kind], detail=waiting_for or None,
                               importance=2, actor="system", source="reconcile")
        elif live_status == "idle" and exec_state in (*_MID_TURN_STATES, "starting"):
            conn.execute(
                "UPDATE sessions SET status = ?, exec_state = 'at_rest', waiting_detail = NULL, "
                "waiting_since = NULL WHERE claude_session_id = ?",
                ("blocked" if _has_open_requests(conn, sid) else "idle", sid),
            )
        conn.execute(
            "UPDATE sessions SET pid = ?, live_checked_at = ?, agent_id = COALESCE(?, agent_id), "
            "agent_kind = COALESCE(?, agent_kind) WHERE claude_session_id = ?",
            (agent.get("pid"), now, jobs.get(sid), "background" if sid in jobs else agent.get("kind"), sid),
        )
        if sid in jobs:
            _link_dispatched_child(conn, sid, agent_id=jobs[sid])
    conn.commit()

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=GONE_GRACE_MINUTES)).isoformat()
    for row in conn.execute(
        "SELECT claude_session_id, status, exec_state FROM sessions WHERE status != 'ended' AND last_seen_at < ?",
        (cutoff,),
    ).fetchall():
        if row["claude_session_id"] in live:
            continue
        exec_state = row["exec_state"] or _LEGACY_EXEC_STATE.get(row["status"])
        reason = "process_gone_mid_turn" if exec_state == "working" else "process_gone"
        session_end(conn, row["claude_session_id"], reason)


SUMMARY_MAX_CHARS = 4000


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS] + "…"


def _clean_recap(text: str) -> str:
    """The fleet card clamps this to a couple of lines as a quick memory-jog, but
    Claude's actual final message is often a full markdown-formatted, multi-
    paragraph wrap-up — displayed verbatim, fenced code blocks and blank lines
    between paragraphs each eat a whole line of that budget on pure noise, and
    bold/inline-code markers clutter whatever's left. This isn't a summarizer
    (no LLM call, same zero-cost principle as the rest of session tracking) —
    just stripping decoration and collapsing it to one flowing line of prose."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"__([^_]*)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return plain_text(re.sub(r"\s+", " ", text).strip())


def plain_text(text: str | None) -> str | None:
    """Markdown tables flattened to prose: separator rows go, cells join with " · "."""
    if not text or "|" not in text:
        return text
    text = re.sub(r"\|\s*:?-{3,}:?\s*(?=\|)", "|", text)
    text = re.sub(r"(?:\s*\|\s*)+", " · ", text)
    text = re.sub(r"([:;,.!?])\s*·\s*", r"\1 ", text)
    return text.strip(" ·")


def _readable_error(error: str | None) -> str | None:
    if error == USAGE_LIMIT_ERROR:
        return "Hit the usage limit — continues when it resets"
    if error == USAGE_LIMIT_STUCK:
        return "Hit the usage limit and won't continue on its own"
    return api_error_reason(error)


def api_error_reason(error: str | None) -> str | None:
    """Claude Code reports "unknown" when it has no error type; that's not a reason."""
    return None if not error or error.strip().lower() == "unknown" else error


def _tidy_summary(summary: str) -> str:
    return re.sub(r":\s*unknown$", "", summary or "")


def collapse_repeats(events: list[dict]) -> list[dict]:
    """The same thing happening again (an agent retrying into the same API error)
    is one line with a count, not a screenful. Keeps the first (newest) position."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for event in events:
        event = {**event, "summary": _tidy_summary(event["summary"]), "repeat": 1}
        key = (event["verb"], event.get("project_id"), event.get("session_id"), event["summary"])
        if key in seen:
            seen[key]["repeat"] += 1
            continue
        seen[key] = event
        out.append(event)
    return out


def record_session_summary(
    conn: sqlite3.Connection, claude_session_id: str, summary: str | None,
    title: str | None = None, last_prompt: str | None = None, at: str | None = None,
    context_tokens: int | None = None,
) -> None:
    """Stop: the "what was this session last doing" memory-refresher — the agent's
    final message of the turn, the prompt that led to it, and Claude Code's own
    ai-title. All free (from the hook payload/transcript, no LLM call). A None field
    keeps whatever was there, so a turn with no readable text never blanks a card."""
    cleaned_summary = _clip(_clean_recap(summary)) if summary else None
    conn.execute(
        """
        UPDATE sessions SET
            last_summary = COALESCE(?, last_summary),
            last_summary_at = CASE WHEN ? IS NULL THEN last_summary_at ELSE ? END,
            title = COALESCE(?, title),
            last_prompt = COALESCE(?, last_prompt),
            context_tokens = COALESCE(?, context_tokens)
        WHERE claude_session_id = ?
        """,
        (cleaned_summary, summary, at or _now(), _clip(title), _clip(last_prompt), context_tokens, claude_session_id),
    )
    conn.commit()


def list_sessions(
    conn: sqlite3.Connection, include_ended: bool = False, ended_since: str | None = None,
) -> list[dict]:
    """Enriched rows for the fleet view / /api/sessions, derived here — the one place
    — so nothing else duplicates the logic:
      display_status  one headline: crashed/ended, errored, waiting_permission,
                      waiting_input, blocked_decision/blocked_action, running, idle
      needs_you       whether that headline is waiting on the human
      stale           a working session with no hook activity for STALE_MINUTES
                      (quiet_minutes says how long); never a session at rest
      ghost           registered but never took a turn, GHOST_MINUTES on"""
    # current_blocker_id here is derived: the oldest still-open request the session
    # filed (it's been waiting longest), with open_request_count saying how many more.
    query = """
        WITH open_requests AS (
            SELECT origin_session_id, MIN(id) AS first_id, COUNT(*) AS n
            FROM decision_requests
            WHERE status = 'open' AND origin_session_id IS NOT NULL
            GROUP BY origin_session_id
        )
        SELECT sessions.*, projects.name AS project_name, repositories.name AS repository_name,
               open_requests.first_id AS derived_blocker_id,
               COALESCE(open_requests.n, 0) AS open_request_count,
               decision_requests.title AS blocker_title, decision_requests.kind AS blocker_kind,
               decision_requests.linear_push_error AS blocker_linear_push_error
        FROM sessions
        LEFT JOIN projects ON projects.id = sessions.project_id
        LEFT JOIN repositories ON repositories.id = sessions.repository_id
        LEFT JOIN open_requests ON open_requests.origin_session_id = sessions.claude_session_id
        LEFT JOIN decision_requests ON decision_requests.id = open_requests.first_id
    """
    params: list = []
    if ended_since is not None:
        query += " WHERE sessions.status != 'ended' OR sessions.last_seen_at >= ?"
        params.append(ended_since)
    elif not include_ended:
        query += " WHERE sessions.status != 'ended'"
    query += " ORDER BY sessions.last_seen_at DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    now = datetime.now(timezone.utc)
    for row in rows:
        # sessions.* carries the vestigial stored column under the same name.
        row["current_blocker_id"] = row.pop("derived_blocker_id")
        row["prompt"] = _loads(row.get("pending_prompt")) or None
        exec_state = row["exec_state"] or _LEGACY_EXEC_STATE.get(row["status"])
        row["exec_state"] = exec_state
        row["display_status"] = _display_status(row, exec_state)
        row["needs_you"] = row["display_status"] in _NEEDS_YOU
        row["last_summary"] = plain_text(row["last_summary"])
        row["error_code"] = row["last_error"]
        row["last_error"] = _readable_error(row["last_error"])
        quiet = int((now - datetime.fromisoformat(row["last_seen_at"])).total_seconds() // 60)
        row["stale"] = exec_state == "working" and quiet >= STALE_MINUTES
        row["quiet_minutes"] = quiet if row["stale"] else None
        # Never active per the hooks AND never confirmed alive by reconcile: a session
        # opened before the hooks were installed has no hook activity but is real.
        row["ghost"] = (
            row["status"] != "ended" and not row["needs_you"]
            and row["active_at"] is None and row["live_checked_at"] is None
            and (now - datetime.fromisoformat(row["started_at"])).total_seconds() > GHOST_MINUTES * 60
        )
        if row["status"] == "ended" and row["verdict"] is None:
            backfill_verdict(conn, row)
    _attach_family(conn, rows)
    return rows


def _attach_family(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """parent (who dispatched it) and children (what it dispatched, with a roll-up like
    "3 running, 1 failed") on each row. Children are read from all sessions, not just
    the rows listed, so an ended one still counts."""
    links = conn.execute(
        "SELECT id, claude_session_id, title, status, end_reason, verdict, last_error, parent_session_id "
        "FROM sessions WHERE parent_session_id IS NOT NULL ORDER BY started_at, id"
    ).fetchall()
    children: dict[str, list[dict]] = {}
    for link in links:
        if link["status"] != "ended":
            state = "errored" if link["last_error"] else "running"
        else:
            state = "failed" if link["end_reason"] == "process_gone_mid_turn" or link["verdict"] in (
                "crashed", "abandoned") else "finished"
        children.setdefault(link["parent_session_id"], []).append({
            "id": link["id"], "claude_session_id": link["claude_session_id"], "title": link["title"], "state": state})
    by_sid = {r["claude_session_id"]: r for r in conn.execute(
        "SELECT id, claude_session_id, title FROM sessions WHERE claude_session_id IN "
        "(SELECT parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL)")}
    for row in rows:
        mine = children.get(row["claude_session_id"], [])
        counts = {state: sum(1 for c in mine if c["state"] == state) for state in ("running", "errored", "failed", "finished")}
        counts["failed"] += counts.pop("errored")
        row["children"] = mine
        row["children_summary"] = ", ".join(
            f"{counts[state]} {state}" for state in ("running", "failed", "finished") if counts[state]) or None
        parent = by_sid.get(row.get("parent_session_id"))
        row["parent"] = ({"id": parent["id"], "claude_session_id": parent["claude_session_id"],
                          "title": parent["title"]} if parent else None)


_NEEDS_YOU = {"errored", "waiting_permission", "waiting_input", "blocked_decision", "blocked_action"}


# --- Prompts answered from the dashboard ---
#
# A background session's permission request, question or plan approval: its hook
# (cli._permission / cli._pretool) posts the prompt here and waits; the dashboard
# writes an answer; the hook returns it to Claude Code as if it came from its own
# dialog. Holding is shorter than the hooks' timeout (installer.SESSION_HOOKS), so
# the hook always gets to clean up and fall back to that dialog.
PROMPT_HOLD_SECONDS = 3600


def hold_prompt(conn: sqlite3.Connection, claude_session_id: str, prompt: dict) -> str | None:
    """None while another prompt of this session is still held: parallel tool calls
    ask in turn, since replacing the first would send it back to the terminal."""
    prompt_id = uuid.uuid4().hex
    cur = conn.execute("UPDATE sessions SET pending_prompt = ?, prompt_answer = NULL "
                       "WHERE claude_session_id = ? AND pending_prompt IS NULL",
                       (json.dumps({**prompt, "id": prompt_id, "asked_at": _now()}), claude_session_id))
    conn.commit()
    return prompt_id if cur.rowcount else None


def _held(conn: sqlite3.Connection, claude_session_id: str, prompt_id: str):
    row = conn.execute("SELECT pending_prompt, prompt_answer FROM sessions WHERE claude_session_id = ?",
                       (claude_session_id,)).fetchone()
    if row is None or (_loads(row["pending_prompt"]) or {}).get("id") != prompt_id:
        return None
    return row


def held_answer(conn: sqlite3.Connection, claude_session_id: str, prompt_id: str) -> dict | None:
    """None while it waits. A prompt that's gone (the turn ended, or another prompt
    replaced it) reads as "answer it in the terminal"."""
    row = _held(conn, claude_session_id, prompt_id)
    if row is None:
        return {"behavior": "terminal"}
    return _loads(row["prompt_answer"]) or None


def answer_prompt(conn: sqlite3.Connection, claude_session_id: str, prompt_id: str, answer: dict) -> bool:
    """False for a prompt that has already been answered or is no longer waiting."""
    row = _held(conn, claude_session_id, prompt_id)
    if row is None or row["prompt_answer"]:
        return False
    cur = conn.execute("UPDATE sessions SET prompt_answer = ? WHERE claude_session_id = ? AND pending_prompt = ? "
                       "AND prompt_answer IS NULL", (json.dumps(answer), claude_session_id, row["pending_prompt"]))
    conn.commit()
    if cur.rowcount and answer.get("behavior") != "terminal":
        _session_event(conn, claude_session_id, "session.prompt_answered", "Answered from the dashboard",
                       detail=_loads(row["pending_prompt"]).get("detail"), importance=1, actor="human",
                       source="dashboard")
    return bool(cur.rowcount)


def end_prompt(conn: sqlite3.Connection, claude_session_id: str, prompt_id: str) -> None:
    row = _held(conn, claude_session_id, prompt_id)
    if row is not None:
        conn.execute("UPDATE sessions SET pending_prompt = NULL, prompt_answer = NULL WHERE claude_session_id = ? "
                     "AND pending_prompt = ?", (claude_session_id, row["pending_prompt"]))
        conn.commit()


# Finished its turn, so a message can go to it.
CONTINUABLE = ("idle", "ended", "crashed", "errored", "blocked_decision", "blocked_action")
# Past this, every turn of a continued session re-reads a lot; a fresh one starts small.
FRESH_AFTER_TOKENS = 100_000
# The model's prompt cache lasts about this long by default. After it, a resumed
# session's first turn re-reads everything at full price.
CACHE_WARM_MINUTES = 5


# What a resumed session is told when you resume it without saying anything.
RESUME_PROMPT = "Pick up where you left off. If the work was already finished, say so in a sentence and stop."


def continue_plan(session: dict, now: datetime | None = None) -> dict:
    """Whether the dashboard can reply to this session, and which way it recommends:
    resume it (it keeps everything, and re-reads everything) or start fresh from a
    handoff."""
    plan = {"allowed": True, "why_not": None, "recommend": "resume", "reason": None}
    if session["status"] != "ended" and session.get("agent_kind") != "background":
        return {**plan, "allowed": False, "why_not": (
            "It's open in a terminal or an editor, so reply there: continuing it from here as well would mix two "
            "conversations into one.")}
    if session["display_status"] not in CONTINUABLE:
        return {**plan, "allowed": False, "why_not": "It's in the middle of a turn. Reply once it has finished."}
    tokens = session.get("context_tokens") or 0
    last = session.get("last_stop_at") or session.get("last_seen_at")
    idle = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(last)).total_seconds() / 60 if last else None
    if tokens >= FRESH_AFTER_TOKENS:
        plan.update(recommend="fresh", reason=f"It carries {tokens // 1000}k tokens, and continuing it re-reads all of "
                                              "them on every turn.")
    elif idle is not None and idle > CACHE_WARM_MINUTES:
        plan.update(recommend="fresh", reason=f"It has been idle {int(idle)} minutes, so its cache has likely expired "
                                              "and continuing would re-read everything at full price.")
    else:
        plan["reason"] = "It's small and recent, so continuing is cheap and keeps everything it knows."
    return plan


def handoff(session: dict, message: str, transcript: str | None) -> str:
    """What a fresh session gets in place of the old one's whole conversation."""
    lines = [message.strip(), "",
             f"This picks up from an earlier session ({session.get('title') or 'untitled'}, "
             f"id {session['claude_session_id']})."]
    if session.get("last_prompt"):
        lines.append(f"It was asked: {session['last_prompt']}")
    if session.get("last_summary"):
        lines.append(f"Where it got to: {session['last_summary']}")
    if transcript:
        lines.append(f"Its full transcript, if a detail from it is needed: {transcript}")
    return "\n".join(lines)


def _display_status(row: dict, exec_state: str | None) -> str:
    if row["status"] == "ended":
        return "crashed" if row["end_reason"] == "process_gone_mid_turn" else "ended"
    if exec_state == "errored" and row["last_error"] == USAGE_LIMIT_ERROR:
        return "usage_limit"
    if exec_state in ("errored", "waiting_permission", "waiting_input"):
        return exec_state
    if row["status"] == "blocked" and row["blocker_kind"]:
        return f"blocked_{row['blocker_kind']}"
    return "running" if exec_state == "working" else "idle"


# --- Checklists ---
#
# Fed by two write paths sharing the same rows, mirroring how a blocker gets
# answered either via the dashboard or via Linear: Claude's native TaskCreated/
# TaskCompleted hooks (completed_by='agent') and manual dashboard edits
# (completed_by='human'). Session tracking's zero-LLM-cost principle applies here
# identically — these are plain hook-triggered writes, nothing polls anything.


def record_checklist_item(
    conn: sqlite3.Connection, claude_session_id: str, claude_task_id: str,
    title: str, description: str | None = None,
) -> int | None:
    """TaskCreated, or PostToolUse(TaskCreate) — both can report the same task, so
    this is idempotent per (session, task id): a repeat updates the title instead of
    inserting a duplicate. No-op (returns None) if the session isn't registered —
    this shouldn't normally happen since SessionStart always fires before any task
    activity, but the hook calling this is fail-open regardless."""
    session = conn.execute(
        "SELECT id FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    if session is None:
        return None
    existing = conn.execute(
        "SELECT id FROM checklist_items WHERE session_id = ? AND claude_task_id = ?",
        (session["id"], claude_task_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE checklist_items SET title = ?, description = COALESCE(?, description) WHERE id = ?",
            (title, description, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO checklist_items (session_id, claude_task_id, title, description, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (session["id"], claude_task_id, title, description, _now()),
    )
    conn.commit()
    return cur.lastrowid


def complete_checklist_item(
    conn: sqlite3.Connection, claude_session_id: str, claude_task_id: str,
    completion_notes: str | None = None,
) -> bool:
    """TaskCompleted. False if no matching pending item is found for this session."""
    cur = conn.execute(
        """
        UPDATE checklist_items
        SET status = 'done', completed_by = 'agent', completion_notes = ?, completed_at = ?
        WHERE claude_task_id = ? AND session_id = (
            SELECT id FROM sessions WHERE claude_session_id = ?
        )
        """,
        (completion_notes, _now(), claude_task_id, claude_session_id),
    )
    conn.commit()
    return cur.rowcount == 1


def set_checklist_item_status(
    conn: sqlite3.Connection, claude_session_id: str, claude_task_id: str,
    status: str | None, title: str | None = None,
) -> bool:
    """PostToolUse(TaskUpdate). Claude Code's status vocabulary is pending/
    in_progress/completed/deleted; 'completed' maps to this table's 'done' and
    'deleted' removes the row. False if the session or task isn't known."""
    session = conn.execute(
        "SELECT id FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    if session is None:
        return False
    where, params = "session_id = ? AND claude_task_id = ?", (session["id"], claude_task_id)
    if status == "deleted":
        cur = conn.execute(f"DELETE FROM checklist_items WHERE {where}", params)
    elif status == "completed":
        cur = conn.execute(
            "UPDATE checklist_items SET completed_at = CASE WHEN status = 'done' THEN completed_at ELSE ? END, "
            "status = 'done', completed_by = COALESCE(completed_by, 'agent'), "
            f"title = COALESCE(?, title) WHERE {where}",
            (_now(), title, *params),
        )
    elif status in ("pending", "in_progress"):
        cur = conn.execute(
            "UPDATE checklist_items SET status = ?, completed_by = NULL, completed_at = NULL, "
            f"title = COALESCE(?, title) WHERE {where}",
            (status, title, *params),
        )
    elif title:
        cur = conn.execute(f"UPDATE checklist_items SET title = ? WHERE {where}", (title, *params))
    else:
        return False
    conn.commit()
    return cur.rowcount == 1


_TODO_PREFIX = "todo:"


def sync_todo_list(conn: sqlite3.Connection, claude_session_id: str, todos: list[dict]) -> None:
    """PostToolUse(TodoWrite), for Claude Code builds that use it instead of the Task
    tools. TodoWrite sends the whole list every call, so this mirrors it: todos keyed
    by their text, ones dropped from the list removed. Manual dashboard items and
    TaskCreate items have no 'todo:' key and are never touched."""
    session = conn.execute(
        "SELECT id FROM sessions WHERE claude_session_id = ?", (claude_session_id,)
    ).fetchone()
    if session is None:
        return
    keep = []
    for todo in todos:
        content = todo.get("content") if isinstance(todo, dict) else None
        if not content:
            continue
        key = _TODO_PREFIX + content
        keep.append(key)
        record_checklist_item(conn, claude_session_id, key, content)
        set_checklist_item_status(conn, claude_session_id, key, todo.get("status") or "pending")
    placeholders = ",".join("?" * len(keep))
    conn.execute(
        f"DELETE FROM checklist_items WHERE session_id = ? AND claude_task_id LIKE '{_TODO_PREFIX}%'"
        + (f" AND claude_task_id NOT IN ({placeholders})" if keep else ""),
        (session["id"], *keep),
    )
    conn.commit()


def add_checklist_item(
    conn: sqlite3.Connection, session_id: int, title: str, description: str | None = None,
) -> int:
    """Manual add from the dashboard — takes the internal sessions.id, not the
    claude_session_id the hook-facing functions above use, since the dashboard
    already has the internal id from the session it's showing."""
    cur = conn.execute(
        """
        INSERT INTO checklist_items (session_id, title, description, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (session_id, title, description, _now()),
    )
    conn.commit()
    return cur.lastrowid


def complete_checklist_item_by_id(
    conn: sqlite3.Connection, item_id: int, completion_notes: str | None = None,
) -> bool:
    """Manual check-off from the dashboard."""
    cur = conn.execute(
        "UPDATE checklist_items SET status = 'done', completed_by = 'human', "
        "completion_notes = ?, completed_at = ? WHERE id = ?",
        (completion_notes, _now(), item_id),
    )
    conn.commit()
    return cur.rowcount == 1


def list_checklist_items(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM checklist_items WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
    ).fetchall()


def list_checklist_items_for_sessions(
    conn: sqlite3.Connection, session_ids: list[int],
) -> dict[int, list[sqlite3.Row]]:
    """One query for a whole page of session cards instead of one per card."""
    if not session_ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM checklist_items WHERE session_id IN ({','.join('?' * len(session_ids))}) "
        "ORDER BY created_at ASC, id ASC",
        list(session_ids),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {sid: [] for sid in session_ids}
    for row in rows:
        grouped[row["session_id"]].append(row)
    return grouped


# --- Attachments ---
#
# Screenshots attached to a decision's answer. The on-disk name is always a fresh
# uuid4 — filename is only ever a display label, never trusted for path construction
# — so a malicious original filename can't path-traverse out of its decision's
# attachments directory.

ATTACHMENT_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
ATTACHMENT_MAX_PER_DECISION = 6


def _sniff_image_type(data: bytes) -> str | None:
    """Identify an image by its magic bytes rather than trusting the client-supplied
    Content-Type header — otherwise a non-image file can be stored (and later served
    back, and copied into a target repo) under a false image type just because the
    uploader claimed one."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_attachment(content_type: str, data: bytes, filename: str = "That file") -> None:
    """Side-effect-free checks shared by save_attachment and the upload route's
    pre-validation pass (the route validates every file in a multi-file request
    before persisting any of them, so one bad file can't leave earlier files in the
    same request orphaned on disk/DB — see app.py's upload_attachments)."""
    if content_type not in ATTACHMENT_CONTENT_TYPES:
        raise ValueError(f"Unsupported attachment content type: {content_type!r}")
    if _sniff_image_type(data) != content_type:
        raise ValueError(f"{filename} doesn't look like a valid {content_type} file")
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise ValueError(f"{filename} exceeds the {ATTACHMENT_MAX_BYTES} byte limit")


def save_attachment(
    conn: sqlite3.Connection, decision_id: int, filename: str, content_type: str, data: bytes
) -> dict:
    validate_attachment(content_type, data, filename)
    if len(list_attachments(conn, decision_id)) >= ATTACHMENT_MAX_PER_DECISION:
        raise ValueError(f"Decision {decision_id} already has {ATTACHMENT_MAX_PER_DECISION} attachments")

    stored_name = uuid.uuid4().hex + ATTACHMENT_CONTENT_TYPES[content_type]
    directory = config.get_attachments_dir() / str(decision_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / stored_name).write_bytes(data)

    cur = conn.execute(
        """
        INSERT INTO attachments (decision_id, filename, stored_name, content_type, size_bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (decision_id, filename, stored_name, content_type, len(data), _now()),
    )
    conn.commit()
    return dict(
        conn.execute("SELECT * FROM attachments WHERE id = ?", (cur.lastrowid,)).fetchone()
    )


def validate_attachments(uploads: list[tuple[str, str, bytes]], existing: int = 0) -> None:
    if existing + len(uploads) > ATTACHMENT_MAX_PER_DECISION:
        raise ValueError(f"That's more than {ATTACHMENT_MAX_PER_DECISION} screenshots.")
    for filename, content_type, data in uploads:
        validate_attachment(content_type, data, filename or "That file")


def add_attachments(conn: sqlite3.Connection, decision_id: int, uploads: list[tuple[str, str, bytes]]) -> list[dict]:
    """Every file is checked before any is saved. The item's file in the repo is then
    rewritten, since a task has no answer step to do it before an agent reads it."""
    validate_attachments(uploads, len(list_attachments(conn, decision_id)))
    saved = [save_attachment(conn, decision_id, filename, content_type, data)
             for filename, content_type, data in uploads]
    _sync_repo_files(conn, decision_id)
    return saved


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def list_attachments(conn: sqlite3.Connection, decision_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM attachments WHERE decision_id = ? ORDER BY id ASC", (decision_id,)
        ).fetchall()
    ]


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> bool:
    row = get_attachment(conn, attachment_id)
    if row is None:
        return False
    path = config.get_attachments_dir() / str(row["decision_id"]) / row["stored_name"]
    path.unlink(missing_ok=True)
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    return True
