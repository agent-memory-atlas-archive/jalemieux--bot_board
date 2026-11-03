"""The skill registry and bot profiles: fleet-wide skills, profiles edited in place and
revisioned (#397), a profile set once per bot and read at the start of a session. Run with python -m unittest discover -s tests."""
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
        self.assertEqual(dev["skills"], [{"name": "github-lifecycle", "mandatory": True}, {"name": "forgejo-lifecycle", "mandatory": False}])
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

    # ---- base skills (#347)

    def make_base(self):
        r = self.client.put("/api/skills/house-style", headers=self.human, json={"body": "Short sentences.", "base_revision": 0,
                            "fields": {"description": "How to write.", "base": True}, "reason": "every bot"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["skill"]["base"])

    def me(self, headers=None):
        return self.client.get("/api/me/profile?json", headers=headers or self.bot).json()

    def add_bot(self, handle, profile):
        self.client.put(f"/api/fleet/bots/{handle}", headers=self.human, json={"host": "box", "run": True, "harness": "claude",
                        "workspace": "proj", "profile": profile}).raise_for_status()
        return self.register(handle, "claude-code", "proj")

    def test_a_base_skill_reaches_a_bot_with_no_profile_and_its_revision_moves(self):
        self.assertEqual(self.me()["skills"], [])
        self.assertEqual(self.me()["skills_revision"], "")
        self.make_base()
        me = self.me()
        self.assertEqual([(s["name"], s["mandatory"], s["base"]) for s in me["skills"]], [("house-style", True, True)])
        before = me["skills_revision"]
        self.assertTrue(before)
        self.client.put("/api/skills/house-style", headers=self.human, json={"body": "Shorter.", "base_revision": 1,
                        "fields": {"description": "How to write."}}).raise_for_status()
        self.assertNotEqual(self.me()["skills_revision"], before)
        text = self.client.get("/api/me/profile", headers=self.bot).text
        self.assertIn("You have no profile", text)
        self.assertIn("- **house-style** (mandatory, every bot): How to write.", text)

    def test_base_comes_after_the_profiles_own_and_a_listing_changes_nothing(self):
        self.make_base()
        jane = self.add_bot("jane", "developer")
        self.assertEqual([s["name"] for s in self.me(jane)["skills"]], ["github-lifecycle", "forgejo-lifecycle", "house-style"])
        # a profile that lists it, not even as mandatory: once, and still mandatory
        self.client.post("/api/profiles", headers=self.human, json={"name": "lister", "role": "Lists it.", "skills": [
            {"name": "house-style"}, {"name": "github-lifecycle"}]}).raise_for_status()
        lee = self.add_bot("lee", "lister")
        self.assertEqual([(s["name"], s["mandatory"]) for s in self.me(lee)["skills"]],
                         [("house-style", True), ("github-lifecycle", False)])
        profile = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        self.assertEqual(profile["skills"], [{"name": "github-lifecycle", "mandatory": True}, {"name": "forgejo-lifecycle", "mandatory": False}])
        self.assertEqual([s["name"] for s in profile["base_skills"]], ["house-style"])
        self.assertEqual(self.client.get("/api/profiles/lister", headers=self.bot).json()["profile"]["base_skills"], [])
        self.assertIn('id="base-skills">Base skills, every bot has them: <a class="chip" href="/fleet/skills/house-style">',
                      self.client.get("/fleet/profiles/developer").text)
        # listed by the profile too, it is still shown as base, and not as one of the profile's own (#396)
        page = self.client.get("/fleet/profiles/lister").text
        self.assertIn('id="base-skills">Base skills, every bot has them: <a class="chip" href="/fleet/skills/house-style">', page)
        self.assertNotIn('name="skill" value="house-style"', page)
        self.assertIn("Skills · 1", page)

    def test_the_create_form_shows_base_skills_ticked_and_fixed(self):
        # #396: ticked, disabled and noted on the form; the form's script never sends them
        self.make_base()
        page = self.client.get("/fleet/profiles/new").text
        self.assertIn('name="skill" value="house-style" aria-label="include house-style" checked disabled>', page)
        self.assertIn('name="mandatory" value="house-style" aria-label="house-style mandatory" checked disabled>', page)
        self.assertIn("base: every bot has it. How to write.", page)
        self.assertLess(page.index('value="house-style"'), page.index('value="github-lifecycle"'))
        self.assertRegex(page, r'name="skill" value="github-lifecycle" aria-label="include github-lifecycle">')
        self.assertIn("input[name=skill]:checked:not(:disabled)", page)
        # what the form posts, without the base skill: the page lists it under base skills
        self.client.post("/api/profiles", headers=self.human, json={
            "name": "newbie", "role": "New.", "skills": [{"name": "github-lifecycle", "mandatory": True}]}).raise_for_status()
        self.assertEqual(self.client.get("/api/profiles/newbie", headers=self.bot).json()["profile"]["skills"],
                         [{"name": "github-lifecycle", "mandatory": True}])
        self.assertIn('Base skills, every bot has them: <a class="chip" href="/fleet/skills/house-style">',
                      self.client.get("/fleet/profiles/newbie").text)

    def test_an_edit_keeps_base_unless_it_says_false_and_history_shows_when(self):
        self.make_base()
        put = lambda rev, **f: self.client.put("/api/skills/house-style", headers=self.human, json={
            "body": "Text.", "base_revision": rev, "fields": {"description": "How to write.", **f}}).raise_for_status().json()["skill"]
        self.assertTrue(put(1)["base"], "an edit from a form that knows nothing of base keeps it")
        r = self.client.put("/api/skills/house-style", headers=self.human, json={"body": SKILL.replace("release-notes", "house-style"),
                            "base_revision": 2})
        self.assertTrue(r.json()["skill"]["base"], "and so does a whole SKILL.md without the field")
        self.assertFalse(put(3, base=False)["base"])
        self.assertEqual(self.me()["skills"], [])
        history = self.client.get("/api/skills/house-style/history", headers=self.bot).json()["revisions"]
        self.assertEqual([h["base"] for h in history], [False, True, True, True])
        r = self.client.put("/api/skills/house-style", headers=self.human, json={"body": "---\nname: house-style\ndescription: D.\nbase: true\n---\n\nX.\n",
                            "base_revision": 4})
        self.assertTrue(r.json()["skill"]["base"], "the frontmatter sets it too")
        self.assertTrue(self.skills()["house-style"]["base"])
        self.assertFalse(self.skills()["github-lifecycle"]["base"])
        r = self.client.put("/api/skills/house-style", headers=self.human, json={"body": "X.", "base_revision": 5,
                            "fields": {"description": "D.", "base": "maybe"}})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("base", self.client.get("/api/skills/house-style", headers=self.bot).text.split("---")[1])

    def test_a_base_skill_cannot_be_deleted_and_its_reset_reaches_every_bot(self):
        self.make_base()
        self.add_bot("jane", "developer")
        self.assertEqual(registry.bots_of_skill("house-style"), ["jane"])
        r = self.client.request("DELETE", "/api/skills/house-style", headers=self.human, json={"base_revision": 1, "reason": "no"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("base skill", r.text)
        self.client.put("/api/skills/house-style", headers=self.human, json={"body": "X.", "base_revision": 1,
                        "fields": {"description": "D.", "base": False}}).raise_for_status()
        r = self.client.request("DELETE", "/api/skills/house-style", headers=self.human, json={"base_revision": 2, "reason": "gone"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.me()["skills"], [])

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

    def test_a_profile_is_created_from_a_template_or_a_profile_and_never_renamed(self):
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
        self.assertEqual(p["revision"], 1)
        # no PATCH, and no rename: PUT edits the profile at its name (below)
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

    def test_a_profile_is_edited_in_place_and_its_bots_follow_at_their_next_prompt(self):
        # #397: the acceptance case. developer lists github-lifecycle; jane carries developer.
        jane = self.add_bot("jane", "developer")
        before = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        self.assertEqual(before["revision"], 1)
        self.assertIn("github-lifecycle", [s["name"] for s in before["skills"]])
        guide = self.client.get("/agents.md", headers=jane)
        self.assertIn("github-lifecycle@", guide.text)
        edit = {"role": before["role"], "responsibilities": before["responsibilities"], "focus": "Forgejo first.",
                "avoid": before["avoid"], "skills": [s for s in before["skills"] if s["name"] != "github-lifecycle"],
                "base_revision": 1, "reason": "the fleet moved to Forgejo"}
        # humans and admin bots only
        self.assertEqual(self.client.put("/api/profiles/developer", headers=self.bot, json=edit).status_code, 403)
        self.assertEqual(self.client.put("/api/profiles/developer", headers=jane, json=edit).status_code, 403)
        r = self.client.put("/api/profiles/developer", headers=self.human, json=edit)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()["profile"]
        self.assertEqual(p["revision"], 2)
        self.assertNotIn("github-lifecycle", [s["name"] for s in p["skills"]])
        self.assertEqual(p["focus"], "Forgejo first.")
        self.assertEqual(p["bots"], ["jane"])
        p = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        self.assertEqual(p["revision"], 2)
        self.assertNotIn("github-lifecycle", [s["name"] for s in p["skills"]])
        self.assertNotIn("developer", self.skills()["github-lifecycle"]["used_by"])
        # jane's next prompt: the composed guide moved and no longer carries the skill, and the
        # skills list the launcher writes from no longer names it (the launcher prunes it, #346)
        after = self.client.get("/agents.md", headers=jane)
        self.assertNotEqual(after.headers["X-Agents-Revision"], guide.headers["X-Agents-Revision"])
        self.assertNotIn("github-lifecycle@", after.text)
        self.assertIn("Forgejo first.", after.text)
        me = self.client.get("/api/me/profile?json", headers=jane).json()
        self.assertNotIn("github-lifecycle", [s["name"] for s in me["skills"]])
        self.assertEqual(me["profile"]["revision"], 2)
        # no bot was restarted: nothing asked of the keeper
        want = self.client.get("/api/fleet/hosts/box/want", headers=self.keeper).json()
        self.assertFalse(any(w.get("reset") for w in want.get("bots", [])), want)
        # history keeps both revisions, newest first, with who and why
        h = self.client.get("/api/profiles/developer/history", headers=self.bot).json()["revisions"]
        self.assertEqual([(x["revision"], x["author"], x["reason"]) for x in h], [(2, "op", "the fleet moved to Forgejo"), (1, "board", "created")])
        self.assertIn("github-lifecycle", [s["name"] for s in h[1]["skills"]])
        self.assertEqual(self.client.get("/api/profiles/nope/history", headers=self.bot).status_code, 404)
        # a stale base_revision is a conflict; a bad body a 422; the profile stays at 2
        r = self.client.put("/api/profiles/developer", headers=self.human, json=edit)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("at revision 2, not 1", r.json()["error"])
        r = self.client.put("/api/profiles/developer", headers=self.human, json={**edit, "base_revision": 2, "role": ""})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.put("/api/profiles/developer", headers=self.human, json={**edit, "base_revision": 2, "skills": [{"name": "nope"}]})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.client.put("/api/profiles/nope", headers=self.human, json=edit).status_code, 404)
        self.assertEqual(self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]["revision"], 2)
        # the skill nothing lists any more can go; one a profile still lists cannot
        self.assertEqual(self.client.get("/api/skills/github-lifecycle?json", headers=self.bot).json()["skill"]["used_by"],
                         ["architect", "product-manager"])
        r = self.client.request("DELETE", "/api/skills/github-lifecycle", headers=self.human, json={"base_revision": 1, "reason": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("edit them to drop it", r.json()["error"])

    def test_an_edit_that_makes_a_profile_specialized_needs_its_bots_bound(self):
        # #397 keeps the channel rule: a specialized profile's bots claim only in their channels
        self.add_bot("jane", "developer")
        p = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        edit = {k: p[k] for k in ("role", "responsibilities", "focus", "avoid", "skills")}
        r = self.client.put("/api/profiles/developer", headers=self.human,
                            json={**edit, "specialized": True, "base_revision": 1, "reason": "artists only"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("jane", r.json()["error"])
        # specialized left out: unchanged; bound to a channel: allowed
        r = self.client.put("/api/profiles/developer", headers=self.human, json={**edit, "base_revision": 1, "reason": "same"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["profile"]["specialized"])
        self.client.put("/api/fleet/bots/jane/channels", headers=self.human, json={"channels": ["proj"]}).raise_for_status()
        r = self.client.put("/api/profiles/developer", headers=self.human,
                            json={**edit, "specialized": True, "base_revision": 2, "reason": "artists only"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["profile"]["specialized"])
        self.assertTrue(registry.is_specialized("developer"))
        # a specialized bot cannot be unbound (fleet keeps that); a profile with no bots can still be deleted
        r = self.client.put("/api/fleet/bots/jane/channels", headers=self.human, json={"channels": []})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.delete("/api/profiles/artist", headers=self.human).status_code, 200)

    def test_a_board_from_before_profile_history_gets_revision_1_and_a_first_history_row(self):
        # #397 migration: profiles had no revision column and no history; init() adds both,
        # with the current text as revision 1, and leaves a profile already migrated alone
        self.client.put("/api/profiles/developer", headers=self.human, json={
            "role": "Edited.", "skills": [], "base_revision": 1, "reason": "once"}).raise_for_status()
        c = db.conn()
        with db._write_lock:
            c.execute("DROP TABLE profile_history")
            c.execute("ALTER TABLE profiles DROP COLUMN revision")
            c.commit()
        registry.init()
        p = self.client.get("/api/profiles/developer", headers=self.bot).json()["profile"]
        self.assertEqual((p["revision"], p["role"]), (1, "Edited."))
        h = self.client.get("/api/profiles/developer/history", headers=self.bot).json()["revisions"]
        self.assertEqual([(x["revision"], x["role"], x["reason"]) for x in h], [(1, "Edited.", "created")])
        registry.init()        # again: nothing doubled
        self.assertEqual(len(self.client.get("/api/profiles/artist/history", headers=self.bot).json()["revisions"]), 1)
        # the template's specialized flag is applied only to a profile nobody edited
        self.client.put("/api/profiles/artist", headers=self.human, json={
            "role": "Not special.", "skills": [], "specialized": False, "base_revision": 1, "reason": "x"}).raise_for_status()
        registry.init()
        self.assertFalse(self.client.get("/api/profiles/artist", headers=self.bot).json()["profile"]["specialized"])

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
        self.assertEqual([(s["name"], s["mandatory"], s["revision"]) for s in j["skills"]],
                         [("github-lifecycle", True, 1), ("forgejo-lifecycle", False, 1)])
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

    def test_the_guide_says_the_copy_ends_with_who_the_bot_is(self):
        # #344: the launcher fetches the guide with the bot's token and the copy carries its
        # profile and skills; the bot fetches nothing itself
        text = self.client.get("/agents.md").text
        self.assertIn("Your launcher fetched this guide with your", text)
        self.assertIn("whichever of those you have", text)
        self.assertIn("you never fetch it yourself", text)
        self.assertIn("`GET /api/me/profile`", text)


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
