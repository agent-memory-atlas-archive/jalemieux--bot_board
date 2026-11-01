"""/fleet: each section folds from its heading and stays folded in this browser; Needs you opens
again when something new is in it (#281). Needs Playwright and Chromium; skipped without them.
Run with python -m unittest discover -s tests."""
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
from app import db, main, web                    # noqa: E402

SECTIONS = ("fleet-attention", "fleet-versions")


class FleetFold(unittest.TestCase):
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
        for box in ("box", "gone"):
            self.report(box)
        self.dark("gone")       # one alert: a box that stopped reporting
        pw = self.stack.enter_context(sync_playwright())
        self.browser = pw.chromium.launch()
        self.stack.callback(self.browser.close)
        self.page = self.browser.new_page()

        def serve(route):
            r = self.client.request(route.request.method, route.request.url.replace("http://board.test", ""),
                                    content=route.request.post_data, headers=route.request.headers)
            route.fulfill(status=r.status_code, body=r.content, content_type=r.headers.get("content-type", "text/plain"))
        self.page.route("**/*", serve)
        self.page.goto("http://board.test/fleet")

    def report(self, box):
        r = self.client.post("/api/agents", json={"handle": f"keeper-{box}", "kind": "keeper"})
        self.client.put(f"/api/fleet/hosts/{box}", headers={"Authorization": "Bearer " + r.json()["token"]},
                        json={"bots": [{"handle": f"ann-{box}", "harness": "claude", "status": "waiting",
                                        "serves": "/pool", "pool": True}]}).raise_for_status()

    def dark(self, box):
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE fleet_hosts SET reported_at='2020-01-01T00:00:00+00:00' WHERE name=?", (box,))

    def settle(self):
        self.page.wait_for_timeout(100)     # a details' toggle event comes after the click

    def open(self):
        return {s: self.page.eval_on_selector(f"#{s}", "d => d.open") for s in SECTIONS}

    def test_needs_you_folds_from_its_heading_and_stays_folded_and_versions_starts_folded(self):
        with patch.object(web.versions_mod, "rows", lambda *a, **k: []):
            self.page.reload()
            self.assertEqual(self.open(), {"fleet-attention": True, "fleet-versions": False})
        self.page.focus("#fleet-attention > summary")
        self.page.keyboard.press("Enter")       # the keyboard folds it too
        self.settle()
        self.page.reload()
        self.assertFalse(self.open()["fleet-attention"])
        self.assertTrue(self.page.is_visible("#fleet-attention > summary"), "a folded section keeps its heading and count")
        self.page.click("#fleet-attention > summary")
        self.settle()
        self.page.reload()
        self.assertTrue(self.open()["fleet-attention"])

    def test_versions_opens_when_a_person_must_act_and_stays_folded_until_the_ask_changes(self):
        ask = lambda n: [{"what": "routine", "name": f"r{i}", "running": "recipe v1", "wanted": "recipe v2", "current": False,
                          "note": "an older recipe: a new version needs a human's yes", "href": "", "bell": False} for i in range(n)]
        with patch.object(web.versions_mod, "rows", lambda *a, **k: ask(1)):
            self.page.reload()
            self.assertTrue(self.open()["fleet-versions"], "a remembered fold from before does not hide an ask")
            self.page.evaluate("document.querySelector('#fleet-versions > summary').click()")
            self.settle()
            self.page.reload()
            self.assertFalse(self.open()["fleet-versions"], "the same ask: it stays folded")
        with patch.object(web.versions_mod, "rows", lambda *a, **k: ask(2)):
            self.page.reload()
            self.assertTrue(self.open()["fleet-versions"], "a new ask opens it")
        keeper = {"what": "keeper", "name": "box", "running": "a", "wanted": "b", "current": False, "note": "stuck", "href": "", "bell": True}
        with patch.object(web.versions_mod, "rows", lambda *a, **k: [keeper]):
            self.page.reload()
            self.assertTrue(self.open()["fleet-versions"], "a keeper only a person can fix counts too")
            self.assertIn("1 needs you", self.page.inner_text("#fleet-versions > summary").lower())

    def test_needs_you_opens_again_when_something_new_is_in_it(self):
        self.page.click("#fleet-attention > summary")
        self.settle()
        self.page.reload()
        self.assertFalse(self.open()["fleet-attention"], "the same alerts: it stays folded")
        self.dark("box")        # a second alert
        self.page.reload()
        self.assertTrue(self.open()["fleet-attention"])
        self.assertIn("NEEDS YOU · 2", self.page.inner_text("#fleet-attention > summary"))


if __name__ == "__main__":
    unittest.main()
