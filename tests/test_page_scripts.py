"""Every inline script the board's pages carry parses as JavaScript. The pages are built as
Python strings, where one stray escape (a '\\n' that Python turned into a newline) breaks the
whole script block and every button on the page with it. Needs node; skipped without it."""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from app import db, main

NODE = shutil.which("node")
SCRIPT_RE = re.compile(r'<script(?![^>]*type="application/json")[^>]*>(.*?)</script>', re.S)


@unittest.skipUnless(NODE, "node is not installed")
class PageScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        r = self.client.post("/api/agents", json={"handle": "op", "kind": "human"})
        self.human = {"Authorization": "Bearer " + r.json()["token"]}
        self.client.post("/api/workspaces", headers=self.human, json={"slug": "proj", "name": "Proj", "purpose": "x"})
        r = self.client.post("/api/agents", json={"handle": "keeper-box", "kind": "keeper"})
        self.client.put("/api/fleet/hosts/box", headers={"Authorization": "Bearer " + r.json()["token"]},
                        json={"bots": [{"handle": "ann", "harness": "claude", "status": "waiting", "serves": "/pool", "pool": True}]}
                        ).raise_for_status()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def check(self, path):
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200, path)
        scripts = SCRIPT_RE.findall(page.text)
        self.assertTrue(scripts, f"{path} carries no script")
        for i, script in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(script)
            try:
                r = subprocess.run([NODE, "--check", fh.name], capture_output=True, text=True)
            finally:
                os.unlink(fh.name)
            self.assertEqual(r.returncode, 0, f"{path} script {i}:\n{r.stderr[:800]}")

    def test_every_page_script_parses(self):
        for path in ("/", "/fleet", "/fleet/skills/github-lifecycle", "/fleet/profiles/developer",
                     "/fleet/profiles/new", "/workspaces", "/routines", "/status", "/admin", "/onboard", "/a/op"):
            with self.subTest(path=path):
                self.check(path)
