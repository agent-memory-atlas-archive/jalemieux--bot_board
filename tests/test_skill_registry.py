"""The skill registry and bot profiles: fleet-wide skills, write-once profiles, a profile
set once per bot and read at the start of a session. Run with python -m unittest discover -s tests."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app import db, main, registry

SKILL = """---
name: release-notes
description: Write the release notes for a tagged version.
---

# Release notes

List what changed since the last tag, grouped by area.
"""


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app, base_url="http://fleet.example:8080")
        self.client.__enter__()
        self.human = self.register("op", "human")
        self.keeper = self.register("keeper-box", "keeper")
        self.client.post("/api/workspaces", headers=self.human, json={"slug": "proj", "name": "Proj", "purpose": "x"})
        self.bot = self.register("tarzan", "claude-code", "proj")
        self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": []}).raise_for_status()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, workspace=None):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind, "workspace": workspace})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def skills(self):
        return {s["name"]: s for s in self.client.get("/api/skills", headers=self.bot).json()["skills"]}

    def profiles(self):
        return {p["name"]: p for p in self.client.get("/api/profiles", headers=self.bot).json()["profiles"]}

    # ---- seed

    def test_first_start_seeds_the_shipped_skills_and_profiles_once(self):
        skills, profiles = self.skills(), self.profiles()
        self.assertIn("github-lifecycle", skills)
        self.assertEqual(skills["github-lifecycle"]["revision"], 1)
        self.assertEqual(skills["github-lifecycle"]["author"], "board")
        self.assertEqual(sorted(skills["github-lifecycle"]["used_by"]), ["architect", "developer", "product-manager"])
        self.assertEqual(set(profiles), {"developer", "artist", "lore-writer", "product-manager", "architect"})
        dev = profiles["developer"]
        self.assertEqual(dev["skills"], [{"name": "github-lifecycle", "mandatory": True}])
        self.assertTrue(dev["responsibilities"].startswith("Work the threads"))
        self.assertTrue(dev["focus"] and dev["avoid"] and dev["role"])
        self.assertEqual(dev["clones"], [])        # a seeded profile is not its own clone
        # the text a bot reads is the shipped SKILL.md, verbatim after the frontmatter
        text = self.client.get("/api/skills/github-lifecycle", headers=self.bot).text
        shipped = Path(ROOT, "docs/skills/github-lifecycle/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(text.split("---", 2)[2].strip(), shipped.split("---", 2)[2].strip())
        self.assertIn("name: github-lifecycle", text)
        # deleting a seeded profile sticks across restarts: the files are seed, not source of truth
        r = self.client.delete("/api/profiles/artist", headers=self.human)
        self.assertEqual(r.status_code, 200, r.text)
        registry.init()
        self.assertNotIn("artist", self.profiles())
        # ...but the template is still offered
        names = [t["name"] for t in self.client.get("/api/profiles/templates", headers=self.human).json()["templates"]]
        self.assertEqual(names, ["architect", "artist", "developer", "lore-writer", "product-manager"])

    # ---- skills

    def test_humans_and_admin_bots_write_skills_everyone_reads_them(self):
        r = self.client.put("/api/skills/release-notes", headers=self.bot, json={"body": SKILL, "base_revision": 0})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"body": SKILL, "base_revision": 0, "reason": "first draft"})
        self.assertEqual(r.status_code, 200, r.text)
        skill = r.json()["skill"]
        self.assertEqual((skill["revision"], skill["author"]), (1, "op"))
        self.assertNotIn("kind", skill)
        self.assertEqual(skill["description"], "Write the release notes for a tagged version.")
        self.assertEqual(skill["used_by"], [])
        # a bot reads it as a SKILL.md; the fields travel in the frontmatter
        r = self.client.get("/api/skills/release-notes", headers=self.bot)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(r.headers["x-skill-revision"], "1")
        self.assertTrue(r.text.startswith("---\nname: release-notes\ndescription: Write the release notes"))
        self.assertIn("grouped by area", r.text)
        # and structured, on request
        self.assertEqual(self.client.get("/api/skills/release-notes?json", headers=self.bot).json()["skill"]["revision"], 1)
        # an admin bot may write too (the admin workspace is the first human's, #181)
        admin = self.register("adminbot", "claude-code", "admin")
        r = self.client.put("/api/skills/release-notes", headers=admin,
                            json={"body": "List every merged pull request since the last tag.", "base_revision": 1,
                                  "fields": {"description": "Release notes from merged PRs."}, "reason": "tighter"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["skill"]["revision"], 2)
        self.assertEqual(r.json()["skill"]["description"], "Release notes from merged PRs.")

    def test_a_stale_write_is_refused_and_history_keeps_every_revision(self):
        self.client.put("/api/skills/release-notes", headers=self.human, json={"body": SKILL, "base_revision": 0})
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"body": SKILL + "\nMore.\n", "base_revision": 0})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("revision 1, not 0", r.json()["error"])
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"body": SKILL + "\nMore.\n", "base_revision": 1, "reason": "more"})
        self.assertEqual(r.status_code, 200, r.text)
        history = self.client.get("/api/skills/release-notes/history", headers=self.bot).json()["revisions"]
        self.assertEqual([h["revision"] for h in history], [2, 1])
        self.assertEqual(history[0]["reason"], "more")
        self.assertNotIn("More.", self.client.get("/api/skills/release-notes?revision=1", headers=self.bot).text)
        self.assertIn("More.", self.client.get("/api/skills/release-notes", headers=self.bot).text)

    def test_a_skill_needs_a_description_and_a_body_and_a_valid_name(self):
        r = self.client.put("/api/skills/release-notes", headers=self.human, json={"body": "Do it.", "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("description", r.json()["error"])
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"body": "", "fields": {"description": "x"}, "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.put("/api/skills/Release Notes", headers=self.human, json={"body": SKILL, "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.put("/api/skills/other-name", headers=self.human, json={"body": SKILL, "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("must agree", r.json()["error"])

    def test_a_github_source_is_refused_skills_live_on_the_board(self):
        # #328: no github kind; a SKILL.md from anywhere is pasted as the body
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"fields": {"source": "https://github.com/acme/skills/tree/main/release-notes"},
                                  "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("live on the board", r.json()["error"])
        r = self.client.put("/api/skills/release-notes", headers=self.human,
                            json={"body": SKILL, "fields": {"kind": "github"}, "base_revision": 0})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn(self.client.post("/api/skills/check", headers=self.human, json={"source": "x"}).status_code, (404, 405))
        # a SKILL.md's frontmatter is read, the lines an older board served (kind, source, ref) dropped
        old = SKILL.replace("---\n\n", "kind: github\nsource: https://github.com/acme/skills/tree/main/release-notes\nref: main\n---\n\n", 1)
        r = self.client.put("/api/skills/release-notes", headers=self.human, json={"body": old, "base_revision": 0})
        self.assertEqual(r.status_code, 200, r.text)
        text = self.client.get("/api/skills/release-notes", headers=self.bot).text
        self.assertEqual(text.split("---")[1], "\nname: release-notes\ndescription: Write the release notes for a tagged version.\n")

    def test_a_github_skill_from_before_becomes_a_board_skill_at_the_next_start(self):
        # #328: a board that holds github-kind skills adopts them, body kept, history kept
        with db._write_lock, db.conn() as c:
            c.execute("INSERT INTO skills(name,revision,fields,body,reason,created_at) VALUES (?,?,?,?,?,?)",
                      ("pony", 1, '{"description": "Lazy.", "kind": "github", "source": "https://github.com/acme/skills/tree/main/pony", "ref": "main"}',
                       "Be lazy.\n", "added from GitHub", db.now()))
            c.execute("INSERT INTO skills(name,revision,fields,body,reason,created_at) VALUES (?,?,?,?,?,?)",
                      ("pony", 2, '{"description": "Lazier.", "kind": "github", "source": "https://github.com/acme/skills/tree/main/pony", "ref": "main"}',
                       "Be lazier.\n", "refetched main", db.now()))
        registry.adopt_github_skills()
        registry.adopt_github_skills()        # idempotent
        skill = self.client.get("/api/skills/pony?json", headers=self.bot).json()["skill"]
        self.assertEqual((skill["revision"], skill["description"], skill["body"]), (3, "Lazier.", "Be lazier.\n"))
        self.assertNotIn("kind", skill)
        self.assertIn("GitHub source https://github.com/acme/skills/tree/main/pony is dropped", skill["reason"])
        self.assertEqual([h["revision"] for h in self.client.get("/api/skills/pony/history", headers=self.bot).json()["revisions"]], [3, 2, 1])
        self.assertNotIn("kind: github", self.client.get("/api/skills/pony", headers=self.bot).text)

    def test_the_skills_revision_moves_when_a_profiles_skill_is_written(self):
        # #328: what a launcher compares at each prompt, instead of a new session
        self.client.put("/api/fleet/bots/jane", headers=self.human, json={"host": "box", "run": True, "harness": "claude",
                                                                          "workspace": "proj", "profile": "developer"}).raise_for_status()
        jane = self.register("jane", "claude-code", "proj")
        r = self.client.get("/api/me/profile", headers=jane)
        before = r.headers["X-Skills-Revision"]
        self.assertRegex(before, r"^[0-9a-f]{16}$")
        self.assertEqual(self.client.get("/api/me/profile?json", headers=jane).json()["skills_revision"], before)
        # another skill moving changes nothing for jane
        self.client.put("/api/skills/release-notes", headers=self.human, json={"body": SKILL, "base_revision": 0}).raise_for_status()
        self.assertEqual(self.client.get("/api/me/profile", headers=jane).headers["X-Skills-Revision"], before)
        # hers does
        self.client.put("/api/skills/github-lifecycle", headers=self.human,
                        json={"body": "Shorter.", "fields": {"description": "Take an issue to a merged PR."},
                              "base_revision": 1, "reason": "shorter"}).raise_for_status()
        after = self.client.get("/api/me/profile", headers=jane).headers["X-Skills-Revision"]
        self.assertNotEqual(after, before)
        # a bot without a profile, or whose profile brings no skills, gets an empty one
        self.assertEqual(self.client.get("/api/me/profile", headers=self.bot).headers["X-Skills-Revision"], "")

    def test_deleting_a_skill_a_profile_lists_is_refused(self):
        r = self.client.request("DELETE", "/api/skills/github-lifecycle", headers=self.human, json={"base_revision": 1})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("developer", r.json()["error"])
        self.assertIn("github-lifecycle", self.skills())
        # a skill no profile lists goes, and its history stays
        self.client.put("/api/skills/release-notes", headers=self.human, json={"body": SKILL, "base_revision": 0})
        r = self.client.request("DELETE", "/api/skills/release-notes", headers=self.human,
                                json={"base_revision": 1, "reason": "unused"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("release-notes", self.skills())
        self.assertEqual(self.client.get("/api/skills/release-notes", headers=self.bot).status_code, 404)
        history = self.client.get("/api/skills/release-notes/history", headers=self.bot).json()["revisions"]
        self.assertEqual([(h["revision"], h["deleted"]) for h in history], [(2, True), (1, False)])
        # and the name can be used again
        r = self.client.put("/api/skills/release-notes", headers=self.human, json={"body": SKILL, "base_revision": 2})
        self.assertEqual(r.status_code, 200, r.text)

    # ---- profiles

    def test_a_profile_is_created_from_a_template_or_a_profile_and_never_edited(self):
        body = {"name": "reviewer", "role": "Reviews pull requests.", "responsibilities": "Read every PR.",
                "focus": "Correctness.", "avoid": "Style nits.", "skills": [{"name": "github-lifecycle"}],
                "from": "developer"}
        self.assertEqual(self.client.post("/api/profiles", headers=self.bot, json=body).status_code, 403)
        r = self.client.post("/api/profiles", headers=self.human, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        p = r.json()["profile"]
        self.assertEqual(p["skills"], [{"name": "github-lifecycle", "mandatory": False}])
        self.assertEqual((p["from"], p["created_by"], p["bots"], p["clones"]), ("developer", "op", [], []))
        self.assertEqual(self.profiles()["developer"]["clones"], ["reviewer"])
        self.assertEqual(sorted(self.skills()["github-lifecycle"]["used_by"]), ["architect", "developer", "product-manager", "reviewer"])
        # there is no edit: not PUT, not PATCH
        self.assertIn(self.client.put("/api/profiles/reviewer", headers=self.human, json=body).status_code, (404, 405))
        self.assertIn(self.client.patch("/api/profiles/reviewer", headers=self.human, json=body).status_code, (404, 405))
        # a name is taken for ever, a role is required, a skill must exist
        r = self.client.post("/api/profiles", headers=self.human, json=body)
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.post("/api/profiles", headers=self.human, json={**body, "name": "reviewer-2", "role": ""})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.post("/api/profiles", headers=self.human,
                             json={**body, "name": "reviewer-2", "skills": [{"name": "no-such-skill"}]})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("no skill named no-such-skill", r.json()["error"])
        # deleted: gone from the list, name still taken
        self.assertEqual(self.client.delete("/api/profiles/reviewer", headers=self.human).status_code, 200)
        self.assertNotIn("reviewer", self.profiles())
        self.assertEqual(self.client.get("/api/profiles/reviewer", headers=self.bot).status_code, 404)
        self.assertEqual(self.client.post("/api/profiles", headers=self.human, json=body).status_code, 409)

    def test_the_profile_page_carries_the_text_a_bot_reads(self):
        p = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        self.assertIn("# Your profile: developer", p["text"])
        self.assertIn("**github-lifecycle** (mandatory)", p["text"])
        self.assertIn("GET http://fleet.example:8080/api/skills/<name>", p["text"])

    # ---- a bot's profile

    def test_a_bot_gets_its_profile_when_added_and_cannot_be_given_another(self):
        r = self.client.put("/api/fleet/bots/jane", headers=self.human,
                            json={"host": "box", "run": True, "harness": "claude", "workspace": "proj", "profile": "nobody"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("no profile named nobody", r.json()["error"])
        r = self.client.put("/api/fleet/bots/jane", headers=self.human,
                            json={"host": "box", "run": True, "harness": "claude", "workspace": "proj", "profile": "developer"})
        self.assertEqual(r.status_code, 200, r.text)
        # the keeper sees it in what is asked of its box; the fleet page's rows carry it
        want = self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": []}).json()["want"]
        self.assertEqual([(w["handle"], w["profile"]) for w in want], [("jane", "developer")])
        bots = self.client.get("/api/fleet", headers=self.human).json()["hosts"][0]["bots"]
        self.assertEqual([(b["handle"], b["profile"]) for b in bots], [("jane", "developer")])
        self.assertEqual(self.profiles()["developer"]["bots"], ["jane"])
        # a stop, a restart: the profile stays without being named again
        self.client.put("/api/fleet/bots/jane", headers=self.human, json={"host": "box", "run": False}).raise_for_status()
        self.client.put("/api/fleet/bots/jane", headers=self.human, json={"host": "box", "run": True}).raise_for_status()
        self.assertEqual(self.profiles()["developer"]["bots"], ["jane"])
        # another profile: refused, and nothing changes
        r = self.client.put("/api/fleet/bots/jane", headers=self.human,
                            json={"host": "box", "run": True, "profile": "artist"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("set once", r.json()["error"])
        self.assertEqual(self.profiles()["developer"]["bots"], ["jane"])
        # the same profile again is fine
        self.assertEqual(self.client.put("/api/fleet/bots/jane", headers=self.human,
                                         json={"host": "box", "run": True, "profile": "developer"}).status_code, 200)
        # its profile cannot be deleted while jane carries it
        r = self.client.delete("/api/profiles/developer", headers=self.human)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("jane", r.json()["error"])
        # the roster says what each bot is
        jane = self.register("jane", "claude-code", "proj")
        roster = {a["handle"]: a for a in self.client.get("/api/agents", headers=self.human).json()["agents"]}
        self.assertEqual(roster["jane"]["profile"], "developer")
        self.assertIsNone(roster["tarzan"]["profile"])
        self.assertEqual(self.client.get("/api/agents/jane", headers=self.bot).json()["agent"]["profile"], "developer")
        # and a new session reads the profile as text, mandatory skills called out
        r = self.client.get("/api/me/profile", headers=jane)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn("# Your profile: developer", r.text)
        self.assertIn("**Role.** A software developer", r.text)
        self.assertIn("## Responsibilities", r.text)
        self.assertIn("- **github-lifecycle** (mandatory): Take a GitHub issue", r.text)
        self.assertIn("When a skill you have changes, your launcher says so in a prompt", r.text)
        j = self.client.get("/api/me/profile?json", headers=jane).json()
        self.assertEqual(j["profile"]["name"], "developer")
        self.assertEqual([(s["name"], s["mandatory"], s["revision"]) for s in j["skills"]], [("github-lifecycle", True, 1)])
        self.assertNotIn("kind", j["skills"][0])

    def test_a_bots_page_shows_its_profile(self):
        # board thread 8318
        self.client.put("/api/fleet/bots/jane", headers=self.human, json={"host": "box", "run": True, "harness": "claude",
                                                                          "workspace": "proj", "profile": "developer"}).raise_for_status()
        self.register("jane", "claude-code", "proj")
        page = self.client.get("/a/jane").text
        self.assertIn('Profile: <a href="/fleet/profiles/developer">developer</a>', page)
        self.assertIn("A software developer", page)
        self.assertIn('<a href="/fleet/skills/github-lifecycle">github-lifecycle</a> (mandatory)', page)
        self.assertNotIn("Profile:", self.client.get("/a/tarzan").text)

    def test_a_board_seeded_before_the_flag_gets_it_from_the_template(self):
        # profiles seeded from the artist template before `specialized` existed: the migration marks them
        with db._write_lock, db.conn() as c:
            c.execute("ALTER TABLE profiles DROP COLUMN specialized")
        registry.init()
        self.assertTrue(self.profiles()["artist"]["specialized"])
        self.assertFalse(self.profiles()["developer"]["specialized"])
        # a profile that only shares the template's name, cloned or hand-made, is left alone
        r = self.client.post("/api/profiles", headers=self.human,
                             json={"name": "artist-2", "role": "paints", "from": "artist"})
        self.assertEqual(r.status_code, 201, r.text)
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE profiles SET specialized=0")
            c.execute("ALTER TABLE profiles DROP COLUMN specialized")
        registry.init()
        self.assertTrue(self.profiles()["artist"]["specialized"])
        self.assertFalse(self.profiles()["artist-2"]["specialized"])

    def test_a_board_with_the_column_but_not_the_flag_gets_it_too(self):
        # the column arrived (#270) before the template said specialized: the next start still marks it
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE profiles SET specialized=0")
        self.assertFalse(self.profiles()["artist"]["specialized"])
        registry.init()
        self.assertTrue(self.profiles()["artist"]["specialized"])
        self.assertFalse(self.profiles()["developer"]["specialized"])

    def test_a_new_unnamed_bot_takes_a_profile_too(self):
        # artist is specialized: it needs the channels it will claim in (board thread 8378)
        r = self.client.post("/api/fleet/bots", headers=self.human,
                             json={"host": "box", "harness": "claude", "workspace": "proj", "profile": "artist"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("specialized profile", r.text)
        r = self.client.post("/api/fleet/bots", headers=self.human,
                             json={"host": "box", "harness": "claude", "workspace": "proj", "profile": "artist",
                                   "channels": ["proj"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.profiles()["artist"]["bots"], [r.json()["handle"]])

    def test_a_bot_without_a_profile_is_told_so(self):
        r = self.client.get("/api/me/profile", headers=self.bot)
        self.assertEqual(r.status_code, 200)
        self.assertIn("You have no profile.", r.text)
        self.assertEqual(self.client.get("/api/me/profile?json", headers=self.bot).json(),
                         {"profile": None, "skills": [], "skills_revision": ""})

    def test_the_guide_sends_a_new_session_to_its_profile(self):
        text = self.client.get("/agents.md").text
        self.assertIn("`GET /api/me/profile`", text)
        self.assertIn("Read it at the start of every session", text)
        self.assertIn("when one of yours changes, your launcher says so in a prompt", text)


class Frontmatter(unittest.TestCase):
    def test_skills_list_and_scalars(self):
        fields, body = registry.parse_front("---\nname: dev\nrole: \"A: dev\"\nskills:\n  - name: a\n    mandatory: true\n"
                                            "  - b\n  - name: c\n    mandatory: false\n---\n\n## Focus\nx\n")
        self.assertEqual(fields["name"], "dev")
        self.assertEqual(fields["role"], "A: dev")
        self.assertEqual(fields["skills"], [{"name": "a", "mandatory": True}, {"name": "b", "mandatory": False},
                                            {"name": "c", "mandatory": False}])
        self.assertEqual(body, "## Focus\nx\n")

    def test_block_scalars_as_ponytail_writes_them(self):
        fields, body = registry.parse_front("---\nname: ponytail-audit\ndescription: >\n  Quality audit of a whole repo.\n"
                                            "  One-shot report.\nlicense: MIT\nnotes: |\n  a\n  b\n---\n\nBody.\n")
        self.assertEqual(fields["description"], "Quality audit of a whole repo. One-shot report.")
        self.assertEqual(fields["license"], "MIT")
        self.assertEqual(fields["notes"], "a b")
        self.assertEqual(body, "Body.\n")

    def test_no_frontmatter_is_all_body(self):
        self.assertEqual(registry.parse_front("# Title\n"), ({}, "# Title\n"))

    def test_skill_text_round_trips(self):
        text = registry.skill_text({"name": "x", "description": "Does: things", "body": "Body."})
        fields, body = registry.parse_front(text)
        self.assertEqual((fields["name"], fields["description"], body.strip()), ("x", "Does: things", "Body."))



class Shipped(unittest.TestCase):
    """The starters reach the board that runs in the image (board thread 8173): without
    docs/profiles the create form offers no template and a new board seeds nothing."""

    def test_the_image_carries_the_skill_and_profile_starters(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY docs/skills ./docs/skills", dockerfile)
        self.assertIn("COPY docs/profiles ./docs/profiles", dockerfile)
        names = [t["name"] for t in registry.templates()]
        self.assertTrue({"developer", "product-manager", "architect"} <= set(names), names)
        for t in registry.templates():
            with self.subTest(template=t["name"]):
                self.assertTrue(t["role"] and t["responsibilities"] and t["focus"] and t["avoid"])


if __name__ == "__main__":
    unittest.main()
