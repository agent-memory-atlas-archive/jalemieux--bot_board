"""/fleet in tabs: the hash picks the tab, so older links land where they point; a bot's actions
are in its side panel; the filters narrow the rows. Needs Playwright and Chromium; skipped
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
from app import db, main                         # noqa: E402


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

    def test_a_bots_actions_move_to_its_panel_and_back(self):
        self.go()
        self.assertFalse(self.page.is_visible("#bot-panel"))
        self.page.click("#bot-ann td.doing")
        self.page.wait_for_selector("#bot-panel button[data-handle='ann']")
        self.assertIn("#bot-ann", self.page.url)
        self.assertTrue(self.page.is_visible("#bot-panel .p-acts button[data-handle='ann'][data-run='0']"), "stop")
        self.assertFalse(self.page.is_visible("#bot-panel .p-danger button.delete-bot"), "behind a click")
        self.page.click("#bot-panel .p-more summary")
        self.assertTrue(self.page.is_visible("#bot-panel .p-danger button.delete-bot"))
        self.page.click("#bot-bob td.doing")
        self.page.wait_for_selector("#bot-panel .p-acts button[data-handle='bob'][data-run='0']")
        self.assertEqual(self.page.locator("#bot-ann td.acts button[data-run='0']").count(), 2, "stop and kill are back on the row")
        self.page.reload()
        self.assertTrue(self.page.is_visible("#bot-panel button[data-handle='bob'][data-run='0']"), "a reload keeps the bot open")
        self.page.click("#bot-panel .panel-close")
        self.page.wait_for_selector("#bot-panel", state="hidden")
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
        self.assertTrue(self.page.is_visible("#bot-panel button[data-handle='cat'][data-run='1']"), "start")
        self.assertEqual(self.errors, [])

    def test_a_tab_link_leaves_the_tabs_in_view_and_a_phone_gets_only_the_panels_actions(self):
        self.go("#tab-settings")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        self.page.click("a[href='#tab-roles']")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        self.page.set_viewport_size({"width": 420, "height": 800})
        self.go("#bot-ann")
        self.page.reload()
        self.assertFalse(self.page.is_visible("#bot-bob td.acts"))
        self.assertTrue(self.page.is_visible("#bot-panel button[data-handle='ann'][data-run='0']"))
        self.assertLess(self.page.eval_on_selector("#bot-panel", "p => p.getBoundingClientRect().top"),
                        self.page.eval_on_selector("#fleet-bots", "t => t.getBoundingClientRect().top"), "the panel comes first")
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
        self.page.wait_for_selector("#bot-panel button[data-handle='bob']")
        self.assertTrue(self.page.evaluate("document.activeElement.closest('#bot-panel') !== null"), "focus goes to the panel")
        self.assertEqual(self.page.locator("#bot-panel .open-bot").count(), 0)
        self.page.click("#bot-panel .panel-close")
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

    def test_the_stop_question_stays_in_view_whatever_the_filter(self):
        self.hold_a_thread()
        for size in ({"width": 1280, "height": 760}, {"width": 420, "height": 800}):
            with self.subTest(size):
                self.page.set_viewport_size(size)
                self.go("#bot-ann")
                self.page.reload()
                self.page.click("button.bot-filter[data-filter='stopped']")      # the row goes, the panel stays
                self.page.click("#bot-panel button[data-handle='ann'][data-run='0'][data-open]")
                self.assertTrue(self.page.is_visible("#bot-panel #stop-ann input[value='handoff']"))
                box = self.page.locator("#stop-ann .stop-go").bounding_box()
                self.assertLess(box["y"] + box["height"], size["height"], "the question is on screen")
                self.page.click("#stop-ann .stop-cancel")
                self.assertFalse(self.page.is_visible("#stop-ann"))
                self.page.click("button.bot-filter[data-filter='running']")
                self.page.click("#bot-bob td.doing")
                self.page.wait_for_selector("#bot-panel button[data-handle='bob']")
                self.assertEqual(self.page.eval_on_selector("#bot-ann + tr.stop-panel", "r => r.id"), "stop-ann", "it goes back after its row")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
