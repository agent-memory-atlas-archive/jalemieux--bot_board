"""A human hides a thread (#374): gone from the channel views for every human, still in search and at
its link, unchanged for bots. Run with python -m unittest discover -s tests."""
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

        self.assertEqual(self.listed(roots="true"), [self.keep])
        self.assertEqual(self.listed(since=self.keep), [], "the live poll and the unread view")
        self.assertEqual(self.count(), 0)
        thread = self.client.get(f"/api/threads/{self.root}", params={"hide_events": "true"}).json()
        self.assertTrue(thread["hidden"])
        self.assertEqual([m["id"] for m in thread["messages"]], [self.root, self.reply], "still at its link")
        self.assertEqual([m["id"] for m in self.client.get("/api/search", params={"q": "needle"}, headers=self.human).json()["messages"]], [self.root])
        self.assertIn(self.root, [m["id"] for m in self.client.get("/api/messages", params={"channel": "proj"}, headers=self.bot).json()["messages"]], "bots still see it")

        self.client.delete(url, headers=self.human).raise_for_status()
        self.assertIn(self.root, self.listed(roots="true"))
        self.assertFalse(self.client.get(f"/api/threads/{self.root}").json()["hidden"])
        self.assertEqual(self.count(), 2)

    def test_the_thread_panel_hides_it_and_brings_it_back(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for board page checks")
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("dialog", lambda d: d.accept())

        def serve(route):
            path = route.request.url.replace("http://board.test", "")
            if "wait=" in path:
                return route.abort()
            r = self.client.request(route.request.method, path, content=route.request.post_data, headers=route.request.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        page.route("**/*", serve)
        page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": self.token})) + ")")
        page.goto("http://board.test/#proj")
        page.click(f"#m{self.root} button.replies")
        page.wait_for_selector("#thide:visible")
        self.assertEqual(page.inner_text("#thide"), "hide")
        page.click("#thide")
        page.wait_for_selector(f"#m{self.root}", state="detached")
        self.assertTrue(page.locator(f"#m{self.keep}").count())
        self.assertTrue(db.thread_hidden(self.root))

        page.goto(f"http://board.test/t/{self.root}")
        page.wait_for_selector("#thide:visible")
        self.assertEqual(page.inner_text("#thide"), "bring back")
        # the live poll leaves it out, so an open hidden thread fetches its own replies
        later = self.client.post("/api/messages", headers=self.bot, json={"channel": "proj", "body": "a late reply", "reply_to": self.root}).json()["message"]["id"]
        page.wait_for_selector(f"#t{later}", timeout=8000)
        self.assertEqual(page.inner_text("#tcount"), "2 replies")
        with page.expect_navigation():
            page.click("#thide")
        self.assertFalse(db.thread_hidden(self.root))
        self.assertEqual(errors, [])

    def test_a_thread_card_in_the_channel_hides_it(self):
        page = self.page()
        page.goto("http://board.test/#proj")
        page.hover(f"#m{self.root}")
        page.click(f"#m{self.root} [data-hide]")
        page.wait_for_selector(f"#m{self.root}", state="detached")
        self.assertTrue(page.locator(f"#m{self.keep}").count())
        self.assertTrue(db.thread_hidden(self.root))
        # the @board toggle sits in the top bar, without opening a thread
        self.assertEqual(page.inner_text("#navboard"), "@board: shown")
        page.click("#navboard")
        self.assertEqual(page.inner_text("#navboard"), "@board: folded")
        self.assertEqual(page.evaluate("localStorage.getItem('bb.boardMsgs')"), '"fold"')
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
        self.errors = []
        page.on("pageerror", lambda err: self.errors.append(str(err)))
        page.on("dialog", lambda d: d.accept())

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
