"""The Work tab as agreed with the owner: filters in one menu, the overdue / due soon /
stuck counts as filters, each row's rarely used moves under its own menu, Done this
week capped at five, and New work folded down to what you need to add something."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return project


def _stuck(conn, item_id):
    at = (datetime.now(timezone.utc) - timedelta(days=db.STUCK_ITEM_DAYS + 1, hours=1)).isoformat()
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?", (at, item_id))
    conn.commit()


def test_filters_live_in_one_menu_and_show_as_chips_when_used(client, acme):
    db.add_work(db.get_connection(), acme["id"], "me", "Renew the domain", labels="billing")
    page = f"/projects/{acme['id']}/work"

    plain = client.get(page).text
    menu = re.search(r'<details class="[^"]*filter-menu[^"]*"([^>]*)>(.*?)</details>', plain, re.S)
    assert menu and "open" not in menu.group(1) and 'name="owner"' in menu.group(2)

    filtered = client.get(page + "?owner=me&label=billing").text
    chips = dict(re.findall(r'<a class="badge filter-chip[^"]*" href="([^"]+)"[^>]*>([^<]+)', filtered))
    assert {v.strip() for v in chips.values()} == {"Owner: Me", "Label: billing"}


def test_sorting_is_a_filed_or_due_toggle(client, acme):
    html = client.get(f"/projects/{acme['id']}/work?sort=due").text

    toggle = html[html.index('class="sort-toggle'):]
    toggle = toggle[:toggle.index("</div>")]
    assert re.search(r'href="[^"]*/work"[^>]*>Filed<', toggle)
    assert re.search(r'aria-current="true"[^>]*>Due<', toggle)


def test_the_due_and_stuck_counts_filter_the_list(client, acme):
    conn = db.get_connection()
    overdue = db.add_work(conn, acme["id"], "me", "Renew the domain", due_on=(date.today() - timedelta(days=1)).isoformat())
    stuck = db.create_task(conn, project_id=acme["id"], title="Write the migration")
    _stuck(conn, stuck)
    page = f"/projects/{acme['id']}/work"

    html = client.get(page).text
    assert 'id="work-overdue"' not in html and 'id="work-due_soon"' not in html
    assert f'href="{page}?due=stuck"' in html and f'href="{page}?due=overdue"' in html

    only_stuck = client.get(page + "?due=stuck").text
    assert "Write the migration" in only_stuck and "Renew the domain" not in only_stuck
    assert overdue and "Stuck" in only_stuck


def test_a_rows_rare_moves_sit_in_its_menu(client, acme):
    conn = db.get_connection()
    mine = db.add_work(conn, acme["id"], "me", "Renew the domain")
    task = db.create_task(conn, project_id=acme["id"], title="Write the migration")

    html = client.get(f"/projects/{acme['id']}/work").text

    def menu(item):
        row = html[html.index(f'value="{item}"'):]
        row = row[:row.index("</li>")]
        return re.search(r'<details class="[^"]*row-more[^"]*".*?</details>', row, re.S).group(0)

    assert f'action="/items/{mine}/cancel"' in menu(mine) and f'action="/decisions/{mine}/snooze"' in menu(mine)
    assert f'action="/items/{task}/rank"' in menu(task) and "snooze" not in menu(task)
    assert 'class="rank-controls"' not in html


def test_done_this_week_shows_the_latest_five(client, acme):
    conn = db.get_connection()
    for n in range(7):
        db.complete_step(conn, db.add_work(conn, acme["id"], "me", f"Errand {n}"), by="human")

    html = client.get(f"/projects/{acme['id']}/work").text
    section = html[html.index('id="work-done"'):]

    fold = re.search(r'<details class="[^"]*show-all[^"]*">\s*<summary>([^<]*)</summary>', section)
    assert fold and fold.group(1).strip() == "Show all 7"
    assert section[:section.index('class="show-all')].count('class="history-item') == 5


def test_new_work_is_folded_to_what_you_need(client, acme):
    html = client.get(f"/projects/{acme['id']}/work").text
    form = html[html.index('class="form-stack new-work"'):]
    form = form[:form.index("</form>")]
    more = re.search(r'<details class="[^"]*new-work-more[^"]*".*?</details>', form, re.S).group(0)

    for field in ('id="work-detail"', 'id="work-due"', 'id="work-labels"', 'name="run_now"'):
        assert field in more, field
    assert 'id="work-title"' not in more and 'name="kind"' not in more


def test_all_projects_uses_the_same_new_work_with_a_project_picker(client, acme):
    html = client.get("/projects/work").text

    assert "<h2>Add work</h2>" not in html
    form = html[html.index('action="/projects/items"'):]
    form = form[:form.index("</form>")]
    assert 'name="project"' in form and 'value="bug"' in form and "new-work-more" in form

    client.post("/projects/items", data={"project": str(acme["id"]), "kind": "bug", "title": "Export crashes"})
    item = db.get_connection().execute("SELECT id FROM decision_requests WHERE title = 'Export crashes'").fetchone()
    assert "bug" in db.get_decision_request(db.get_connection(), item["id"])["labels"]
