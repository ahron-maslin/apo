"""Clicks in a real (headless) browser. The answer forms' JavaScript once disabled the
first button during the submit event, so clicking option 1 or Done sent the form
without the clicked button — an empty answer, refused with a raw error page. Only
a browser builds form data that way, so only a browser test can catch it."""
import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apo import db

SHELL = Path.home() / ".cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell"
ROOT = Path(__file__).resolve().parent.parent

websockets = pytest.importorskip("websockets")
pytestmark = pytest.mark.skipif(not SHELL.exists(), reason="no headless Chromium shell installed")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(port, path, seconds=20):
    """Localhost only — via http.client, since conftest blocks urlopen against real APIs."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", path)
            return conn.getresponse().read()
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(path)


@pytest.fixture
def server(db_path, tmp_path):
    port = _free_port()
    env = dict(os.environ, APO_DB=str(db_path), APO_OFFLINE="1", APO_CLAUDE_BIN=str(tmp_path / "no-claude"),
               APO_POLL_SECONDS="100000", APO_LINEAR_API_KEY="", APO_LINEAR_TEAM="")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "apo.web.app:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for(port, "/settings")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _in_browser(url, selector, act, window_size=None):
    """Open url, wait for selector, then hand act an async js(expression) to drive the page."""
    port = _free_port()
    size = [f"--window-size={window_size[0]},{window_size[1]}"] if window_size else []
    browser = subprocess.Popen([str(SHELL), "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
                                *size, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def run():
        targets = json.loads(_wait_for(port, "/json"))
        page = next(t for t in targets if t["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            counter = 0

            async def call(method, **params):
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == counter:
                        return msg.get("result", {})

            async def js(expr):
                result = await call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
                return result.get("result", {}).get("value")

            await call("Page.enable")
            await call("Page.navigate", url=url)
            for _ in range(50):
                if await js(f"!!document.querySelector({json.dumps(selector)}) && document.readyState === 'complete'"):
                    break
                await asyncio.sleep(0.1)
            return await act(js)

    try:
        return asyncio.run(run())
    finally:
        browser.terminate()


def _click(url, selector):
    async def act(js):
        await js(f"document.querySelector({json.dumps(selector)}).click()")
        await asyncio.sleep(2)
        return await js("location.pathname"), await js("document.body.innerText")

    return _in_browser(url, selector, act)


def _project(conn, tmp_path):
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return project


def test_clicking_the_first_option_answers_with_it(server, tmp_path):
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=_project(conn, tmp_path)["id"], title="Queue",
                                             question="Which?", options=[{"label": "Postgres"}, {"label": "SQLite"}])

    path, text = _click(f"{server}/decisions/{decision_id}", 'button[name="option"][value="0"]')

    assert path == f"/decisions/{decision_id}"
    assert '"detail"' not in text
    assert db.get_decision_request(db.get_connection(), decision_id)["answer_text"].startswith("Chose option 1: Postgres")


def test_clicking_done_on_an_action_marks_it_done(server, tmp_path):
    conn = db.get_connection()
    action_id = db.create_action_request(conn, project_id=_project(conn, tmp_path)["id"], title="Add DNS",
                                         question="Add the record")

    _click(f"{server}/decisions/{action_id}", 'button[name="choice"][value="done"]')

    assert db.get_decision_request(db.get_connection(), action_id)["answer_kind"] == "done"


def test_ticking_a_checklist_step_stays_on_the_agents_tab(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    db.register_session(conn, "s-1", str(tmp_path))
    sid = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-1'").fetchone()["id"]
    item = db.add_checklist_item(conn, sid, "Write the tests")

    path, _ = _click(f"{server}/projects/{project['id']}/agents", f"#session-card-{sid} form.complete-item button")

    assert path == f"/projects/{project['id']}/agents"
    status = db.get_connection().execute("SELECT status FROM checklist_items WHERE id = ?", (item,)).fetchone()[0]
    assert status == "done"


def test_show_more_on_a_long_recap_opens_a_modal_with_the_full_text_that_survives_a_refresh(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    db.register_session(conn, "s-1", str(tmp_path))
    db.record_session_summary(conn, "s-1", "Fixed the flaky login test. " * 40)
    sid = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-1'").fetchone()["id"]
    toggle = f"#session-card-{sid} [data-recap-toggle]"

    async def act(js):
        await asyncio.sleep(0.5)
        shown = await js(f"!document.querySelector({json.dumps(toggle)}).hidden")
        await js(f"document.querySelector({json.dumps(toggle)}).click()")
        opened = await js("document.querySelector('dialog.recap-modal').open")
        full = await js("document.querySelector('dialog.recap-modal .recap-modal-text').textContent.length")
        await js("""fetch(location.href).then(r => r.text()).then(html => {
            const next = new DOMParser().parseFromString(html, "text/html").querySelector('[data-live-region="sessions"]');
            const region = document.querySelector('[data-live-region="sessions"]');
            region.innerHTML = next.innerHTML;
            App.autoEnhance(region);
        })""")
        await asyncio.sleep(0.3)
        still = await js("document.querySelector('dialog.recap-modal').open")
        await js("document.querySelector('dialog.recap-modal [value=close]').click()")
        closed = await js("!document.querySelector('dialog.recap-modal').open")
        return shown, opened, full > 1000, still, closed

    assert _in_browser(f"{server}/projects/{project['id']}/agents", toggle, act) == (True, True, True, True, True)


def test_show_more_on_the_all_projects_agents_page_opens_the_modal(server, tmp_path):
    conn = db.get_connection()
    _project(conn, tmp_path)
    db.register_session(conn, "s-1", str(tmp_path))
    db.record_session_summary(conn, "s-1", "Fixed the flaky login test. " * 40)
    sid = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-1'").fetchone()["id"]
    toggle = f"#session-card-{sid} [data-recap-toggle]"

    async def act(js):
        await asyncio.sleep(0.5)
        await js(f"document.querySelector({json.dumps(toggle)}).click()")
        return await js("document.querySelector('dialog.recap-modal').open")

    assert _in_browser(f"{server}/projects/agents", toggle, act) is True


def test_new_work_shows_the_fields_for_the_kind_you_pick(server, tmp_path):
    project = _project(db.get_connection(), tmp_path)

    async def act(js):
        shown = "(sel) => document.querySelector(sel).getClientRects().length > 0"
        state = f"[({shown})('#work-expected'), ({shown})('input[name=run_now]')]"
        before = await js(state)
        await js("document.querySelector('input[name=kind][value=bug]').click()")
        bug = await js(state)
        await js("document.querySelector('input[name=kind][value=me]').click()")
        me = await js(state)
        return before, bug, me

    assert _in_browser(f"{server}/projects/{project['id']}/work", "#work-title", act) == (
        [False, True], [True, True], [False, False])


def test_new_work_taller_than_the_window_scrolls_on_its_own(server, tmp_path):
    """Beside a long work list, New work stuck to the top with its bottom (the Add
    button) out of reach until the page had scrolled to the end of the list."""
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    for i in range(40):
        db.add_work(conn, project["id"], "task", f"Item {i}")

    async def act(js):
        await js("document.querySelector('input[name=kind][value=bug]').click()")
        await js("scrollTo(0, 400)")
        await asyncio.sleep(0.2)
        return await js("""(() => {
            const aside = document.querySelector('#new-work');
            return [aside.scrollHeight > aside.clientHeight, aside.getBoundingClientRect().bottom <= innerHeight];
        })()""")

    assert _in_browser(f"{server}/projects/{project['id']}/work", "#work-title", act,
                       window_size=(1400, 560)) == [True, True]




def test_an_open_work_tab_shows_new_work_within_seconds(server, tmp_path):
    project = _project(db.get_connection(), tmp_path)

    async def act(js):
        await asyncio.sleep(1)
        db.create_task(db.get_connection(), project_id=project["id"], title="Filed by a hook elsewhere")
        for _ in range(40):
            if await js("document.body.innerText.includes('Filed by a hook elsewhere')"):
                return True
            await asyncio.sleep(0.1)
        return False

    assert _in_browser(f"{server}/projects/{project['id']}/work", "#work-title", act) is True


def test_done_on_a_work_row_closes_it_and_stays_on_work(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    action = db.add_work(conn, project["id"], "me", "Renew the domain")

    path, text = _click(f"{server}/projects/{project['id']}/work", "[data-key-done]")

    assert path == f"/projects/{project['id']}/work" and '"detail"' not in text
    assert db.get_decision_request(db.get_connection(), action)["status"] == "resolved"


PRESS = "(key) => document.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}))"


def test_e_marks_the_selected_work_row_done(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    first = db.add_work(conn, project["id"], "me", "Renew the domain")
    second = db.add_work(conn, project["id"], "me", "Call the bank")

    async def act(js):
        await js(f"({PRESS})('e')")
        await asyncio.sleep(0.5)
        await js(f"['j', 'j', 'e'].forEach({PRESS})")
        await asyncio.sleep(2)
        return await js("location.pathname")

    assert _in_browser(f"{server}/projects/{project['id']}/work", "[data-key-done]", act) == f"/projects/{project['id']}/work"
    conn = db.get_connection()
    assert [db.get_decision_request(conn, i)["status"] for i in (first, second)] == ["open", "resolved"]


def test_e_does_nothing_once_a_refresh_has_dropped_the_selection(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    action = db.add_work(conn, project["id"], "me", "Renew the domain")

    async def act(js):
        await js(f"({PRESS})('j')")
        # What live.js does when anything changes: swap in the region from a fresh fetch.
        await js("""fetch(location.href).then(r => r.text()).then(html => {
            const next = new DOMParser().parseFromString(html, "text/html").querySelector('[data-live-region="work"]');
            document.querySelector('[data-live-region="work"]').innerHTML = next.innerHTML;
        })""")
        await js(f"({PRESS})('e')")
        await asyncio.sleep(1)
        return await js("document.querySelectorAll('.is-key-selected').length")

    assert _in_browser(f"{server}/projects/{project['id']}/work", "[data-key-done]", act) == 0
    assert db.get_decision_request(db.get_connection(), action)["status"] == "open"


PASTE_PNG = """(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 4;
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    const clipboard = new DataTransfer();
    clipboard.items.add(new File([blob], 'image.png', {type: 'image/png'}));
    document.querySelector('#work-title').dispatchEvent(
        new ClipboardEvent('paste', {clipboardData: clipboard, bubbles: true, cancelable: true}));
})()"""


def test_screenshots_pasted_into_new_work_are_saved_with_it(server, tmp_path):
    project = _project(db.get_connection(), tmp_path)

    async def act(js):
        await js("document.querySelector('input[name=kind][value=bug]').click()")
        await js("document.querySelector('#work-title').value = 'Crash on save'")
        await js(PASTE_PNG)
        await js(PASTE_PNG)
        await asyncio.sleep(0.2)
        await js("document.querySelector('[data-screenshot-grid] .attachment-remove').click()")
        shown = await js("document.querySelectorAll('[data-screenshot-grid] img').length")
        await js("document.querySelector('#work-title').form.requestSubmit()")
        await asyncio.sleep(2)
        return shown

    assert _in_browser(f"{server}/projects/{project['id']}/work", "#work-title", act) == 1
    conn = db.get_connection()
    [item] = conn.execute("SELECT id FROM decision_requests").fetchall()
    assert [a["filename"] for a in db.list_attachments(conn, item["id"])] == ["image.png"]


def _contrast(fg, bg):
    def luminance(rgb):
        channels = [int(c) / 255 for c in rgb.split("(")[1].split(")")[0].replace(",", " ").split()[:3]]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    light, dark = sorted([luminance(fg), luminance(bg)], reverse=True)
    return (light + 0.05) / (dark + 0.05)


FILLED_ACCENT_COLOURS = """(async () => {
    const out = {};
    for (const theme of ['light', 'dark']) {
        document.documentElement.dataset.theme = theme;
        await new Promise(resolve => setTimeout(resolve, 500));
        const button = document.querySelector('.btn--primary');
        const count = document.querySelector('.nav-count[data-tone="accent"]');
        const hoverRule = [...document.styleSheets].flatMap(s => [...s.cssRules])
            .find(r => r.selectorText === '.btn--primary:hover:not(:disabled)');
        const probe = document.createElement('span');
        probe.style.background = hoverRule.style.background || hoverRule.style.backgroundColor;
        document.body.append(probe);
        out[theme] = {
            button: [getComputedStyle(button).color, getComputedStyle(button).backgroundColor],
            hover: [getComputedStyle(button).color, getComputedStyle(probe).backgroundColor],
            count: [getComputedStyle(count).color, getComputedStyle(count).backgroundColor],
        };
        probe.remove();
    }
    return out;
})()"""


def test_white_text_on_the_accent_fill_is_readable_in_both_themes(server, tmp_path):
    conn = db.get_connection()
    db.create_action_request(conn, project_id=_project(conn, tmp_path)["id"], title="Add DNS", question="Add the record")

    async def act(js):
        return await js(FILLED_ACCENT_COLOURS)

    colours = _in_browser(f"{server}/", ".btn--primary", act)
    for theme, parts in colours.items():
        for part, (fg, bg) in parts.items():
            assert _contrast(fg, bg) >= 4.5, f"{theme} {part}: {fg} on {bg}"


FOCUSED_SHADOWS = """(async () => {
    const out = {};
    for (const selector of ['.btn--primary', '.plan-bar .btn--secondary']) {
        const el = document.querySelector(selector);
        el.focus();
        await new Promise(resolve => setTimeout(resolve, 500));
        out[selector] = [el.matches(':focus-visible'), getComputedStyle(el).boxShadow];
    }
    return out;
})()"""


def test_keyboard_focus_rings_filled_and_outlined_buttons(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    db.create_action_request(conn, project_id=project["id"], title="Add DNS", question="Add the record")
    db.create_decision_request(conn, project_id=project["id"], title="Queue", question="Which?",
                               options=[{"label": "Postgres"}, {"label": "SQLite"}])

    async def act(js):
        return await js(FOCUSED_SHADOWS)

    for selector, (visible, shadow) in _in_browser(f"{server}/", ".plan-bar", act).items():
        assert visible, selector
        assert "0px 0px 0px 4px" in shadow, f"{selector}: {shadow}"


def test_the_now_page_fits_a_phone_screen(server, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path)
    db.create_action_request(conn, project_id=project["id"], title="Point the shop's domain at the new host",
                             question="Add the record")
    db.create_decision_request(conn, project_id=project["id"], title="Allow guest checkout?", question="Allow it?",
                               options=[{"label": "Yes", "reversible": True}, {"label": "No", "reversible": True}],
                               recommended_option=0, confidence=90)

    async def act(js):
        return await js("[innerWidth, document.documentElement.scrollWidth]")

    width, scroll_width = _in_browser(f"{server}/", ".plan-bar-bulk", act, window_size=(360, 800))
    assert width == 360
    assert scroll_width <= width


def test_the_burnup_chart_keeps_its_distance_from_the_steps(server, tmp_path):
    conn = db.get_connection()
    outcome_id = db.create_outcome(conn, _project(conn, tmp_path)["id"], "Ship 1.0")
    db.add_step(conn, outcome_id, "Write the release notes", "agent")
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conn.execute("UPDATE outcomes SET created_at = ? WHERE id = ?", (three_days_ago, outcome_id))
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE outcome_id = ?", (three_days_ago, outcome_id))
    conn.commit()

    async def act(js):
        return await js("document.querySelector('.checklist-steps').getBoundingClientRect().top"
                        " - document.querySelector('.outcome-trend').getBoundingClientRect().bottom")

    assert _in_browser(f"{server}/outcomes/{outcome_id}", ".outcome-trend", act) >= 12
