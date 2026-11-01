"""One agent guide covers collaboration and protocol."""
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, web


class AgentGuide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app, base_url="http://fleet.example:8080")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def test_guide_serves_the_single_source_with_request_address(self):
        source = Path(main._ROOT, "AGENTS.md").read_text(encoding="utf-8")
        response = self.client.get("/agents.md")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, source.replace(
            "http://127.0.0.1:8080", "http://fleet.example:8080"))
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertIn("One thread has one worker.", response.text)
        self.assertIn("The board is the only communication channel between fleet agents.", response.text)
        self.assertIn("use harness-native messaging, teammate chats, or local cross-session channels", response.text)
        self.assertIn("Authorization: Bearer $BOARD_TOKEN", response.text)
        self.assertNotIn("llms.txt", response.text)
        # #241: a revision of exactly the text served, which launchers compare with what a session read
        self.assertEqual(response.headers["x-agents-revision"],
                         hashlib.sha256(response.text.encode()).hexdigest()[:16])

    def register(self, handle, kind, workspace=None):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind, "workspace": workspace})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def test_a_bots_token_gets_the_guide_composed_for_it(self):
        # #344: the file, then who the bot is: workspace and channels, profile, skills with the
        # mandatory ones written out. The launcher puts the whole text in the prompt.
        human = self.register("op", "human")
        keeper = self.register("keeper-box", "keeper")
        self.client.post("/api/workspaces", headers=human, json={"slug": "proj", "name": "Proj", "purpose": "Ship it."}).raise_for_status()
        self.client.post("/api/workspaces/proj/channels", headers=human,
                         json={"slug": "art", "name": "Art", "description": "concept art"}).raise_for_status()
        self.client.put("/api/fleet/hosts/box", headers=keeper, json={"bots": []}).raise_for_status()
        self.client.put("/api/fleet/bots/jane", headers=human, json={"host": "box", "run": True, "harness": "claude",
                                                                      "workspace": "proj", "profile": "developer"}).raise_for_status()
        jane = self.register("jane", "claude-code", "proj")
        source = Path(main._ROOT, "AGENTS.md").read_text(encoding="utf-8").replace("http://127.0.0.1:8080", "http://fleet.example:8080")
        r = self.client.get("/agents.md", headers=jane)
        self.assertEqual(r.status_code, 200)
        text = r.text
        self.assertTrue(text.startswith(source), "the guide comes first, unchanged")
        own = text[len(source):]
        self.assertIn("# Your identity", own)
        self.assertIn("You are `@jane`", own)
        self.assertIn("**Proj** (`proj`). Ship it.", own)
        self.assertIn("- `#proj` (General): Ship it.", own)
        self.assertIn("- `#proj.art`: Art — concept art", own)
        self.assertIn("`GET http://fleet.example:8080/api/knowledge/INDEX.md`", own)
        self.assertIn("## Your profile: developer", own)
        self.assertIn("### Responsibilities", own)
        self.assertIn("## Your skills", own)
        self.assertIn("- **github-lifecycle@1** (mandatory):", own)
        self.assertIn("### Skill: github-lifecycle", own)
        body = Path(main._ROOT, "docs/skills/github-lifecycle/SKILL.md").read_text(encoding="utf-8").split("---", 2)[2].strip()
        self.assertIn(body, own, "a mandatory skill is written out in full")
        self.assertEqual(r.headers["x-agents-revision"], hashlib.sha256(text.encode()).hexdigest()[:16])
        # a skill of the bot written on the board moves the revision; another skill does not
        before = r.headers["x-agents-revision"]
        self.client.put("/api/skills/other", headers=human, json={"body": "---\nname: other\ndescription: Other.\n---\n\nX.\n", "base_revision": 0}).raise_for_status()
        self.assertEqual(self.client.get("/agents.md", headers=jane).headers["x-agents-revision"], before)
        self.client.put("/api/skills/github-lifecycle", headers=human,
                        json={"body": "Shorter.", "fields": {"description": "Take an issue to a merged PR."},
                              "base_revision": 1, "reason": "shorter"}).raise_for_status()
        r = self.client.get("/agents.md", headers=jane)
        self.assertNotEqual(r.headers["x-agents-revision"], before)
        self.assertIn("- **github-lifecycle@2** (mandatory):", r.text)
        self.assertIn("### Skill: github-lifecycle\n\nShorter.", r.text)
        # a human's token, or none, gets the file alone
        self.assertEqual(self.client.get("/agents.md", headers=human).text, source)
        self.assertEqual(self.client.get("/agents.md").text, source)
        self.assertNotIn("# Your identity", source)

    def test_a_bot_without_a_profile_or_workspace_gets_only_what_it_has(self):
        # what a bot does not have is not listed: no "no profile" line, no empty headings
        bot = self.register("loner", "claude-code")
        own = self.client.get("/agents.md", headers=bot).text.split("# Your identity", 1)[1]
        self.assertIn("You are `@loner`", own)
        for absent in ("## Your workspace", "## Your profile", "## Your skills", "no profile", "no workspace"):
            self.assertNotIn(absent, own)

    def test_an_admin_bot_gets_the_administration_reference_composed(self):
        human = self.register("op", "human")
        self.client.post("/api/workspaces", headers=human, json={"slug": "admin", "name": "Admin", "purpose": "x"})
        bot = self.register("mod", "claude-code", "admin")
        text = self.client.get("/agents.md", headers=bot).text
        self.assertTrue(text.startswith("# Board administration"))
        self.assertIn("You are `@mod`", text)
        self.assertIn("**Admin** (`admin`)", text)

    def test_retired_protocol_endpoint_is_removed(self):
        self.assertEqual(self.client.get("/llms.txt").status_code, 404)

    def test_discovery_and_onboarding_point_to_the_single_guide(self):
        page = web.onboard_page("Fleet", "http://fleet.example:8080", False)
        self.assertIn('href="/agents.md"', page)
        self.assertIn("The fleet launcher handles pickup, delivery and waiting", page)
        self.assertNotIn("llms.txt", page)
        self.assertNotIn("Standing by for work", page)
        self.assertIn("GET /agents.md", main.API_MAP["endpoints"])
