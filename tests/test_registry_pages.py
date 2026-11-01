"""Skills and Profiles on /fleet, and the pages under /fleet/skills/ and /fleet/profiles/.
Run with python -m unittest discover -s tests."""
import os
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app import db, main, registry_web

VOID = {"br", "img", "input", "meta", "link", "hr", "col", "wbr", "source"}


class Balance(HTMLParser):
    """Every opened tag is closed, in order: the pages are hand-built strings."""
    def __init__(self, page):
        super().__init__()
        self.stack, self.errors = [], []
        self.feed(page)
        if self.stack:
            self.errors.append("unclosed: " + ", ".join(self.stack))

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'}")
        else:
            self.stack.pop()


class RegistryPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app, base_url="http://fleet.example:8080")
        self.client.__enter__()
        self.human = self.register("op", "human")
        self.keeper = self.register("keeper-box", "keeper")
        self.client.post("/api/workspaces", headers=self.human, json={"slug": "proj", "name": "Proj", "purpose": "x"})
        self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": [
            {"handle": "wren", "harness": "claude", "status": "waiting", "serves": "/pool", "pool": True}]}).raise_for_status()
        r = self.client.put("/api/fleet/bots/jane", headers=self.human,
                            json={"host": "box", "run": True, "harness": "claude", "workspace": "proj", "profile": "developer"})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.put("/api/skills/release-notes", headers=self.human, json={
            "body": "---\nname: release-notes\ndescription: Notes <for> a tag.\n---\n\n## Steps\n\n1. List `gh pr list` output.\n"
                    "2. Group by **area**.\n\n```\nexample\n```\n", "base_revision": 0, "reason": "first"}).raise_for_status()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, workspace=None):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind, "workspace": workspace})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def balanced(self, page):
        errors = Balance(page).errors
        self.assertEqual(errors, [], errors[:5])

    def test_registry_buttons_are_not_bot_actions(self):
        # The fleet page's click handler runs stop/start for every button.act; the registry's own
        # .act buttons (once the Check button of + Add a skill) made a click also try to add a bot
        # and alert "refused: 422". The handler takes only buttons that name a bot.
        page = self.client.get("/fleet").text
        self.assertIn("closest('button.act[data-handle][data-run]')", page)

    def test_fleet_page_keeps_each_section_on_its_tab(self):
        page = self.client.get("/fleet").text
        self.balanced(page)
        for first, then in (("tab-bots", "fleet-bots"), ("fleet-bots", "tab-machines"), ("tab-machines", "fleet-machines"),
                            ("fleet-machines", "fleet-versions"), ("fleet-versions", "tab-roles"), ("tab-roles", "fleet-profiles"),
                            ("fleet-profiles", "fleet-skills"), ("fleet-skills", "tab-settings"), ("tab-settings", "secrets")):
            self.assertLess(page.index(f'id="{first}"'), page.index(f'id="{then}"'))
        # skills: the seeded ones (github-lifecycle, board-guide, board-review, board-care) and ours, with kind, revision and who uses it
        self.assertIn("Skills · 5", page)
        self.assertIn('href="/fleet/skills/github-lifecycle"', page)
        self.assertIn('href="/fleet/skills/release-notes"', page)
        self.assertIn("Notes &lt;for&gt; a tag.", page)
        self.assertIn("no profile uses it", page)
        self.assertIn('<a class="chip" href="/fleet/profiles/developer">developer</a>', page)
        self.assertIn("+ Add a skill", page)
        # one form (board thread 8119): a GitHub link, or the skill written there
        self.assertIn('id="skill-add"', page)
        self.assertNotIn('name="source"', page)       # #328: no GitHub form; the skill is written here
        self.assertIn('name="body"', page)
        # profiles: the six seeded, their skills with ★ for mandatory, the bots, the actions
        self.assertIn("Roles · 5", page)
        self.assertIn('href="/fleet/profiles/new"', page)
        self.assertIn('href="/fleet/profiles/new?from=developer"', page)
        self.assertIn('<span class="star" title="mandatory">★</span>', page)
        self.assertIn('1 · <a href="/a/jane">jane</a>', page)
        self.assertIn("0 · no bot yet", page)
        self.assertNotIn('class="act del-profile" data-name="developer"', page)     # jane carries it
        self.assertIn('class="act del-profile" data-name="artist"', page)
        # no edit control anywhere for a profile
        self.assertNotIn("edit profile", page.lower())
        # bots without a profile are gathered on the last row
        self.assertIn('id="profile-none"', page)
        self.assertIn('<a href="/a/wren">wren</a>', page)

    def test_bot_rows_and_add_a_bot_carry_the_profile(self):
        page = self.client.get("/fleet").text
        self.assertIn("<th>Role</th>", page)
        self.assertIn('<td data-label="profile" data-sort="developer"><a class="chip" href="/fleet/profiles/developer">developer</a>', page)
        self.assertIn('<td data-label="profile" data-sort=""><span class="dim">none</span>', page)
        # a fleet bot's row says where it claims, and offers to change it (board thread 8378)
        self.assertIn('<div class="by dim">any channel</div>', page)
        self.assertIn('class="act chan-edit needs-me" data-handle="jane"', page)
        self.assertIn('<select name="channels" multiple', page)
        self.assertIn('<select name="profile"', page)
        self.assertIn('<option value="">none</option>', page)
        self.assertIn('<option value="developer">developer · A software developer', page)
        self.assertIn("keeps that profile for life", page)
        self.assertIn("f.elements.profile.value", page)

    def test_skill_page_edits_previews_and_lists_history_and_users(self):
        r = self.client.get("/fleet/skills/release-notes")
        self.assertEqual(r.status_code, 200)
        page = r.text
        self.balanced(page)
        self.assertIn("<h1>release-notes</h1>", page)
        self.assertIn("Save as rev 2", page)
        self.assertIn('name="reason" required', page)
        self.assertIn("History · 1", page)
        self.assertIn("No profile lists it yet", page)
        self.assertIn('class="act del-skill" data-name="release-notes" data-rev="1">', page)
        self.assertNotIn("data-rev=\"1\" disabled", page)
        # the preview is rendered Markdown, escaped
        self.assertIn("<h4>Steps</h4>", page)
        self.assertIn("<ol>", page)
        self.assertIn("<code>gh pr list</code>", page)
        self.assertIn("<b>area</b>", page)
        self.assertIn("<pre><code>example</code></pre>", page)
        self.assertIn("Notes &lt;for&gt; a tag.", page)
        self.assertNotIn("<for>", page)
        # the seeded skill is used by developer, so its delete is refused on the page too
        page = self.client.get("/fleet/skills/github-lifecycle").text
        self.balanced(page)
        self.assertIn("Used by · 3", page)
        self.assertIn('<a class="chip" href="/fleet/profiles/developer">developer</a> <span class="dim">1 bot · @jane', page)
        self.assertIn('data-name="github-lifecycle" data-rev="1" disabled>', page)
        self.assertIn("Refused while 3 profiles list it: architect, developer, product-manager.", page)
        self.assertEqual(self.client.get("/fleet/skills/nope").status_code, 404)
        # history grows and old revisions offer restore
        self.client.put("/api/skills/release-notes", headers=self.human,
                        json={"body": "## Steps\n\nNew.\n", "fields": {"description": "Notes."}, "base_revision": 1,
                              "reason": "shorter"}).raise_for_status()
        page = self.client.get("/fleet/skills/release-notes").text
        self.assertIn("History · 2", page)
        self.assertIn('class="act restore needs-me" data-rev="1"', page)
        self.assertNotIn('data-rev="2">restore', page)
        self.assertIn("shorter", page)

    def test_profile_page_is_read_only_and_shows_bots_text_and_lineage(self):
        r = self.client.get("/fleet/profiles/developer")
        self.assertEqual(r.status_code, 200)
        page = r.text
        self.balanced(page)
        self.assertIn("<h1>developer</h1>", page)
        self.assertIn('href="/fleet/profiles/new?from=developer">Clone</a>', page)
        self.assertIn("<h2>What it says</h2>", page)
        self.assertIn("<dt>responsibilities</dt>", page)
        self.assertIn("Skills · 1", page)
        self.assertIn("★ mandatory", page)
        self.assertIn("Bots with this profile · 1", page)
        self.assertIn('<a href="/a/jane">@jane</a>', page)
        self.assertIn('class="reset" data-handle="jane"', page)
        self.assertIn("# Your profile: developer", page)
        self.assertIn("GET http://fleet.example:8080/api/skills/&lt;name&gt;", page)
        self.assertIn("from the shipped developer template", page)
        self.assertIn('data-name="developer" disabled>Delete this profile</button>', page)
        self.assertIn("Not while 1 bot carries it.", page)
        self.assertEqual(page.count("<form"), 1)        # only the shell's search form: nothing to edit
        self.assertEqual(self.client.get("/fleet/profiles/nope").status_code, 404)
        # a clone appears in the lineage of both
        self.client.post("/api/profiles", headers=self.human, json={
            "name": "developer-2", "role": "Dev two", "skills": [{"name": "release-notes"}], "from": "developer"}).raise_for_status()
        self.assertIn('<a class="chip" href="/fleet/profiles/developer-2">developer-2</a>',
                      self.client.get("/fleet/profiles/developer").text)
        page = self.client.get("/fleet/profiles/developer-2").text
        self.assertIn('cloned from <a href="/fleet/profiles/developer">developer</a>', page)
        self.assertIn("No bot carries it yet.", page)
        self.assertIn('data-name="developer-2">Delete this profile</button>', page)

    def test_the_create_form_starts_from_a_template_or_a_profile(self):
        page = self.client.get("/fleet/profiles/new").text
        self.balanced(page)
        self.assertIn("<h2>Start from</h2>", page)
        for key in ("", "template:developer", "template:artist", "template:lore-writer", "developer", "artist", "lore-writer"):
            self.assertIn(f'data-key="{key}"', page)
        self.assertIn('data-key="developer" aria-pressed="true"', page)     # the default starter: the developer profile
        self.assertIn('data-key="template:developer" aria-pressed="false"', page)
        self.assertIn('name="skill" value="github-lifecycle"', page)
        self.assertIn('name="mandatory" value="release-notes"', page)
        self.assertIn('id="preview"', page)
        page = self.client.get("/fleet/profiles/new?from=artist").text
        self.assertIn('data-key="artist" aria-pressed="true"', page)
        self.assertIn('data-key="developer" aria-pressed="false"', page)
        # once the seeded profile is gone, its name picks the template
        self.client.delete("/api/profiles/artist", headers=self.human).raise_for_status()
        page = self.client.get("/fleet/profiles/new?from=artist").text
        self.assertIn('data-key="template:artist" aria-pressed="true"', page)
        self.assertNotIn('data-key="artist"', page)
        # the embedded data carries the starters and the taken names, as text in a JSON block
        self.assertIn('"taken": ["architect", "artist", "developer", "lore-writer", "product-manager"]', page)
        self.assertIn('"kind": "template"', page)
        self.assertIn('"kind": "profile"', page)

    def test_a_skills_bots_get_a_new_session_from_its_page(self):
        self.client.put("/api/skills/notes", headers=self.human, json={
            "body": "---\nname: notes\ndescription: Notes.\n---\n\nOne.\n", "base_revision": 0}).raise_for_status()
        page = self.client.get("/fleet/skills/notes").text
        self.balanced(page)
        self.assertNotIn('id="src-check"', page)      # #328: nothing to refetch, the board is the source
        self.assertNotIn("Refetch", page)
        self.assertIn("Save as rev 2", page)
        self.assertNotIn('id="reset-all"', page)       # no profile lists it yet
        # a profile that lists it: its bots are the ones to reset
        self.client.post("/api/profiles", headers=self.human, json={
            "name": "scribe", "role": "Scribe", "skills": [{"name": "notes"}]}).raise_for_status()
        self.client.put("/api/fleet/bots/ann", headers=self.human, json={
            "host": "box", "run": True, "harness": "claude", "workspace": "proj", "profile": "scribe"}).raise_for_status()
        self.assertIn('id="reset-all"', self.client.get("/fleet/skills/notes").text)
        bot = self.register("wren", "claude-code", "proj")
        self.assertEqual(self.client.post("/api/skills/notes/reset", headers=bot).status_code, 403)
        self.assertEqual(self.client.post("/api/skills/nope/reset", headers=self.human).status_code, 404)
        r = self.client.post("/api/skills/notes/reset", headers=self.human)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reset"] + list(r.json()["skipped"]), ["ann"])
        r = self.client.post("/api/profiles/developer/reset", headers=self.human)
        self.assertEqual(r.json()["reset"] + list(r.json()["skipped"]), ["jane"])
        self.assertIn('id="reset-all"', self.client.get("/fleet/profiles/developer").text)


class Markdown(unittest.TestCase):
    def test_subset(self):
        html = registry_web.md_html("# T\n\npara **b** `c`\n\n- a\n- b\n  more\n\n1. one\n2) two\n\n```\nx < y\n```\n")
        self.assertEqual(html, "<h3>T</h3>\n<p>para <b>b</b> <code>c</code></p>\n<ul>\n<li>a</li>\n<li>b more</li>\n</ul>\n"
                               "<ol>\n<li>one</li>\n<li>two</li>\n</ol>\n<pre><code>x &lt; y</code></pre>")

    def test_everything_is_escaped(self):
        self.assertNotIn("<script>", registry_web.md_html("<script>x</script>\n\n## <b>\n\n- <i>"))


if __name__ == "__main__":
    unittest.main()
