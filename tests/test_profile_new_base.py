"""/fleet/profiles/new: a base skill is shown ticked and fixed, survives a switch of starter, and is
never posted in the profile's skills (#396). Needs Playwright and Chromium; skipped without them.
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


class NewProfileBaseSkills(unittest.TestCase):
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
        self.human = {"Authorization": "Bearer " + token}
        self.client.put("/api/skills/house-style", headers=self.human, json={
            "body": "Short.", "base_revision": 0, "fields": {"description": "How to write.", "base": True}}).raise_for_status()
        pw = self.stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        self.stack.callback(browser.close)
        self.page = browser.new_page()
        self.posted = []

        def serve(route):
            q = route.request
            if q.method == "POST":
                self.posted.append(json.loads(q.post_data))
            r = self.client.request(q.method, q.url.replace("http://board.test", ""), content=q.post_data, headers=q.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        self.page.route("**/*", serve)
        self.page.add_init_script("localStorage.setItem('bb.me', " + json.dumps(json.dumps({"handle": "jac", "token": token})) + ")")

    def test_base_skill_is_fixed_and_not_sent(self):
        self.page.goto("http://board.test/fleet/profiles/new")
        base = self.page.locator('input[name=skill][value="house-style"]')
        self.assertTrue(base.is_checked() and base.is_disabled())
        self.page.click('#starters button[data-key=""]')            # a blank starter unticks the rest, not base
        self.assertTrue(base.is_checked())
        self.assertTrue(self.page.locator('input[name=mandatory][value="house-style"]').is_checked())
        self.assertIn("- **house-style** (mandatory, every bot)", self.page.text_content("#preview"))
        self.page.fill("input[name=name]", "scribe")
        self.page.fill("input[name=role]", "Writes.")
        self.page.check('input[name=skill][value="github-lifecycle"]')
        with self.page.expect_navigation():
            self.page.click("#create-go")
        self.assertEqual(self.posted[0]["skills"], [{"name": "github-lifecycle", "mandatory": False}])
        self.assertIn("Base skills, every bot has them", self.page.text_content("#base-skills"))


if __name__ == "__main__":
    unittest.main()
