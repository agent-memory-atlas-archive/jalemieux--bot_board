"""A human collapses a thread (#374, #442): out of the unread counts for every human, one line in the
channel stream with a chevron to expand it (#407), still in search and at its link, unchanged for bots.
Run with python -m unittest discover -s tests."""
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


class HideThread(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "DB_PATH", os.path.join(tmp, "board.db")))
        self.stack.enter_context(patch.object(db, "_local", threading.local()))
        self.stack.enter_context(patch.object(main, "READ_TOKEN_REQUIRED", False))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.addCleanup(lambda: hasattr(db._local, "conn") and db._local.conn.close())
        self.token = self.client.post("/api/agents", json={"handle": "jac", "kind": "human"}).json()["token"]
        self.human = {"Authorization": "Bearer " + self.token}
        self.client.post("/api/workspaces", headers=self.human, json={"slug": "proj", "name": "proj", "purpose": "x"}).raise_for_status()
        bot = self.client.post("/api/agents", json={"handle": "tarzan", "kind": "claude-code", "workspace": "proj"})
        self.bot = {"Authorization": "Bearer " + bot.json()["token"]}
        self.keep = self.post("stays")
        self.root = self.post("needle in a hidden thread")
        self.reply = self.post("a reply", self.root)

    def post(self, body, reply_to=None):
        r = self.client.post("/api/messages", headers=self.human, json={"channel": "proj", "body": body, "reply_to": reply_to})
        return r.json()["message"]["id"]

    def listed(self, **params):
        r = self.client.get("/api/messages", params={"channel": "proj", "hide_events": "true", **params})
        return [m["id"] for m in r.json()["messages"]]

    def count(self):
        boards = self.client.get("/api/channels", params={"hide_events": "true", "seen": f"proj:{self.keep}"}, headers=self.human).json()["channels"]
        return next(b["unread"] for b in boards if b["slug"] == "proj")

    def test_hidden_from_the_views_kept_elsewhere_and_brought_back(self):
        self.assertEqual(self.count(), 2)
        url = f"/api/threads/{self.reply}/hidden"     # any message of the thread names it
        self.assertEqual(self.client.put(url, headers=self.bot).status_code, 403, "humans only")
        self.assertEqual(self.client.put("/api/threads/99999/hidden", headers=self.human).status_code, 404)
        self.assertEqual(self.client.put(url, headers=self.human).json(), {"thread_id": self.root, "hidden": True})
        self.client.put(url, headers=self.human).raise_for_status()      # twice is fine

        # the stream's list of roots keeps it, marked, so the page folds it in place (#407)
        self.assertEqual(self.listed(roots="true"), [self.keep, self.root])
        roots = {m["id"]: m for m in self.client.get("/api/messages", params={"channel": "proj", "hide_events": "true", "roots": "true"}).json()["messages"]}
        self.assertEqual((roots[self.root]["hidden"], roots[self.root]["hidden_by"]), (True, "jac"))
        self.assertRegex(roots[self.root]["hidden_at"], r"^\d{4}-")
        self.assertNotIn("hidden", roots[self.keep])
        self.assertEqual(self.listed(since=self.keep), [], "the live poll and the unread view")
        self.assertEqual(self.count(), 0)
        thread = self.client.get(f"/api/threads/{self.root}", params={"hide_events": "true"}).json()
        self.assertTrue(thread["hidden"])
        self.assertEqual([m["id"] for m in thread["messages"]], [self.root, self.reply], "still at its link")
        self.assertEqual([m["id"] for m in self.client.get("/api/search", params={"q": "needle"}, headers=self.human).json()["messages"]], [self.root])
        self.assertIn(self.root, [m["id"] for m in self.client.get("/api/messages", params={"channel": "proj"}, headers=self.bot).json()["messages"]], "bots still see it")

        self.client.delete(url, headers=self.human).raise_for_status()
        self.assertIn(self.root, self.listed(roots="true"))
        self.assertNotIn("hidden", {m["id"]: m for m in self.client.get("/api/messages", params={"channel": "proj", "hide_events": "true", "roots": "true"}).json()["messages"]}[self.root])
        self.assertFalse(self.client.get(f"/api/threads/{self.root}").json()["hidden"])
        self.assertEqual(self.count(), 2)

    def test_the_thread_panel_collapses_it_and_expands_it(self):
        page = self.page()
        page.goto("http://board.test/#proj")
        page.click(f"#m{self.root} button.replies")
        page.wait_for_selector("#thide:visible")
        self.assertEqual(page.inner_text("#thide"), "collapse")
        page.click("#thide")
        # one line in its place (#442); the thread panel stays open, its button now saying expand
        page.wait_for_selector("#stream button.tcollapsed")
        line = page.locator("#stream button.tcollapsed")
        self.assertIn(f"#{self.root} needle in a hidden thread · 1 reply", line.inner_text())
        self.assertIn("@jac", line.inner_text())
        self.assertTrue(page.locator(f".msg#m{self.keep}").is_visible())
        self.assertEqual(page.locator(f".msg#m{self.root}").count(), 0)
        page.wait_for_function("document.querySelector('#thide').textContent === 'expand'")
        self.assertTrue(db.thread_hidden(self.root))
        # a reload shows it collapsed the same way, from the board's mark
        page.goto("http://board.test/#proj")
        page.wait_for_selector("#stream button.tcollapsed")
        self.assertEqual(page.locator(f".msg#m{self.root}").count(), 0)

        page.goto(f"http://board.test/t/{self.root}")
        page.wait_for_selector("#thide:visible")
        self.assertEqual(page.inner_text("#thide"), "expand")
        # the live poll leaves it out, so an open collapsed thread fetches its own replies
        later = self.client.post("/api/messages", headers=self.bot, json={"channel": "proj", "body": "a late reply", "reply_to": self.root}).json()["message"]["id"]
        page.wait_for_selector(f"#t{later}", timeout=8000)
        self.assertEqual(page.inner_text("#tcount"), "2 replies")
        page.click("#thide")         # expands in place: no confirm, no reload
        page.wait_for_function("document.querySelector('#thide').textContent === 'collapse'")
        self.assertFalse(db.thread_hidden(self.root))
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.errors, [])

    def test_the_chevron_on_a_thread_collapses_and_expands_it(self):
        page = self.page()
        page.goto("http://board.test/#proj")
        chevron = page.locator(f".msg#m{self.root} [data-collapse]")
        self.assertTrue(chevron.is_visible(), "on the header, without hovering")
        self.assertEqual(chevron.get_attribute("aria-expanded"), "true")
        self.assertEqual(page.locator("[data-hide], .shide").count(), 0, "no hide button any more")
        chevron.click()
        page.wait_for_selector("#stream button.tcollapsed")    # in place, no confirm; no thread opens
        self.assertTrue(db.thread_hidden(self.root))
        self.assertEqual(page.locator(f".msg#m{self.root}").count(), 0)
        self.assertTrue(page.is_visible(f".msg#m{self.keep}"))
        self.assertEqual(page.locator("#app.thread-closed").count(), 1)
        line = page.locator(f"#stream button.tcollapsed[data-expand='{self.root}']")
        self.assertEqual(line.get_attribute("aria-expanded"), "false")
        line.click()
        page.wait_for_selector(f".msg#m{self.root} [data-collapse]")
        self.assertFalse(db.thread_hidden(self.root))
        self.assertEqual(page.locator("#stream button.tcollapsed").count(), 0)
        self.assertEqual(self.dialogs, [])
        # the @board toggle sits in the top bar, without opening a thread
        self.assertEqual(page.inner_text("#navboard"), "@board: shown")
        page.click("#navboard")
        self.assertEqual(page.inner_text("#navboard"), "@board: folded")
        self.assertEqual(page.evaluate("localStorage.getItem('bb.boardMsgs')"), '"fold"')
        self.assertEqual(self.errors, [])

    def test_consecutive_collapsed_threads_each_keep_their_line(self):
        other = self.post("another thread")
        for tid in (self.root, other):
            self.client.put(f"/api/threads/{tid}/hidden", headers=self.human).raise_for_status()
        page = self.page()
        page.goto("http://board.test/#proj")
        page.wait_for_selector("#stream button.tcollapsed")
        self.assertEqual(page.locator("#stream button.tcollapsed").count(), 2)
        page.click(f"[data-expand='{other}']")
        page.wait_for_selector(f".msg#m{other}")
        self.assertTrue(db.thread_hidden(self.root))
        self.assertEqual(self.errors, [])

    def page(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for board page checks")
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        page = browser.new_page()
        self.errors, self.dialogs = [], []
        page.on("pageerror", lambda err: self.errors.append(str(err)))
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))

        def serve(route):
            path = route.request.url.replace("http://board.test", "")
            if "wait=" in path:
                return route.abort()
            r = self.client.request(route.request.method, path, content=route.request.post_data, headers=route.request.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        page.route("**/*", serve)
        page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": self.token})) + ")")
        return page


if __name__ == "__main__":
    unittest.main()
