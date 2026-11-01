"""/fleet: each table sorts by a column from its heading, by value, and keeps the sort through a
reload (#282). Needs Playwright and Chromium; skipped without them.
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
from app import db, main                         # noqa: E402


class FleetSort(unittest.TestCase):
    # when each bot last ran: "3d ago" sorts before "5h ago" by time, not by its text
    RAN = {"zed": "2026-10-28T10:00:00+00:00", "amy": "2026-10-25T10:00:00+00:00",
           "bob": "2026-10-28T12:00:00+00:00", "kit": "2026-10-20T10:00:00+00:00"}

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
        # load per core: 9/8 on big, 2/1 on tiny, 0.5/4 on calm; the names sort the other way round
        self.report("big", 9, 8, ["zed", "amy"])
        self.report("tiny", 2, 1, ["bob"])
        self.report("calm", 0.5, 4, ["kit"])
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

    def report(self, box, load, cores, handles):
        r = self.client.post("/api/agents", json={"handle": f"keeper-{box}", "kind": "keeper"})
        self.client.put(f"/api/fleet/hosts/{box}", headers={"Authorization": "Bearer " + r.json()["token"]},
                        json={"load": load, "cores": cores, "mem_total_mb": 1000, "mem_free_mb": 500,
                              "bots": [{"handle": h, "harness": "claude", "status": "waiting", "serves": "/pool",
                                        "pool": True, "last_run": self.RAN[h]} for h in handles]}).raise_for_status()

    def column(self, table, i):
        return self.page.eval_on_selector_all(f"{table} tbody tr", f"rs => rs.map(r => r.cells[{i}].innerText.trim())")

    def sort_by(self, table, label):
        self.page.click(f"{table} thead button.sort:text-is('{label}')")

    def test_machines_sort_by_load_as_a_number_and_back(self):
        t = "#fleet-machines table"
        self.page.click("a[href='#tab-machines']")
        default = self.column(t, 0)
        self.assertIsNone(self.page.get_attribute(f"{t} thead th:nth-child(4)", "aria-sort"))
        self.sort_by(t, "Load")
        self.assertEqual([m.split("\n")[0] for m in self.column(t, 0)], ["calm", "big", "tiny"])     # 0.125, 1.125, 2
        self.assertEqual(self.page.get_attribute(f"{t} thead th:nth-child(4)", "aria-sort"), "ascending")
        self.sort_by(t, "Load")
        self.assertEqual([m.split("\n")[0] for m in self.column(t, 0)], ["tiny", "big", "calm"])
        self.assertEqual(self.page.get_attribute(f"{t} thead th:nth-child(4)", "aria-sort"), "descending")
        self.page.reload()
        self.assertEqual([m.split("\n")[0] for m in self.column(t, 0)], ["tiny", "big", "calm"], "kept through a reload")
        self.assertEqual(self.page.get_attribute(f"{t} thead th:nth-child(4)", "aria-sort"), "descending")
        self.assertNotEqual(default, self.column(t, 0))

    def test_bots_sort_by_name_and_by_machine_from_the_keyboard(self):
        t = "#fleet-bots table"
        self.sort_by(t, "Bot")
        self.assertEqual(self.column(t, 0), ["@amy", "@bob", "@kit", "@zed"])
        self.page.focus(f"{t} thead button.sort:text-is('Machine')")
        self.page.keyboard.press("Enter")
        self.assertEqual(self.column(t, 5), ["big", "big", "calm", "tiny"])
        self.assertEqual(self.page.get_attribute(f"{t} thead th:nth-child(1)", "aria-sort"), None)
        self.sort_by(t, "Active")
        self.assertEqual(self.column(t, 0), ["@kit", "@amy", "@zed", "@bob"])     # longest idle first

    def test_columns_with_nothing_to_sort_stay_plain(self):
        self.assertEqual(self.page.eval_on_selector_all("#fleet-versions thead th", "ts => ts.map(t => !!t.querySelector('button'))"),
                         [True, True, True, True, False])     # Note
        self.assertFalse(self.page.eval_on_selector("#fleet-machines thead th:last-child", "t => !!t.querySelector('button')"))


if __name__ == "__main__":
    unittest.main()
