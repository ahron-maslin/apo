"""Answer-source adapters.

Each external channel that can receive a pushed-out decision request and/or report an
answer back implements AnswerSource. db.py's create_decision_request/
answer_decision_request only ever talk to the AnswerSource interface and the SOURCES
registry below — adding a new channel means writing a new adapter class and appending
an instance to SOURCES, not editing db.py or app.py.
"""
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from apo import config
from apo import credentials
from apo import db
from apo.integrations import linear_client


# mark_resolved's echo of a non-Linear answer starts with this, which is how the poll
# tells it apart from a human replying after the fact.
ANSWER_COMMENT_PREFIX = "**Answered via "

# Errors not tied to any one pending change — a later successful call to Linear is
# proof enough they're over. linear_push/linear_resolve are only resolved when that
# decision's own push/resolve lands; linear_post_answer_comment isn't a failure at all.
_TRANSIENT_ERROR_SOURCES = ("linear_poll", "linear_retry", "linear_push_retry")

# Wait after the 1st, 2nd, ... failure before the next attempt — spread out so the
# config.sync_retry_max_attempts() attempts outlast a typical outage (~2.5h total)
# instead of all landing inside the first couple of minutes of one.
_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))


@dataclass
class NewAnswer:
    decision_id: int
    answer_text: str


class AnswerSource:
    name: str = "base"

    def is_configured(self) -> bool:
        raise NotImplementedError

    def push_question(self, conn: sqlite3.Connection, decision_id: int, project_name: str, decision: dict) -> None:
        """Push a newly-created decision request out to the external system,
        persisting whatever reference/error info this source needs onto the row."""
        raise NotImplementedError

    def retry_pending(self) -> None:
        """Re-attempt any push or resolve that previously failed — the "queue" is just
        the DB rows carrying a recorded error, so a source that was down catches up
        automatically next time this runs (the poll loop, every APO_POLL_SECONDS) rather
        than staying stuck until someone notices and retries by hand. Default no-op;
        only sources with a push/resolve step need this.

        This is the only retrier, deliberately: one loop in one process means a
        pending push can never be re-sent twice concurrently (a duplicate issue).

        Runs inside asyncio.to_thread like poll_new_answers, so it must open its own
        DB connection rather than share one created on the event-loop thread —
        sqlite3 connections can't cross threads."""
        pass

    def push_edit(self, conn: sqlite3.Connection, decision_id: int, text: bool = False, state: str | None = None) -> None:
        """Carry an edit (new title/description) and/or a reopen (state) to the pushed
        copy. No-op by default."""
        pass

    def poll_new_answers(self) -> list[NewAnswer]:
        """Check for new answers on decisions this source has pushed out. Marks
        anything returned as synced so it isn't returned again.

        Runs inside asyncio.to_thread, so it must open its own DB connection rather
        than share one created on the event-loop thread — sqlite3 connections can't
        cross threads."""
        raise NotImplementedError

    def mark_resolved(self, conn: sqlite3.Connection, decision_id: int) -> None:
        """Called once, right after a decision's answer is first recorded, regardless
        of which source produced the answer. No-op by default — only sources that
        track their own resolved/open state need this."""
        pass

    def sync_goals(self) -> None:
        """Bring in goals planned in this channel and keep their status in step with it.
        Default no-op. Runs inside asyncio.to_thread like poll_new_answers."""
        pass


