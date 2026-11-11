"""The made-up dashboard the website's screenshots show: three projects (larkspur-store,
a ceramics shop; riverbend-pipeline, a sales data pipeline; trailmate-app, a hiking
app), their sessions, a goal and the questions they ask. Run by run.sh against an empty
database in a scratch HOME; it refuses anything else.

    seed.py SCRATCH"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(sys.argv[1]).resolve()
assert os.environ.get("APO_OFFLINE") == "1", "APO_OFFLINE=1 required"
assert Path(os.environ["APO_DB"]).resolve().is_relative_to(SCRATCH), "APO_DB must be in the scratch folder"
assert Path(os.environ["HOME"]).resolve().is_relative_to(SCRATCH), "HOME must be in the scratch folder"
assert not Path(os.environ["APO_DB"]).exists(), "the database must start empty"

from apo import db  # noqa: E402

HOME = Path(os.environ["HOME"])
ROOT = HOME / "projects"
UTC = timezone.utc
conn = db.get_connection()
db.ensure_schema(conn)


def ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


def backdate(item_id: int, column="created_at", **kw):
    conn.execute(f"UPDATE decision_requests SET {column} = ? WHERE id = ?", (ago(**kw), item_id))
    conn.commit()


def project(name):
    p = db.get_or_create_project(conn, name)
    folder = ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    r = db.get_or_create_repository(conn, p["id"], str(folder))
    return p["id"], r["id"], str(folder)


def transcript(sid, cwd, turns):
    folder = HOME / ".claude" / "projects" / ("-" + cwd.strip("/").replace("/", "-"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    t = datetime.now(UTC) - timedelta(minutes=50)
    lines = []
    for kind, text in turns:
        t += timedelta(minutes=3)
        stamp = t.isoformat().replace("+00:00", "Z")
        if kind == "user":
            lines.append({"type": "user", "origin": {"kind": "human"}, "timestamp": stamp,
                          "message": {"role": "user", "content": text}})
        else:
            lines.append({"type": "assistant", "timestamp": stamp, "message": {
                "role": "assistant", "model": "claude-demo", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 1500, "cache_read_input_tokens": 60000}}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(path)


def session(sid, cwd, prompt, title, summary=None, ctx=None, todos=None, stopped=True):
    tp = transcript(sid, cwd, [("user", prompt), ("assistant", summary or "Working on it.")])
    db.register_session(conn, sid, cwd, transcript_path=tp)
    db.session_prompt_submitted(conn, sid, prompt, cwd)
    db.session_heartbeat(conn, sid, cwd)
    db.record_session_summary(conn, sid, summary, title=title, last_prompt=prompt, context_tokens=ctx)
    if todos:
        db.sync_todo_list(conn, sid, [{"content": c, "status": s} for c, s in todos])
    if stopped:
        db.session_stop(conn, sid)


riverbend, river_repo, river_dir = project("riverbend-pipeline")
larkspur, lark_repo, lark_dir = project("larkspur-store")
trailmate, trail_repo, trail_dir = project("trailmate-app")

db.set_project_brief(conn, riverbend, goal="Nightly sales pipeline: point-of-sale exports into one reporting table.")
db.set_project_brief(conn, larkspur, goal="Online shop for Larkspur Ceramics: hand-thrown mugs, bowls and planters.",
                     links="Shop https://shop.larkspur-ceramics.example")
db.set_project_brief(conn, trailmate, goal="A hiking app: trail search, trail pages and offline maps.")

# Sessions, as the Agents tab shows them.
session("demo-lk-checkout", lark_dir, "Build the checkout: cart, shipping and payment", "Checkout: cart, shipping, payment",
        "Cart and shipping rates are done. Paused on the payment provider and on stock counts: filed #4 and #5.", 97_000,
        [("Cart", "completed"), ("Shipping rates", "completed"), ("Payment step", "in_progress"), ("Order email", "pending")])
session("demo-lk-golive", lark_dir, "Get the shop ready to go live on its own domain", "Go live on shop.larkspur-ceramics.example",
        "Deployed to the host and set up redirects from the old shop. The domain needs a CNAME before it goes live: filed #6.",
        42_000)
session("demo-rb-exports", river_dir, "Read the new store exports from the POS bucket", "New store exports",
        "Mapped the new stores' export schema. Waiting on read access (#3) to load them.", 34_000)
session("demo-rb-late", river_dir, "Handle events that arrive after the nightly cutoff", "Late-arriving sales events",
        "Measured late arrivals and added dedupe. Filed #2 on what to do with late rows.", 61_000,
        [("Measure late arrivals", "completed"), ("Dedupe", "completed"), ("Late-row policy", "pending")])
session("demo-rb-staging", river_dir, "Rebuild the staging models after the dedupe change", "Rebuild staging after dedupe",
        None, 29_000, [("Orders model", "completed"), ("Refunds model", "in_progress"), ("Daily totals", "pending")],
        stopped=False)
session("demo-tm-maps", trail_dir, "Offline map downloads per region", "Offline maps",
        "Region picker and download queue are in; working on storage limits.", 55_000,
        [("Region picker", "completed"), ("Download queue", "completed"), ("Storage limits", "in_progress")],
        stopped=False)
session("demo-tm-search", trail_dir, "Make trail search tolerate typos", "Trail search",
        "Fuzzy matching is in; searches for 'pacfic crest' now find the Pacific Crest Trail.", 23_000)

# 1: an older decision, answered and worked.
first = db.create_decision_request(conn, project_id=riverbend, repository_id=river_repo, title="Keep raw exports for how long?",
                                   question="How many days of raw POS exports should the bucket keep?",
                                   options=[{"label": "30 days"}, {"label": "90 days"}], recommended_option=1,
                                   recommendation="90 days, to cover a quarter's reconciliation", confidence=80)
db.answer_decision_request(conn, first, "90 days, to cover a quarter's reconciliation", source="dashboard")
db.resolve_decision_request(conn, first)
backdate(first, days=6)

# 2: the decision the screenshots lead with.
late = db.create_decision_request(
    conn, project_id=riverbend, repository_id=river_repo, origin_session_id="demo-rb-late",
    title="Events that arrive after the cutoff: backfill or quarantine?",
    question="Late sales events land after the 02:00 cutoff. Rewrite the affected days, or keep late rows apart?",
    summary="About 2% of sales events arrive after the 02:00 cutoff, some up to three days late. Should the pipeline "
            "rewrite old days or set those rows aside?",
    context="Late rows come from two stores whose tills sync over a weekly connection.",
    options=[{"label": "Backfill up to 3 days", "consequence": "Rewrite the last 3 daily partitions each night.", "reversible": True},
             {"label": "Quarantine late rows", "consequence": "Write them to sales_late and report them separately.", "reversible": True},
             {"label": "Drop rows older than 24h", "consequence": "Late rows are discarded at load time.", "reversible": False}],
    recommended_option=0, recommendation="Backfill up to 3 days: no late event is older than that, and it keeps one table.",
    confidence=70, blocked_work=["Late-row policy"], safe_work=["Dedupe", "Staging rebuild"],
    resume_plan={"objective": "Apply the late-row policy", "remaining_steps": ["Implement it in load_sales", "Add a test"],
                 "expected_outcome": "Nightly totals match the tills"})
backdate(late, hours=4)

# 3: an action only you can do.
access = db.create_action_request(
    conn, project_id=riverbend, repository_id=river_repo, origin_session_id="demo-rb-exports",
    title="Give the pipeline read access to the exports bucket",
    question="Create a read-only key for the pos-exports bucket and add it to the pipeline's secrets.",
    summary="The pipeline can't read the new stores' exports yet. It needs a read-only key for the pos-exports bucket.")
backdate(access, hours=1)

# 4 and 5: the checkout session's questions.
pay = db.create_decision_request(
    conn, project_id=larkspur, repository_id=lark_repo, origin_session_id="demo-lk-checkout",
    title="Which payment provider for checkout?", question="Stripe or Square for card payments?",
    summary="Checkout needs one card provider. Square matches the card reader used at markets.",
    options=[{"label": "Stripe", "consequence": "Payment step done this week", "reversible": True},
             {"label": "Square", "consequence": "Two extra days; one dashboard for market and online", "reversible": True}],
    recommended_option=1, recommendation="Square, so market and online sales share one dashboard", confidence=75)
backdate(pay, hours=3)
stock = db.create_decision_request(
    conn, project_id=larkspur, repository_id=lark_repo, origin_session_id="demo-lk-checkout",
    title="Show stock counts on product pages?", question="Show how many are left of each piece?",
    summary="Small batches sell out; counts create urgency but look odd at 1.",
    options=[{"label": "Only when fewer than 3 are left", "reversible": True}, {"label": "Never", "reversible": True}],
    recommended_option=0, recommendation="Only when fewer than 3 are left", confidence=65)
backdate(stock, hours=2)

# 6: the domain.
dns = db.create_action_request(
    conn, project_id=larkspur, repository_id=lark_repo, origin_session_id="demo-lk-golive",
    title="Point shop.larkspur-ceramics.example at the host",
    question="Add a CNAME record for shop.larkspur-ceramics.example pointing at the host.",
    summary="The shop is deployed but its domain doesn't point at it yet. Add one CNAME record at the DNS provider.")
backdate(dns, hours=1)

# 7 to 11: the TestFlight goal.
goal = db.create_outcome(conn, trailmate, "Ship 1.0 to TestFlight", done_when="Build 1.0 is installable by the 30-person beta group.",
                         description="First public beta: trail search, trail pages, and offline maps.",
                         due_on=(datetime.now(UTC) + timedelta(days=5)).date().isoformat())
crash = db.add_step(conn, goal, "Fix the crash when location permission is denied", owner="agent", repository_id=trail_repo)
maps = db.add_step(conn, goal, "Offline map downloads per region", owner="agent", repository_id=trail_repo)
listing = db.add_step(conn, goal, "Create the App Store Connect listing", owner="me", repository_id=trail_repo,
                      detail="App name, subtitle, privacy answers and the beta group.")
review = db.add_step(conn, goal, "Design review of the onboarding screens", owner="external", waiting_on="Priya (design)",
                     follow_up_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(), repository_id=trail_repo)
notes = db.add_step(conn, goal, "Write the TestFlight release notes", owner="agent", repository_id=trail_repo,
                    blocked_by=[maps, listing])
db.claim_task(conn, crash, "demo-tm-search")
db.complete_task(conn, crash, note="Fixed: the map falls back to the last region when location is denied. Added a UI test.")
db.claim_task(conn, maps, "demo-tm-maps")
conn.execute("UPDATE decision_requests SET claimed_at = ? WHERE id = ?", (ago(minutes=50), maps))
backdate(listing, days=2)
conn.commit()

# 12: answered and on its way to an agent (the site's walkthrough uses it).
ios = db.create_decision_request(
    conn, project_id=trailmate, repository_id=trail_repo, title="Lowest iOS version to support?",
    question="What's the minimum iOS version for 1.0?",
    options=[{"label": "iOS 16", "reversible": True}, {"label": "iOS 17", "reversible": True}],
    recommended_option=0, recommendation="iOS 16: wider reach for a beta, and the workaround is small.", confidence=85)
db.answer_decision_request(conn, ios, "Accepted your recommendation: iOS 16: wider reach for a beta, and the workaround is small.",
                           source="dashboard")

# A finished turn to review, and the dashboard last looked at just now (no "since you were away").
db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
db.set_ui_state(conn, "last_visit_at", datetime.now(UTC).isoformat())
print("seeded")
