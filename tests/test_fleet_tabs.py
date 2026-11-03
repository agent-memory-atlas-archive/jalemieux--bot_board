"""/fleet in tabs: the hash picks the tab, so older links land where they point; a bot's actions
are in the drawer under its row; the filters narrow the rows. Needs Playwright and Chromium; skipped
without them. Run with python -m unittest discover -s tests."""
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient       # noqa: E402
from app import db, fleet, main                  # noqa: E402


class FleetTabs(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for /fleet UI checks")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "DB_PATH", os.path.join(tmp, "board.db")))
        self.stack.enter_context(patch.object(db, "_local", threading.local()))
        self.stack.enter_context(patch.object(main, "READ_TOKEN_REQUIRED", False))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.addCleanup(lambda: hasattr(db._local, "conn") and db._local.conn.close())
        token = self.client.post("/api/agents", json={"handle": "jac", "kind": "human"}).json()["token"]
        keeper = self.client.post("/api/agents", json={"handle": "keeper-box", "kind": "keeper"}).json()["token"]
        self.client.put("/api/fleet/hosts/box", headers={"Authorization": "Bearer " + keeper}, json={
            "load": 1, "cores": 4, "mem_total_mb": 1000, "mem_free_mb": 500, "bots": [
                {"handle": "ann", "harness": "claude", "status": "running in x", "serves": "/pool", "pool": True},
                {"handle": "bob", "harness": "codex", "status": "waiting", "serves": "/pool", "pool": True},
                {"handle": "cat", "harness": "claude", "status": "stopped", "serves": "/pool", "pool": True}]}).raise_for_status()
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        self.page = browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda err: self.errors.append(str(err)))

        def serve(route):
            r = self.client.request(route.request.method, route.request.url.replace("http://board.test", ""),
                                    content=route.request.post_data, headers=route.request.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        self.page.route("**/*", serve)
        self.page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": token})) + ")")

    def go(self, hash_=""):
        self.page.goto("http://board.test/fleet" + hash_)

    def tab(self):
        return self.page.eval_on_selector_all(".tab-pane.on", "ps => ps.map(p => p.id)")

    def rows(self):
        return self.page.eval_on_selector_all("#tab-bots tr.bot-row", "rs => rs.filter(r => r.offsetParent).map(r => r.id)")

    def test_the_hash_picks_the_tab_and_a_reload_keeps_it(self):
        self.go()
        self.assertEqual(self.tab(), ["tab-bots"])
        self.page.click("a[href='#tab-roles']")
        self.page.wait_for_selector("#tab-roles.on")
        self.assertEqual(self.tab(), ["tab-roles"])
        self.page.reload()
        self.assertEqual(self.tab(), ["tab-roles"])
        for link, pane, target in (("#box-box", "tab-machines", "#box-box"), ("#secrets", "tab-settings", "#secrets"),
                                   ("#fleet-add-machine", "tab-settings", "#fleet-add-machine"),
                                   ("#fleet-skills", "tab-roles", "#fleet-skills")):
            with self.subTest(link):
                self.go(link)
                self.page.reload()
                self.assertEqual(self.tab(), [pane])
                self.assertTrue(self.page.is_visible(target), "a folded list opens for the link")
        self.assertEqual(self.errors, [])

    def test_a_bots_row_opens_its_drawer_and_a_second_click_closes_it(self):
        self.go()
        self.assertFalse(self.page.is_visible("#drawer-ann"))
        self.assertEqual(self.page.inner_text("#bot-bob td[data-label='harness']").strip(), "codex", "the harness is on the row")
        self.page.click("#bot-ann td.doing")
        self.page.wait_for_selector("#drawer-ann button[data-handle='ann']")
        self.assertIn("#bot-ann", self.page.url)
        self.assertTrue(self.page.is_visible("#drawer-ann .d-acts button[data-handle='ann'][data-run='0']"), "stop")
        self.assertTrue(self.page.is_visible("#drawer-ann .d-danger button.delete-bot"), "delete, set apart")
        self.assertEqual(self.page.get_attribute("#bot-ann button.open-bot", "aria-expanded"), "true")
        self.page.click("#bot-bob td.doing")
        self.page.wait_for_selector("#drawer-bob .d-acts button[data-handle='bob'][data-run='0']")
        self.assertFalse(self.page.is_visible("#drawer-ann"), "one drawer at a time")
        self.page.reload()
        self.assertTrue(self.page.is_visible("#drawer-bob button[data-handle='bob'][data-run='0']"), "a reload keeps the bot open")
        self.page.click("#bot-bob td.doing")
        self.page.wait_for_selector("#drawer-bob", state="hidden")
        self.assertNotIn("#bot-bob", self.page.url)
        self.assertEqual(self.errors, [])

    def test_a_bot_two_boxes_report_has_one_row_and_its_drawer_opens(self):
        # #362: a stopped copy left on another box gave two rows one id, and no drawer opened
        keeper = self.client.post("/api/agents", json={"handle": "keeper-old", "kind": "keeper"}).json()["token"]
        self.client.put("/api/fleet/hosts/old", headers={"Authorization": "Bearer " + keeper}, json={
            "load": 1, "cores": 4, "mem_total_mb": 1000, "mem_free_mb": 500, "bots": [
                {"handle": "ann", "harness": "claude", "status": "stopped", "serves": "/old", "pool": True},
                {"handle": "cat", "harness": "claude", "status": "stopped", "serves": "/old", "pool": True}]}).raise_for_status()
        self.go()
        count = lambda i: self.page.evaluate("i => document.querySelectorAll('[id=\"' + i + '\"]').length", i)
        self.assertEqual((count("bot-ann"), count("drawer-ann")), (1, 1), "a stopped copy of a running bot")
        self.assertEqual((count("bot-cat"), count("drawer-cat")), (1, 1), "stopped on both")
        self.page.click("#bot-ann button.open-bot")
        self.assertTrue(self.page.is_visible("#drawer-ann"))
        self.page.click(".bot-filter[data-filter=stopped]")
        self.page.click("#bot-cat button.open-bot")
        self.assertTrue(self.page.is_visible("#drawer-cat"))
        self.assertEqual(self.errors, [])

    def test_filters_narrow_the_rows_and_a_link_to_a_stopped_bot_shows_it(self):
        self.go()
        self.assertEqual(self.rows(), ["bot-ann", "bot-bob"])
        self.page.click("button.bot-filter[data-filter='working']")
        self.assertEqual(self.rows(), ["bot-ann"])
        self.page.reload()
        self.assertEqual(self.rows(), ["bot-ann"], "a reload keeps the filter")
        self.page.click("button.bot-filter[data-filter='stopped']")
        self.assertEqual(self.rows(), ["bot-cat"])
        self.page.click("button.bot-filter[data-filter='running']")
        self.go("#bot-cat")
        self.page.reload()
        self.assertEqual(self.rows(), ["bot-cat"])
        self.assertTrue(self.page.is_visible("#drawer-cat button[data-handle='cat'][data-run='1']"), "start")
        self.assertEqual(self.errors, [])

    def test_a_tab_link_leaves_the_tabs_in_view_and_a_phone_opens_the_drawer_under_the_row(self):
        self.go("#tab-settings")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        self.page.click("a[href='#tab-roles']")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        self.page.set_viewport_size({"width": 420, "height": 800})
        self.go("#bot-ann")
        self.page.reload()
        self.assertFalse(self.page.is_visible("#drawer-bob"))
        self.assertTrue(self.page.is_visible("#drawer-ann button[data-handle='ann'][data-run='0']"))
        self.assertGreater(self.page.eval_on_selector("#drawer-ann", "d => d.getBoundingClientRect().top"),
                           self.page.eval_on_selector("#bot-ann", "r => r.getBoundingClientRect().top"), "the drawer is under its row")
        self.assertEqual(self.errors, [])

    def hold_a_thread(self, handle="ann"):
        """Report the bot as holding an open thread, so stop asks about it."""
        b = self.client.post("/api/agents", json={"handle": handle, "kind": "claude-code"}).json()
        h = {"Authorization": "Bearer " + b["token"]}
        tid = self.client.post("/api/messages", headers=h, json={"channel": "general", "body": "work"}).json()["message"]["id"]
        self.client.post(f"/api/threads/{tid}/worker", headers=h).raise_for_status()

    def test_the_keyboard_reaches_a_bots_actions(self):
        self.go()
        self.page.focus("#bot-bob button.open-bot")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#drawer-bob button[data-handle='bob']")
        self.assertTrue(self.page.evaluate("document.activeElement.closest('#drawer-bob') !== null"), "focus goes to the drawer")
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#drawer-bob", state="hidden")
        self.assertEqual(self.page.evaluate("document.activeElement.getAttribute('aria-label')"), "Actions for @bob", "focus comes back")
        self.assertEqual(self.errors, [])

    def test_a_link_changes_the_filters_that_would_hide_what_it_names(self):
        self.go()
        for stored in ("stopped", "working"):
            with self.subTest(stored):
                self.page.evaluate(f"localStorage.setItem('bb.fleetFilter', '{stored}')")
                self.go("#bot-bob")
                self.page.reload()
                self.assertIn("bot-bob", self.rows())
                self.assertEqual(self.page.eval_on_selector("#bot-bob", "r => r.classList.contains('sel')"), True)
        self.page.evaluate("localStorage.setItem('bb.fleetFilter', 'running')")
        self.go("#fleet-stopped")
        self.page.reload()
        self.assertEqual(self.rows(), ["bot-cat"])
        self.assertEqual(self.errors, [])

    def test_a_bots_voice_is_set_when_adding_it_and_edited_in_its_drawer(self):
        # #348: two full-width fields in the form; shown in the drawer, Edit turns them into fields
        calls = []
        self.page.on("request", lambda q: calls.append((q.method, q.url, q.post_data)) if "/api/fleet/bots" in q.url else None)
        self.go()
        self.page.click("button.addbot-btn")
        self.assertTrue(self.page.is_visible("form.newbot-top textarea[name=voice]"))
        self.assertTrue(self.page.is_visible("form.newbot-top textarea[name=personality]"))
        self.page.fill("form.newbot-top input[name=handle]", "dee")
        self.page.fill("form.newbot-top textarea[name=voice]", " Plain. ")
        self.page.fill("form.newbot-top textarea[name=personality]", "<b>Dry</b>")
        with self.page.expect_navigation():
            self.page.click("form.newbot-top button.addbot-go")
        sent = json.loads(calls[-1][2])
        self.assertEqual((sent["voice"], sent["personality"]), ("Plain.", "<b>Dry</b>"))

        self.page.click("#bot-ann button.open-bot")
        self.assertEqual(self.page.locator("#drawer-ann .d-voice").count(), 0, "no fleet row, nowhere to store them")
        self.page.click("#bot-dee button.open-bot")
        box = "#drawer-dee .d-voice"
        self.assertEqual(self.page.inner_text(box + " .quote:first-child span"), "Plain.")
        self.assertEqual(self.page.inner_text(box + " .quote:last-child span"), "<b>Dry</b>", "shown as text")
        self.assertIn("new session", self.page.inner_text(box + " p"))

        self.page.click(box + " button.voice-edit")
        self.assertEqual(self.page.input_value(box + " textarea[name=personality]"), "<b>Dry</b>")
        self.page.click(box + " button.voice-cancel")
        self.assertFalse(self.page.is_visible(box + " textarea"))
        self.assertTrue(self.page.is_visible(box + " .d-voice-two"))

        self.page.click(box + " button.voice-edit")
        self.page.fill(box + " textarea[name=voice]", "Warm.")
        self.page.fill(box + " textarea[name=personality]", "")
        with self.page.expect_navigation():
            self.page.click(box + " button[type=submit]")
        self.assertEqual(calls[-1][:1], ("PUT",))
        self.assertTrue(calls[-1][1].endswith("/api/fleet/bots/dee/voice"))
        self.assertTrue(self.page.is_visible(box), "the drawer stays open across the reload")
        self.assertEqual(self.page.inner_text(box + " .quote:first-child span"), "Warm.")
        self.assertTrue(self.page.inner_text(box + " .quote:last-child span").startswith("fleet default, until set:"))   # #381
    def test_a_bot_live_on_two_boxes_has_two_rows_and_each_opens_its_own_drawer(self):
        # #364: both rows are shown, as a real problem to see; they used to share one id and one drawer
        self.hold_a_thread()
        keeper = self.client.post("/api/agents", json={"handle": "keeper-old", "kind": "keeper"}).json()["token"]
        self.client.put("/api/fleet/hosts/old", headers={"Authorization": "Bearer " + keeper}, json={
            "load": 1, "cores": 4, "mem_total_mb": 1000, "mem_free_mb": 500, "bots": [
                {"handle": "ann", "harness": "claude", "status": "waiting", "serves": "/old", "pool": True}]}).raise_for_status()
        self.go()
        ids = self.page.eval_on_selector_all("[id]", "els => els.map(x => x.id)")
        self.assertEqual(len(ids), len(set(ids)), "no id twice")
        rows = self.page.eval_on_selector_all("#fleet-bots tr.bot-row", "rs => rs.map(r => r.id).filter(i => i.startsWith('bot-ann'))")
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(row):
                other = rows[1 - rows.index(row)]
                drawer = 'tr[id="drawer-%s"]' % row[4:]
                self.page.click('tr[id="%s"] button.open-bot' % row)
                self.assertTrue(self.page.is_visible(drawer))
                self.assertFalse(self.page.is_visible('tr[id="drawer-%s"]' % other[4:]))
                self.page.reload()
                self.assertTrue(self.page.is_visible(drawer), "a reload lands on the row that was opened")
                self.page.click(drawer + " button[data-handle='ann'][data-run='0'][data-open]")
                self.assertTrue(self.page.is_visible(drawer + " .stop-panel"), "its own stop question")
                self.assertEqual(self.page.get_attribute(drawer + " .stop-go", "data-host"),
                                 self.page.inner_text('tr[id="%s"] td[data-label="machine"]' % row).strip())
                self.page.click(drawer + " .stop-cancel")
                self.page.click('tr[id="%s"] button.open-bot' % row)
        self.assertEqual(self.errors, [])

    def test_the_stop_question_opens_in_the_drawer_on_any_screen(self):
        self.hold_a_thread()
        for size in ({"width": 1280, "height": 760}, {"width": 420, "height": 800}):
            with self.subTest(size):
                self.page.set_viewport_size(size)
                self.go("#bot-ann")
                self.page.reload()
                self.page.click("#drawer-ann button[data-handle='ann'][data-run='0'][data-open]")
                self.assertTrue(self.page.is_visible("#drawer-ann #stop-ann input[value='handoff']"))
                box = self.page.locator("#stop-ann .stop-go").bounding_box()
                self.assertLess(box["y"] + box["height"], size["height"], "the question is on screen")
                self.page.click("#stop-ann .stop-cancel")
                self.assertFalse(self.page.is_visible("#stop-ann"))
                self.page.click("button.bot-filter[data-filter='stopped']")      # the row goes, and its drawer with it
                self.assertFalse(self.page.is_visible("#drawer-ann"))
                self.page.click("button.bot-filter[data-filter='running']")
                self.assertTrue(self.page.is_visible("#drawer-ann"), "and comes back with it")
        self.assertEqual(self.errors, [])

    def test_delete_on_a_dark_boxs_row_names_the_other_box_and_both_are_asked(self):
        # #366: it reads as removing the stale copy, and it deletes the bot on every box (#368)
        keeper = self.client.post("/api/agents", json={"handle": "keeper-old", "kind": "keeper"}).json()["token"]
        self.client.put("/api/fleet/hosts/old", headers={"Authorization": "Bearer " + keeper}, json={
            "load": 1, "cores": 4, "mem_total_mb": 1000, "mem_free_mb": 500, "bots": [
                {"handle": "ann", "harness": "claude", "status": "waiting", "serves": "/old", "pool": True}]}).raise_for_status()
        with db.conn() as c:
            c.execute("UPDATE fleet_hosts SET reported_at='2000-01-01T00:00:00+00:00' WHERE name='old'")
        self.go()
        said = []
        self.page.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
        click = lambda sel: self.page.eval_on_selector(sel, "b => b.click()")
        click("button.delete-bot[data-handle=ann][data-also^='It is also listed on box']")     # the row on the dark box
        self.assertIn("It is also listed on box, and this deletes it there too; a box not reporting does that when it returns.", said[-1])
        click("button.delete-bot[data-handle=ann][data-also^='It is also listed on old']")
        self.assertIn("It is also listed on old, and this deletes it there too", said[-1])
        click("button.delete-bot[data-handle=cat]")
        self.assertNotIn("also listed", said[-1])
        self.assertEqual(self.errors, [])
        # what it says is what a delete does: both boxes are asked
        self.client.post("/api/agents", json={"handle": "ann", "kind": "claude-code"}).raise_for_status()
        self.assertFalse(fleet.delete("ann", {"handle": "jac"}))
        asked = [[w["handle"] for w in fleet.wanted(box) + fleet.leftovers(box) if w.get("deleted")] for box in ("box", "old")]
        self.assertEqual(asked, [["ann"], ["ann"]])


if __name__ == "__main__":
    unittest.main()