class LinearSource(AnswerSource):
    name = "linear"

    def is_configured(self) -> bool:
        if config.offline():
            return False
        conn = db.get_connection()
        return bool(credentials.get_credentials(conn, "linear")) or bool(config.linear_api_key())

    def push_question(self, conn: sqlite3.Connection, decision_id: int, project_name: str, decision: dict) -> None:
        started = _now()
        try:
            issue = linear_client.create_issue(project_name, decision)
            conn.execute(
                "UPDATE decision_requests SET linear_issue_id = ?, linear_identifier = ?, linear_url = ?, "
                "linear_push_error = NULL WHERE id = ?",
                (issue["id"], issue["identifier"], issue["url"], decision_id),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[sources.linear] push failed (decision still saved locally): {exc}", file=sys.stderr)
            _record_failure(conn, decision_id, "linear_push_error", "linear_push", exc)
            return
        _clear_retry_state(conn, decision_id)
        db.resolve_sync_errors(conn, ["linear_push"], before=started, decision_ids=[decision_id])
        db.resolve_sync_errors(conn, _TRANSIENT_ERROR_SOURCES, before=started)

    def retry_pending(self) -> None:
        conn = db.get_connection()
        due = (
            "linear_retry_attempts < ? AND (linear_next_retry_at IS NULL OR linear_next_retry_at <= ?)"
        )
        due_params = (config.sync_retry_max_attempts(), _now())
        rows = conn.execute(
            "SELECT decision_requests.id AS id, projects.name AS project_name "
            "FROM decision_requests JOIN projects ON projects.id = decision_requests.project_id "
            f"WHERE linear_push_error IS NOT NULL AND linear_issue_id IS NULL AND {due}",
            due_params,
        ).fetchall()
        unresolved = conn.execute(
            "SELECT id FROM decision_requests "
            f"WHERE linear_resolve_error IS NOT NULL AND linear_issue_id IS NOT NULL AND {due}",
            due_params,
        ).fetchall()
        for row in rows:
            decision = db.get_decision_request(conn, row["id"])
            self.push_question(conn, row["id"], row["project_name"], decision)
            # Answered while the push was failing: mark_resolved skipped it then for
            # lack of an issue, so the freshly created issue still needs closing.
            if decision["answer_text"] is not None:
                self.mark_resolved(conn, row["id"])

        for row in unresolved:
            self.mark_resolved(conn, row["id"], retrying=True)

        edits = conn.execute(
            "SELECT id FROM decision_requests "
            f"WHERE linear_edit_error IS NOT NULL AND linear_issue_id IS NOT NULL AND {due}",
            due_params,
        ).fetchall()
        for row in edits:
            self.push_edit(conn, row["id"])

    def poll_new_answers(self) -> list[NewAnswer]:
        # Not just still-open decisions — a human can reply again *after* a decision
        # was already answered (e.g. to correct a bad first answer), and that reply
        # must not be silently dropped just because answer_text is already set. But
        # only for a while after it's closed: polling every issue ever filed, every
        # cycle, grows the API traffic with all-time history.
        conn = db.get_connection()
        followup_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=config.linear_followup_days())
        ).isoformat()
        rows = conn.execute(
            "SELECT * FROM decision_requests WHERE linear_issue_id IS NOT NULL AND "
            "(status != 'resolved' OR COALESCE(resolved_at, answered_at, created_at) >= ?)",
            (followup_cutoff,),
        ).fetchall()
        results: list[NewAnswer] = []
        first_success: str | None = None
        for row in rows:
            started = _now()
            try:
                comments = linear_client.get_comments(row["linear_issue_id"])
            except Exception as exc:  # noqa: BLE001
                db.record_sync_error(conn, "linear_poll", row["id"], str(exc))
                continue
            first_success = first_success or started
            if not comments:
                continue

            synced = row["linear_synced_comment_id"]
            if synced is None:
                new_comments = comments
            else:
                idx = next((i for i, c in enumerate(comments) if c["id"] == synced), None)
                new_comments = comments[idx + 1:] if idx is not None else comments
            if not new_comments:
                continue

            conn.execute(
                "UPDATE decision_requests SET linear_synced_comment_id = ? WHERE id = ?",
                (new_comments[-1]["id"], row["id"]),
            )
            conn.commit()
            human_comments = [c for c in new_comments if not _is_our_answer_comment(c)]
            if not human_comments:
                continue

            answer_text = "\n\n---\n\n".join(
                f"**{(c.get('user') or {}).get('name', 'Unknown')}:** {c['body']}"
                for c in human_comments
            )
            if row["answer_text"] is None:
                results.append(NewAnswer(decision_id=row["id"], answer_text=answer_text))
            else:
                db.record_sync_error(
                    conn, "linear_post_answer_comment", row["id"],
                    f"New comment(s) on {row['linear_identifier']} after decision #{row['id']} "
                    f"was already answered — not auto-applied, review and file a follow-up "
                    f"decision if it changes anything:\n\n{answer_text}",
                )
        if first_success:
            db.resolve_sync_errors(conn, _TRANSIENT_ERROR_SOURCES, before=first_success)
        return results

    def sync_goals(self) -> None:
        # Imported here, not at the top: hook processes import this module whenever
        # they file a decision, and a module the live install doesn't map yet must
        # not be able to break filing.
        from apo.integrations import linear_goals
        linear_goals.sync()

    def push_edit(self, conn: sqlite3.Connection, decision_id: int, text: bool = False, state: str | None = None) -> None:
        row = conn.execute(
            "SELECT linear_issue_id, linear_edit_pending FROM decision_requests WHERE id = ?", (decision_id,)
        ).fetchone()
        # Without an issue yet, the pending create sends the current text anyway.
        if not row or not row["linear_issue_id"]:
            return
        pending = json.loads(row["linear_edit_pending"] or "{}")
        text = text or bool(pending.get("text"))
        state = state or pending.get("state")
        conn.execute("UPDATE decision_requests SET linear_edit_pending = ? WHERE id = ?",
                     (json.dumps({"text": text, "state": state}), decision_id))
        conn.commit()
        started = _now()
        try:
            if text:
                linear_client.update_issue(row["linear_issue_id"], db.get_decision_request(conn, decision_id))
            if state:
                linear_client.move_to_state(row["linear_issue_id"], state)
        except Exception as exc:  # noqa: BLE001
            print(f"[sources.linear] push_edit failed: {exc}", file=sys.stderr)
            _record_failure(conn, decision_id, "linear_edit_error", "linear_edit", exc)
            return
        conn.execute("UPDATE decision_requests SET linear_edit_error = NULL, linear_edit_pending = NULL WHERE id = ?",
                     (decision_id,))
        conn.commit()
        _clear_retry_state(conn, decision_id)
        db.resolve_sync_errors(conn, ["linear_edit"], before=started, decision_ids=[decision_id])
        db.resolve_sync_errors(conn, _TRANSIENT_ERROR_SOURCES, before=started)

    def mark_resolved(self, conn: sqlite3.Connection, decision_id: int, retrying: bool = False) -> None:
        row = conn.execute(
            "SELECT linear_issue_id, answer_text, answer_source FROM decision_requests WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if not row or not row["linear_issue_id"]:
            return
        started = _now()
        try:
            # Skip the comment when the answer came from Linear itself — it's
            # already visible there as the comment that produced it, so posting it
            # back would just echo the human's own reply. On a retry the earlier
            # attempt may have got as far as posting it before failing to close.
            if (
                row["answer_source"] != self.name and row["answer_text"]
                and not (retrying and _answer_comment_already_posted(row["linear_issue_id"]))
            ):
                linear_client.create_comment(
                    row["linear_issue_id"],
                    f"{ANSWER_COMMENT_PREFIX}{row['answer_source']}:**\n\n{row['answer_text']}",
                )
            linear_client.move_to_state(row["linear_issue_id"], "completed")
        except Exception as exc:  # noqa: BLE001
            print(f"[sources.linear] mark_resolved failed: {exc}", file=sys.stderr)
            _record_failure(conn, decision_id, "linear_resolve_error", "linear_resolve", exc)
            return
        conn.execute("UPDATE decision_requests SET linear_resolve_error = NULL WHERE id = ?", (decision_id,))
        conn.commit()
        _clear_retry_state(conn, decision_id)
        db.resolve_sync_errors(conn, ["linear_resolve"], before=started, decision_ids=[decision_id])
        db.resolve_sync_errors(conn, _TRANSIENT_ERROR_SOURCES, before=started)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_failure(
    conn: sqlite3.Connection, decision_id: int, error_column: str, error_source: str, exc: Exception
) -> None:
    """Shared by push and close — they never pend at once (a close needs the issue a
    push creates), so one attempt counter covers both, reset on each success."""
    attempts = conn.execute(
        "SELECT linear_retry_attempts FROM decision_requests WHERE id = ?", (decision_id,)
    ).fetchone()["linear_retry_attempts"] + 1
    delay = _RETRY_DELAYS[min(attempts, len(_RETRY_DELAYS)) - 1]
    conn.execute(
        f"UPDATE decision_requests SET {error_column} = ?, linear_retry_attempts = ?, "
        "linear_next_retry_at = ? WHERE id = ?",
        (str(exc), attempts, (datetime.now(timezone.utc) + delay).isoformat(), decision_id),
    )
    conn.commit()
    message = str(exc)
    if attempts >= config.sync_retry_max_attempts():
        message += f" — gave up after {attempts} attempts"
    db.record_sync_error(conn, error_source, decision_id, message)


def _clear_retry_state(conn: sqlite3.Connection, decision_id: int) -> None:
    conn.execute(
        "UPDATE decision_requests SET linear_retry_attempts = 0, linear_next_retry_at = NULL WHERE id = ?",
        (decision_id,),
    )
    conn.commit()


def _is_our_answer_comment(comment: dict) -> bool:
    return (comment.get("body") or "").startswith(ANSWER_COMMENT_PREFIX)


def _answer_comment_already_posted(issue_id: str) -> bool:
    return any(_is_our_answer_comment(c) for c in linear_client.get_comments(issue_id))


SOURCES: list[AnswerSource] = [LinearSource()]
