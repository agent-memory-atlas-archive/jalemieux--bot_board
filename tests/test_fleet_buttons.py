"""/fleet: a bot row's delete and channels buttons do what they say, and only that (#314): they share
.act with start and stop, whose handler used to fire too, sending a start/stop with no box.
Needs Playwright and Chromium; skipped without them. Run with python -m unittest discover -s tests."""
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


class FleetButtons(unittest.TestCase):
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
        self.client.post("/api/workspaces", headers={"Authorization": "Bearer " + token},
                         json={"slug": "proj", "name": "Proj", "purpose": "x"})
        self.client.post("/api/agents", json={"handle": "tarzan", "kind": "claude-code", "workspace": "proj"})
        self.client.put("/api/fleet/hosts/box", headers={"Authorization": "Bearer " + keeper}, json={"bots": [
            {"handle": "tarzan", "harness": "claude", "status": "waiting", "serves": "/pool", "pool": True}]}).raise_for_status()
        self.client.put("/api/fleet/bots/tarzan", headers={"Authorization": "Bearer " + token},
                        json={"host": "box", "run": True}).raise_for_status()
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        self.page = browser.new_page()
        self.calls, self.alerts = [], []

        def serve(route):
            q = route.request
            path = q.url.replace("http://board.test", "")
            r = self.client.request(q.method, path, content=q.post_data, headers=q.headers)
            if q.method != "GET":
                self.calls.append((q.method, path, r.status_code))
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))

        def dialog(d):
            if d.type == "alert":
                self.alerts.append(d.message)
            d.accept(self.answer) if d.type == "prompt" else d.accept()
        self.answer = ""
        self.page.route("**/*", serve)
        self.page.on("dialog", dialog)
        self.page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": token})) + ")")
        self.page.goto("http://board.test/fleet")

    def click(self, selector):
        self.page.click("tr.bot-row td.doing")      # a bot's actions are in its side panel
        self.page.wait_for_selector(".bot-panel button")
        self.page.evaluate("document.querySelectorAll('.p-more').forEach(d => d.open = true)")
        self.page.click(".bot-panel " + selector)
        self.page.wait_for_timeout(300)

    def test_delete_only_deletes(self):
        self.click("button.delete-bot")
        self.assertEqual(self.calls, [("DELETE", "/api/agents/tarzan", 200)])
        self.assertEqual(self.alerts, [])

    def test_channels_only_binds_channels(self):
        self.click("button.chan-edit")
        self.assertEqual(self.calls, [("PUT", "/api/fleet/bots/tarzan/channels", 200)])
        self.assertEqual(self.alerts, [])

    def test_stop_still_stops(self):
        self.click("button.act[data-run='0']:not([data-kill])")
        self.assertEqual(self.calls, [("PUT", "/api/fleet/bots/tarzan", 200)])
        self.assertEqual(self.alerts, [])


if __name__ == "__main__":
    unittest.main()
