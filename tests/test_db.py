import pytest

from apo import db


def test_get_or_create_project_creates_once(db_path):
    conn = db.get_connection()
    p1 = db.get_or_create_project(conn, "acme")
    p2 = db.get_or_create_project(conn, "acme")
    assert p1["id"] == p2["id"]
    assert p1["name"] == "acme"


def test_create_repository_under_project(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", "/srv/acme-backend")
    assert repo["project_id"] == project["id"]
    assert repo["name"] == "backend"
    assert repo["local_path"] == "/srv/acme-backend"


def test_get_or_create_repository_reuses_existing_path(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    first = db.get_or_create_repository(conn, project["id"], "/srv/acme-backend")
    second = db.get_or_create_repository(conn, project["id"], "/srv/acme-backend")
    assert first["id"] == second["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM repositories WHERE project_id = ?", (project["id"],)
    ).fetchone()[0] == 1


def test_get_or_create_repository_names_from_path_basename(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.get_or_create_repository(conn, project["id"], "/srv/acme-backend")
    assert repo["name"] == "acme-backend"
    assert repo["local_path"] == "/srv/acme-backend"


def test_get_or_create_repository_falls_back_to_path_on_name_collision(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "acme-backend", "/some/other/path")
    repo = db.get_or_create_repository(conn, project["id"], "/srv/acme-backend")
    assert repo["local_path"] == "/srv/acme-backend"
    assert repo["name"] == "/srv/acme-backend"


def test_create_decision_request_with_resume_plan(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        title="Clarify mobile navigation",
        question="Bottom tabs, drawer, or sidebar?",
        context="Desktop implementation is complete.",
        why_it_matters="Only the mobile navigation component depends on this.",
        options=[{"label": "Bottom tabs"}, {"label": "Drawer"}, {"label": "Sidebar"}],
        blocked_work=["MobileMenu.tsx", "mobile navigation tests"],
        safe_work=["Backend", "Authentication", "Documentation"],
        recommendation="Drawer navigation.",
        confidence=72,
        resume_plan={
            "objective": "Implement mobile navigation",
            "files": ["MobileMenu.tsx"],
            "previous_reasoning": "Desktop uses a top nav; mobile needs its own pattern.",
            "remaining_steps": ["Implement drawer", "Update layout", "Update tests"],
            "expected_outcome": "Mobile nav matches desktop functionality.",
        },
    )
    row = db.get_decision_request(conn, decision_id)
    assert row["title"] == "Clarify mobile navigation"
    assert row["status"] == "open"
    assert row["confidence"] == 72
    assert row["options"] == [
        {"label": "Bottom tabs", "detail": None, "consequence": None, "reversible": None},
        {"label": "Drawer", "detail": None, "consequence": None, "reversible": None},
        {"label": "Sidebar", "detail": None, "consequence": None, "reversible": None},
    ]
    assert row["blocked_work"] == ["MobileMenu.tsx", "mobile navigation tests"]
    assert row["resume_plan"]["objective"] == "Implement mobile navigation"
    assert row["resume_plan"]["remaining_steps"] == ["Implement drawer", "Update layout", "Update tests"]


def test_create_decision_request_stores_and_returns_summary(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        title="Clarify mobile navigation",
        question="Bottom tabs, drawer, or sidebar?",
        summary="Pick a mobile nav pattern before MobileMenu.tsx can ship.",
        context="Desktop implementation is complete.",
    )
    row = db.get_decision_request(conn, decision_id)
    assert row["summary"] == "Pick a mobile nav pattern before MobileMenu.tsx can ship."


def test_get_decision_request_normalizes_plain_string_options_to_label_dicts(db_path):
    """Real callers file options as a plain list of strings (a much more natural
    shape than a list of {label, detail} dicts), but linear_client.create_issue and
    decision.html both assumed dicts — crashing Linear pushes with
    "'str' object has no attribute 'get'" and rendering blank <li>s in the UI.
    Normalizing here, at the single read path every consumer goes through, fixes
    both without every consumer needing its own type check."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        title="Q",
        question="Q?",
        options=["Bottom tabs", "Drawer"],
    )
    row = db.get_decision_request(conn, decision_id)
    assert row["options"] == [
        {"label": "Bottom tabs", "detail": None, "consequence": None, "reversible": None},
        {"label": "Drawer", "detail": None, "consequence": None, "reversible": None},
    ]


def test_get_decision_request_preserves_dict_options_with_detail(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        title="Q",
        question="Q?",
        options=[{"label": "Bottom tabs", "detail": "Simple, familiar"}],
    )
    row = db.get_decision_request(conn, decision_id)
    assert row["options"] == [{"label": "Bottom tabs", "detail": "Simple, familiar", "consequence": None, "reversible": None}]


def test_list_decision_requests_filters_by_status(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    open_id = db.create_decision_request(
        conn, project_id=project["id"], title="Open one", question="Q1",
    )
    answered_id = db.create_decision_request(
        conn, project_id=project["id"], title="Answered one", question="Q2",
    )
    db.answer_decision_request(conn, answered_id, "Use drawer navigation.")

    open_rows = db.list_decision_requests(conn, status="open")
    answered_rows = db.list_decision_requests(conn, status="answered")

    assert [r["id"] for r in open_rows] == [open_id]
    assert [r["id"] for r in answered_rows] == [answered_id]


def test_list_decision_requests_search_matches_title_question_context(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    drive_id = db.create_decision_request(
        conn, project_id=project["id"], title="Need SharePoint drive ID",
        question="What's the drive ID?", context="Blocks Excel write-back",
    )
    db.create_decision_request(
        conn, project_id=project["id"], title="Unrelated", question="Something else",
    )

    assert [r["id"] for r in db.list_decision_requests(conn, search="drive id")] == [drive_id]
    assert [r["id"] for r in db.list_decision_requests(conn, search="write-back")] == [drive_id]
    assert db.list_decision_requests(conn, search="nonexistent term") == []


def test_confidence_on_a_zero_to_ten_scale_is_stored_as_a_percentage(db_path):
    """Agents filed 6, 7, 8 meaning out of 10; the UI rendered them as 7%."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    stored = {
        given: db.get_decision_request(conn, db.create_decision_request(
            conn, project_id=project["id"], title="Q", question="Q?", confidence=given,
        ))["confidence"]
        for given in (7, 10, 0, 55, 100, 140, None)
    }
    assert stored == {7: 70, 10: 100, 0: 0, 55: 55, 100: 100, 140: 100, None: None}


def test_upgrading_rescales_existing_zero_to_ten_confidences(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    other_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?", confidence=70)
    conn.execute("UPDATE decision_requests SET confidence = 8 WHERE id = ?", (decision_id,))
    conn.execute("PRAGMA user_version = 4")
    conn.commit()

    conn = db.get_connection()

    assert db.get_decision_request(conn, decision_id)["confidence"] == 80
    assert db.get_decision_request(conn, other_id)["confidence"] == 70


def test_project_counts_groups_open_and_answered_by_project(db_path):
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    globex = db.get_or_create_project(conn, "globex")
    db.create_decision_request(conn, project_id=acme["id"], title="A1", question="Q1")
    answered_id = db.create_decision_request(conn, project_id=acme["id"], title="A2", question="Q2")
    db.answer_decision_request(conn, answered_id, "answered")
    db.create_decision_request(conn, project_id=globex["id"], title="G1", question="Q1")

    counts = db.project_counts(conn)

    assert counts["acme"]["open"] == 1
    assert counts["acme"]["total"] == 2
    assert counts["globex"]["open"] == 1
    assert counts["globex"]["total"] == 1


def test_answer_decision_request_is_atomic(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?",
    )
    first = db.answer_decision_request(conn, decision_id, "First answer")
    second = db.answer_decision_request(conn, decision_id, "Second answer")

    assert first is True
    assert second is False
    row = db.get_decision_request(conn, decision_id)
    assert row["answer_text"] == "First answer"
    assert row["status"] == "answered"


def test_audit_log_records_creation_and_answer(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "An answer")

    events = [row["event_type"] for row in db.list_audit_log(conn, decision_id)]
    assert events == ["created", "answered"]


def test_resolve_decision_request_is_atomic(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    before_answer = db.resolve_decision_request(conn, decision_id)
    assert before_answer is False
    assert db.get_decision_request(conn, decision_id)["status"] == "open"

    db.answer_decision_request(conn, decision_id, "An answer")
    first = db.resolve_decision_request(conn, decision_id)
    second = db.resolve_decision_request(conn, decision_id)

    assert first is True
    assert second is False
    assert db.get_decision_request(conn, decision_id)["status"] == "resolved"


def test_find_repository_by_path_matches_exact_and_subdirectory(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", "/srv/acme-backend")

    assert db.find_repository_by_path(conn, "/srv/acme-backend")["id"] == repo["id"]
    assert db.find_repository_by_path(conn, "/srv/acme-backend/src/lib")["id"] == repo["id"]
    assert db.find_repository_by_path(conn, "/srv/other-repo") is None
    assert db.find_repository_by_path(conn, "/srv/acme-backend-2") is None


def test_close_decision_request_force_closes_open_or_answered(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    open_id = db.create_decision_request(conn, project_id=project["id"], title="Open Q", question="Q?")
    answered_id = db.create_decision_request(conn, project_id=project["id"], title="Answered Q", question="Q?")
    db.answer_decision_request(conn, answered_id, "Real answer")

    assert db.close_decision_request(conn, open_id, "Closed — no longer needed.") is True
    closed_open = db.get_decision_request(conn, open_id)
    assert closed_open["status"] == "resolved"
    assert closed_open["answer_text"] == "Closed — no longer needed."
    assert closed_open["answer_source"] == "archive"

    assert db.close_decision_request(conn, answered_id, "Closed — no longer needed.") is True
    closed_answered = db.get_decision_request(conn, answered_id)
    assert closed_answered["status"] == "resolved"
    assert closed_answered["answer_text"] == "Real answer"  # real answer preserved, not overwritten

    assert db.close_decision_request(conn, open_id, "again") is False


def test_archive_project_closes_dangling_decisions_and_retains_all_records(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    open_id = db.create_decision_request(conn, project_id=project["id"], title="Open Q", question="Q?")
    resolved_id = db.create_decision_request(conn, project_id=project["id"], title="Resolved Q", question="Q?")
    db.answer_decision_request(conn, resolved_id, "answer")
    db.resolve_decision_request(conn, resolved_id)

    result = db.archive_project(conn, project["id"])

    assert result["archived"] is True
    assert result["closed_decision_ids"] == [open_id]
    assert db.get_decision_request(conn, open_id)["status"] == "resolved"
    assert db.get_decision_request(conn, resolved_id)["status"] == "resolved"

    counts = db.project_counts(conn)
    assert counts["acme"]["archived"] is True
    assert counts["acme"]["total"] == 2  # nothing deleted

    again = db.archive_project(conn, project["id"])
    assert again["archived"] is False


def test_unarchive_project_reverses_archive(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.archive_project(conn, project["id"])

    assert db.unarchive_project(conn, project["id"]) is True
    assert db.project_counts(conn)["acme"]["archived"] is False
    assert db.unarchive_project(conn, project["id"]) is False


def test_recent_sync_errors_since_hours_excludes_old_entries(db_path):
    from datetime import datetime, timedelta, timezone

    conn = db.get_connection()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute(
        "INSERT INTO sync_errors (occurred_at, source, decision_request_id, message) VALUES (?, ?, ?, ?)",
        (old_time, "linear_push", None, "stale"),
    )
    conn.commit()
    db.record_sync_error(conn, "linear_push", None, "fresh")

    windowed = [e["message"] for e in db.recent_sync_errors(conn, since_hours=24)]
    assert "fresh" in windowed
    assert "stale" not in windowed

    # No window (the CLI's use case) still sees full history, for debugging.
    everything = [e["message"] for e in db.recent_sync_errors(conn)]
    assert "stale" in everything
    assert "fresh" in everything


def test_resolve_sync_errors_only_marks_older_matching_rows(db_path):
    from datetime import datetime, timedelta, timezone

    conn = db.get_connection()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for source, decision_id in (("linear_poll", None), ("linear_push", 7), ("linear_push", 8), ("repo_writer", 7)):
        conn.execute(
            "INSERT INTO sync_errors (occurred_at, source, decision_request_id, message) VALUES (?, ?, ?, ?)",
            (old, source, decision_id, f"{source}-{decision_id}"),
        )
    conn.commit()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.record_sync_error(conn, "linear_poll", None, "after-cutoff")

    db.resolve_sync_errors(conn, ["linear_poll"], before=cutoff)
    db.resolve_sync_errors(conn, ["linear_push"], before=cutoff, decision_ids=[7])

    unresolved = sorted(e["message"] for e in db.recent_sync_errors(conn, limit=100, unresolved_only=True))
    assert unresolved == ["after-cutoff", "linear_push-8", "repo_writer-7"]
    assert len(db.recent_sync_errors(conn, limit=100)) == 5


def test_connector_enabled_by_default(db_path):
    conn = db.get_connection()
    assert db.is_connector_enabled(conn, "linear") is True
    assert db.is_connector_enabled(conn, "jira") is True  # unknown names default on too


def test_set_connector_enabled_persists_and_toggles(db_path):
    conn = db.get_connection()
    db.set_connector_enabled(conn, "linear", False)
    assert db.is_connector_enabled(conn, "linear") is False

    db.set_connector_enabled(conn, "linear", True)
    assert db.is_connector_enabled(conn, "linear") is True


def test_get_connector_settings_lists_only_explicitly_set_connectors(db_path):
    conn = db.get_connection()
    assert db.get_connector_settings(conn) == {}
    db.set_connector_enabled(conn, "linear", False)
    assert db.get_connector_settings(conn) == {"linear": False}


def test_get_repository_for_project_returns_sole_repo(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    assert db.get_repository_for_project(conn, project["id"]) is None

    repo = db.create_repository(conn, project["id"], "backend", "/srv/acme-backend")
    assert db.get_repository_for_project(conn, project["id"])["id"] == repo["id"]

    db.create_repository(conn, project["id"], "frontend", "/srv/acme-frontend")
    assert db.get_repository_for_project(conn, project["id"]) is None


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _make_decision(conn):
    project = db.get_or_create_project(conn, "acme")
    return db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")


def test_save_attachment_writes_file_and_row(db_path, config_module):
    conn = db.get_connection()
    decision_id = _make_decision(conn)
    data = PNG_MAGIC + b"fake-png-bytes"

    saved = db.save_attachment(conn, decision_id, "screenshot.png", "image/png", data)

    assert saved["decision_id"] == decision_id
    assert saved["filename"] == "screenshot.png"
    assert saved["content_type"] == "image/png"
    assert saved["size_bytes"] == len(data)
    assert saved["stored_name"].endswith(".png")
    assert saved["stored_name"] != "screenshot.png"  # never trusts the original name

    on_disk = config_module.get_attachments_dir() / str(decision_id) / saved["stored_name"]
    assert on_disk.read_bytes() == data


def test_save_attachment_rejects_disallowed_content_type(db_path):
    conn = db.get_connection()
    decision_id = _make_decision(conn)

    import pytest
    with pytest.raises(ValueError):
        db.save_attachment(conn, decision_id, "evil.svg", "image/svg+xml", b"<svg></svg>")


def test_save_attachment_rejects_content_mismatching_declared_type(db_path):
    """A client-supplied Content-Type isn't trusted on its own — the actual bytes
    must match it (magic-byte sniff), so a non-image file can't be stored (and
    later served, or copied into a target repo) under a false image type."""
    conn = db.get_connection()
    decision_id = _make_decision(conn)

    import pytest
    with pytest.raises(ValueError):
        db.save_attachment(conn, decision_id, "evil.png", "image/png", b"<script>alert(1)</script>")


def test_save_attachment_rejects_oversized_file(db_path):
    conn = db.get_connection()
    decision_id = _make_decision(conn)

    import pytest
    too_big = PNG_MAGIC + b"x" * (10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        db.save_attachment(conn, decision_id, "huge.png", "image/png", too_big)


def test_save_attachment_enforces_max_six_per_decision(db_path):
    conn = db.get_connection()
    decision_id = _make_decision(conn)

    for _ in range(6):
        db.save_attachment(conn, decision_id, "shot.png", "image/png", PNG_MAGIC + b"data")

    import pytest
    with pytest.raises(ValueError):
        db.save_attachment(conn, decision_id, "shot.png", "image/png", PNG_MAGIC + b"data")


def test_list_attachments_returns_saved_attachments_for_decision(db_path):
    conn = db.get_connection()
    decision_id = _make_decision(conn)
    other_decision_id = _make_decision(conn)
    db.save_attachment(conn, other_decision_id, "other.png", "image/png", PNG_MAGIC + b"other")

    first = db.save_attachment(conn, decision_id, "one.png", "image/png", PNG_MAGIC + b"one")
    second = db.save_attachment(conn, decision_id, "two.jpg", "image/jpeg", JPEG_MAGIC + b"two")

    listed = db.list_attachments(conn, decision_id)
    assert [a["id"] for a in listed] == [first["id"], second["id"]]
    assert [a["filename"] for a in listed] == ["one.png", "two.jpg"]


def test_delete_attachment_removes_row_and_file(db_path, config_module):
    conn = db.get_connection()
    decision_id = _make_decision(conn)
    saved = db.save_attachment(conn, decision_id, "one.png", "image/png", PNG_MAGIC + b"one")
    on_disk = config_module.get_attachments_dir() / str(decision_id) / saved["stored_name"]
    assert on_disk.exists()

    assert db.delete_attachment(conn, saved["id"]) is True

    assert not on_disk.exists()
    assert db.list_attachments(conn, decision_id) == []


def test_delete_attachment_returns_false_for_unknown_id(db_path):
    conn = db.get_connection()
    assert db.delete_attachment(conn, 999) is False


def test_delete_attachment_is_a_noop_if_file_already_gone(db_path, config_module):
    conn = db.get_connection()
    decision_id = _make_decision(conn)
    saved = db.save_attachment(conn, decision_id, "one.png", "image/png", PNG_MAGIC + b"one")
    on_disk = config_module.get_attachments_dir() / str(decision_id) / saved["stored_name"]
    on_disk.unlink()

    assert db.delete_attachment(conn, saved["id"]) is True
    assert db.list_attachments(conn, decision_id) == []

def test_source_import_failure_does_not_break_filing_or_answering(db_path, monkeypatch):
    import sys

    import apo.integrations

    monkeypatch.delattr(apo.integrations, "sources", raising=False)
    monkeypatch.setitem(sys.modules, "apo.integrations.sources", None)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?",
    )

    assert db.answer_decision_request(conn, decision_id, "A") is True

    assert db.get_decision_request(conn, decision_id)["status"] == "answered"
    errors = conn.execute("SELECT source FROM sync_errors").fetchall()
    assert [e["source"] for e in errors].count("sources") == 2


def _projects_root(tmp_path):
    root = tmp_path / "projects"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / ".git").mkdir()
    (root / "acme" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "notes").mkdir()
    return root


def test_create_project_without_a_folder(db_path, tmp_path):
    conn = db.get_connection()

    project = db.create_project(conn, "  acme  ", None, _projects_root(tmp_path))

    assert project["name"] == "acme"
    assert db.project_repositories(conn, project["id"]) == []
    assert any(e["verb"] == "project.created" for e in db.list_events(conn, project_id=project["id"]))


def test_create_project_attaches_a_folder_under_the_root(db_path, tmp_path):
    conn = db.get_connection()
    root = _projects_root(tmp_path)

    project = db.create_project(conn, "Acme", "acme", root)

    (repo,) = db.project_repositories(conn, project["id"])
    assert repo["local_path"] == str((root / "acme").resolve())
    assert repo["name"] == "Acme"
    assert db.find_repository_by_path(conn, str(root / "acme"))["project_id"] == project["id"]


def test_create_project_needs_a_name(db_path, tmp_path):
    conn = db.get_connection()

    with pytest.raises(ValueError):
        db.create_project(conn, "   ", None, _projects_root(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


@pytest.mark.parametrize("taken", ["acme", "ACME", "a-c-m-e", "old-acme"])
def test_create_project_refuses_a_name_already_in_use(db_path, tmp_path, taken):
    conn = db.get_connection()
    existing = db.get_or_create_project(conn, "acme")
    db.add_project_alias(conn, "old-acme", existing["id"])

    with pytest.raises(ValueError, match="already a project called acme"):
        db.create_project(conn, taken, None, _projects_root(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def _outside_folders(tmp_path, root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "sneaky").symlink_to(outside, target_is_directory=True)
    return [str(outside), "../elsewhere", "acme/../../elsewhere", "sneaky", ".", "..", "/", "missing"]


def test_create_project_refuses_a_folder_that_is_not_inside_the_root(db_path, tmp_path):
    conn = db.get_connection()
    root = _projects_root(tmp_path)

    for folder in _outside_folders(tmp_path, root):
        with pytest.raises(ValueError):
            db.create_project(conn, "acme", folder, root)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 0


def test_create_project_refuses_a_file_under_the_root(db_path, tmp_path):
    conn = db.get_connection()
    root = _projects_root(tmp_path)
    (root / "README").write_text("hi")

    with pytest.raises(ValueError):
        db.create_project(conn, "acme", "README", root)


def test_create_project_refuses_a_folder_another_project_has(db_path, tmp_path):
    conn = db.get_connection()
    root = _projects_root(tmp_path)
    other = db.get_or_create_project(conn, "other")
    db.create_repository(conn, other["id"], "other", str((root / "acme").resolve()))

    with pytest.raises(ValueError, match="other"):
        db.create_project(conn, "acme", "acme", root)
    assert db.find_project(conn, "acme") is None


def test_project_folder_choices_lists_the_roots_subfolders(db_path, tmp_path):
    conn = db.get_connection()
    root = _projects_root(tmp_path)
    (root / "Beta").mkdir()
    (root / ".cache").mkdir()
    (root / "README").write_text("hi")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "sneaky").symlink_to(outside, target_is_directory=True)
    taken = db.get_or_create_project(conn, "Notes App")
    db.create_repository(conn, taken["id"], "notes", str((root / "notes").resolve()))

    choices = db.project_folder_choices(conn, root)

    assert [c["name"] for c in choices] == ["acme", "Beta", "notes"]
    assert {c["name"]: c["git"] for c in choices} == {"acme": True, "Beta": False, "notes": False}
    assert {c["name"]: c["project"] for c in choices} == {"acme": None, "Beta": None, "notes": "Notes App"}


def test_project_folder_choices_is_empty_when_the_root_is_missing(db_path, tmp_path):
    assert db.project_folder_choices(db.get_connection(), tmp_path / "nope") == []
