"""The board page: a workspace folds completely, unread or not (#373), and a thread with no replies
can be replied to from the channel (#372). Needs Playwright and Chromium; skipped without them.
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


class BoardRail(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install Playwright and Chromium for board page checks")
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
        for ws in ("aaa", "bbb"):
            self.client.post("/api/workspaces", headers=self.human, json={"slug": ws, "name": ws, "purpose": "x"}).raise_for_status()
            self.root = self.client.post("/api/messages", headers=self.human, json={"channel": ws, "body": "forgot something"}).json()["message"]["id"]
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        self.page = browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda err: self.errors.append(str(err)))

        def serve(route):
            path = route.request.url.replace("http://board.test", "")
            if "wait=" in path:
                return route.abort()
            r = self.client.request(route.request.method, path, content=route.request.post_data, headers=route.request.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        self.page.route("**/*", serve)
        self.page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": self.token})) + ");"
                                  "localStorage.setItem('bb.seen', '{\"aaa\":0,\"bbb\":0}')")

    def rail(self):
        return self.page.eval_on_selector_all("#channels li.sub", "ls => ls.map(l => l.querySelector('.name').title)")

    def test_a_folded_workspace_shows_only_its_unread_count(self):
        self.page.goto("http://board.test/#bbb")
        self.page.wait_for_selector("#channels li.ws")
        self.assertEqual(self.rail(), ["#bbb"], "the open channel stays; unread in the other workspace does not")
        self.assertEqual(self.page.inner_text("#channels li.ws[data-proj=aaa] .badge.sum"), "1")
        self.page.click("#channels li.ws[data-proj=aaa] button.fold")
        self.assertEqual(sorted(self.rail()), ["#aaa", "#bbb"], "opened, it lists its channels")
        self.page.click("#channels li.ws[data-proj=aaa] button.fold")
        self.assertEqual(self.rail(), ["#bbb"], "and folds again, unread or not")
        self.assertEqual(self.errors, [])

    def test_a_thread_with_no_replies_can_be_replied_to(self):
        self.page.goto("http://board.test/#bbb")
        button = f"#m{self.root} button.replies.none"
        self.page.wait_for_selector(button, state="attached")
        self.page.hover(f"#m{self.root}")
        self.page.click(button)
        self.page.wait_for_selector(f"#t{self.root}")
        self.page.fill("#thread textarea", "and one more thing")
        self.page.click("#thread button.send")
        self.page.wait_for_function("id => document.querySelector('#m' + id + ' button.replies .n').textContent === '1 reply'", arg=self.root)
        self.assertEqual(self.client.get(f"/api/threads/{self.root}").json()["messages"][-1]["body"], "and one more thing")
        self.assertEqual(self.page.locator(f"#t{self.root} button.replies").count(), 0, "no button inside the thread itself")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
