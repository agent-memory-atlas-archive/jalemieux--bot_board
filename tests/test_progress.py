"""Who may claim, and the board keeping work moving (board thread 8378).
Run with python -m unittest discover -s tests."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BOT_BOARD_PROGRESS"] = "0"      # the sweep is called by hand here
from fastapi.testclient import TestClient
from app import db, main, progress

LONG_AGO = "2026-01-01T00:00:00+00:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register("op", "human")
        self.keeper = self.register("keeper-box", "keeper")
        self.client.post("/api/workspaces", headers=self.human, json={"slug": "sg", "name": "SG", "purpose": "x"})
        r = self.client.post("/api/workspaces/sg/channels", headers=self.human,
                             json={"slug": "art", "name": "Art", "description": "paintings"})
        self.assertEqual(r.status_code, 201, r.text)
        self.art = r.json()["channel"]["slug"]
        self.bot = self.register("edie", "claude-code", "sg")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind, workspace=None, **extra):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind, "workspace": workspace, **extra})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def post(self, body, channel="sg", auth=None, reply=None):
        r = self.client.post("/api/messages", headers=auth or self.human,
                             json={"channel": channel, "body": body, "reply_to": reply})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["message"]

    def claim(self, tid, auth=None):
        return self.client.post(f"/api/threads/{tid}/worker", headers=auth or self.bot)

    def worker(self, tid):
        return self.client.get(f"/api/threads/{tid}/worker").json()["worker"]

    def report(self, handle, status):
        r = self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": [
            {"handle": handle, "harness": "claude", "status": status, "serves": "/pool", "pool": True}]})
        self.assertEqual(r.status_code, 200, r.text)

    def add(self, handle, expected=200, **body):
        r = self.client.put(f"/api/fleet/bots/{handle}", headers=self.human,
                            json={"host": "box", "run": True, "harness": "claude", "workspace": "sg", **body})
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def messages(self, tid):
        return self.client.get(f"/api/threads/{tid}").json()["messages"]

    def silent(self, handle, at=LONG_AGO):
        db.conn().execute("UPDATE agents SET last_seen_at=? WHERE handle=?", (at, handle))
        db.conn().commit()


class WhoMayClaim(Base):
    def test_a_bot_bound_to_channels_claims_only_there(self):
        self.report("edie", "waiting")
        self.add("edie", channels=[self.art])
        bot = next(b for b in self.client.get("/api/fleet").json()["hosts"][0]["bots"] if b["handle"] == "edie")
        self.assertEqual(bot["channels"], [self.art])
        general = self.post("general work")["id"]
        painting = self.post("a painting", channel=self.art)["id"]
        r = self.claim(general)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("bound to " + self.art, r.json()["error"] if "error" in r.json() else r.text)
        self.assertEqual(self.claim(painting).status_code, 200)
        # mentioned elsewhere, it still takes part: a reply needs no claim
        self.post("@edie what do you think?", reply=general)
        # unbound again: anywhere
        r = self.client.put("/api/fleet/bots/edie/channels", headers=self.human, json={"channels": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.claim(general).status_code, 200)
        page = self.client.get("/fleet").text
        self.assertIn('id="channels-data"', page)
        self.assertIn('name="channels" multiple', page)

    def test_channels_must_belong_to_the_workspace(self):
        self.report("edie", "waiting")
        r = self.add("edie", expected=409, channels=["elsewhere"])
        self.assertIn("not a channel of sg", r.text)
        r = self.client.put("/api/fleet/bots/edie/channels", headers=self.human, json={"channels": ["nope"]})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.client.put("/api/fleet/bots/edie/channels", headers=self.bot,
                                         json={"channels": []}).status_code, 403)

    def test_a_specialized_profile_needs_a_channel(self):
        self.report("lena", "waiting")
        self.assertTrue(self.client.get("/api/profiles/artist").json()["profile"]["specialized"])
        r = self.add("lena", expected=409, profile="artist")
        self.assertIn("specialized profile", r.text)
        self.add("lena", profile="artist", channels=[self.art])
        r = self.client.put("/api/fleet/bots/lena/channels", headers=self.human, json={"channels": []})
        self.assertEqual(r.status_code, 409, r.text)      # cannot unbind a specialized bot
        self.assertIn("Specialized", self.client.get("/fleet/profiles/artist").text)
        # a new profile can be created specialized, and a developer is not
        r = self.client.post("/api/profiles", headers=self.human,
                             json={"name": "sound", "role": "sounds", "specialized": True})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.json()["profile"]["specialized"])
        self.assertFalse(self.client.get("/api/profiles/developer").json()["profile"]["specialized"])
        self.assertIn("**Specialized.**", self.client.get("/api/profiles/sound").json()["profile"]["text"])

    def test_an_interactive_session_never_claims(self):
        me = self.register("wren", "claude-code", "sg", interactive=True)
        tid = self.post("work")["id"]
        r = self.claim(tid, me)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("interactive session", r.text)
        self.assertTrue(self.client.get("/api/agents/wren").json()["agent"]["interactive"])
        # flipped later by a human, or by the session itself
        self.assertEqual(self.client.put("/api/agents/wren/interactive", headers=self.bot,
                                         json={"interactive": True}).status_code, 403)
        self.assertEqual(self.client.put("/api/agents/wren/interactive", headers=me,
                                         json={"interactive": False}).status_code, 200)
        self.assertEqual(self.claim(tid, me).status_code, 200)
        self.assertEqual(self.client.put("/api/agents/wren/interactive", headers=self.human,
                                         json={"interactive": True}).status_code, 200)
        self.assertIn('&quot;interactive&quot;: true', self.client.get("/onboard").text)

    def test_the_roster_carries_the_role(self):
        self.report("lena", "waiting")
        self.add("lena", profile="artist", channels=[self.art])
        self.register("lena-agent", "claude-code", "sg")      # the handle registers when the bot starts
        rows = {a["handle"]: a for a in self.client.get("/api/agents", headers=self.bot).json()["agents"]}
        self.assertIsNone(rows["edie"]["role"])
        # the fleet row for lena exists before her agent does; once registered, the role shows
        self.register("lena", "claude-code", "sg")
        rows = {a["handle"]: a for a in self.client.get("/api/agents", headers=self.bot).json()["agents"]}
        self.assertEqual(rows["lena"]["profile"], "artist")
        self.assertTrue(rows["lena"]["role"].startswith("A visual artist"))


class Moderation(Base):
    def moderator(self):
        return self.client.get("/api/workspaces", headers=self.human).json()["moderator"]

    def test_the_board_knows_its_moderator(self):
        # the admin workspace exists since the first human signed in; no bot in it yet
        self.assertEqual(self.moderator(), {"handle": None, "running": False})
        # any bot of the admin workspace is the moderator: no profile to pick
        self.report("ada", "waiting")
        r = self.client.put("/api/fleet/bots/ada", headers=self.human,
                            json={"host": "box", "run": True, "harness": "claude", "workspace": "admin"})
        self.assertEqual(r.status_code, 200, r.text)
        self.register("ada", "claude-code", "admin")
        self.assertEqual(self.moderator(), {"handle": "ada", "running": True})
        r = self.client.put("/api/fleet/bots/ada", headers=self.human,
                            json={"host": "box", "run": False, "harness": "claude"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.moderator(), {"handle": "ada", "running": False})

    def test_the_pages_say_so(self):
        page = self.client.get("/workspaces").text
        self.assertIn("No moderator: no bot in the admin workspace.", page)
        self.assertIn("Add a bot in the admin workspace", page)
        self.assertIn("moderatorNote(d)", page)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("### Moderator", readme)
        self.assertIn("| Moderator |", readme)
        self.assertNotIn("moderator", [p["name"] for p in self.client.get("/api/profiles").json()["profiles"]])
        self.assertIn("## Asks you will get", self.client.get("/api/skills/board-guide").text)
        admin = self.client.get("/admin/agents.md", headers=self.human).text
        self.assertIn("**You are the moderator.**", admin)
        self.assertIn("GET /api/skills/board-guide", admin)
        self.assertIn("GET /api/skills/board-review", admin)
        self.assertIn("## 2. Measure, do not guess", self.client.get("/api/skills/board-review").text)


class KeepingWorkMoving(Base):
    def setUp(self):
        super().setUp()
        self.report("edie", "running sg")
        self.add("edie")
        self.tid = self.post("build the thing")["id"]
        self.assertEqual(self.claim(self.tid).status_code, 200)
        self.post("on it: branch edie-thing, PR #12 soon", auth=self.bot, reply=self.tid)

    def test_a_running_worker_is_left_alone(self):
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})
        self.assertEqual(self.worker(self.tid), "edie")

    def test_a_stopped_worker_is_released_with_where_it_stood(self):
        self.report("edie", "stopped")      # crashed, or stopped for good: the keeper says down...
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})     # ...a restart gets a grace period
        self.silent("edie")                 # ...and the bot has not touched the board since
        done = progress.sweep()
        self.assertEqual(done["released"], [self.tid])
        self.assertIsNone(self.worker(self.tid))
        notice = self.messages(self.tid)[-1]
        self.assertEqual(notice["author"], "board")
        self.assertEqual(notice["meta"], {"kind": "thread_released", "by": "board", "was": "edie", "why": "stopped"})
        self.assertIn("@edie is stopped; available for pickup.", notice["body"])
        self.assertIn("Last from @edie", notice["body"])
        self.assertIn("branch edie-thing, PR #12 soon", notice["body"])
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})     # once
        # the next eligible bot claims it; a bot not in the fleet, unseen for an hour, counts as away
        other = self.register("nico", "claude-code", "sg")
        self.assertEqual(self.claim(self.tid, other).status_code, 200)
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})     # seen just now
        self.silent("nico")
        self.assertEqual(progress.sweep()["released"], [self.tid])
        self.assertIn("has not been seen for a while", self.messages(self.tid)[-1]["body"])

    def test_a_human_reply_into_a_dead_thread_frees_it_at_once(self):
        self.report("edie", "stopped")
        self.silent("edie")
        self.post("@edie what is the status?", reply=self.tid)
        self.assertIsNone(self.worker(self.tid))
        bodies = [m["body"] for m in self.messages(self.tid)]
        self.assertIn("what is the status?", bodies[-2])
        self.assertTrue(bodies[-1].startswith("Thread worker released by the board: @edie is stopped"))
        # a worker that is running gets the reply as usual, nothing is released
        self.assertEqual(self.claim(self.tid).status_code, 200)      # the claim touches the board: seen now
        self.report("edie", "running sg")
        self.post("and now?", reply=self.tid)
        self.assertEqual(self.worker(self.tid), "edie")
        # a box that stopped reporting: presence decides, after an hour
        db.conn().execute("DELETE FROM fleet_hosts"); db.conn().commit()
        self.post("anyone?", reply=self.tid)
        self.assertEqual(self.worker(self.tid), "edie")
        self.silent("edie")
        self.post("anyone at all?", reply=self.tid)
        self.assertIsNone(self.worker(self.tid))
        self.assertIn("has not been seen for a while", self.messages(self.tid)[-1]["body"])

    def test_a_quiet_worker_is_nudged_once_then_freed(self):
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid))
        db.conn().commit()
        done = progress.sweep()
        self.assertEqual(done, {"released": [], "nudged": [self.tid], "attention": None})
        nudge = self.messages(self.tid)[-1]
        self.assertEqual(nudge["author"], "board")
        self.assertTrue(nudge["body"].startswith("@edie this thread has not moved in"))
        self.assertEqual(nudge["mentions"], ["edie"])
        self.assertEqual(nudge["meta"], {"kind": "nudge", "to": "edie"})
        self.assertEqual(self.worker(self.tid), "edie")
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})     # not twice
        # the worker answers: the clock restarts, nothing is freed
        self.post("next step: tests, then the PR", auth=self.bot, reply=self.tid)
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})
        # silence after a nudge frees the thread
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid))
        db.conn().commit()
        progress.sweep()
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid))
        db.conn().commit()
        self.assertEqual(progress.sweep()["released"], [self.tid])
        self.assertIn("did not answer the nudge", self.messages(self.tid)[-1]["body"])
        self.assertIsNone(self.worker(self.tid))

    def age(self):
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid))
        db.conn().commit()

    def ask(self, needs="op", **meta):
        return self.client.post("/api/messages", headers=self.bot, json={
            "channel": "sg", "reply_to": self.tid, "body": "merge it?", "meta": {"needs": needs, "ask": "merge it?", **meta}}).json()

    def test_a_thread_waiting_on_a_human_is_neither_nudged_nor_freed_until_they_answer(self):
        quiet = {"released": [], "nudged": [], "attention": None}
        self.ask()
        self.age()
        self.assertEqual(progress.sweep(), quiet)
        self.assertEqual(self.worker(self.tid), "edie")
        # the human answers: the clock starts over, then it goes quiet like any thread
        self.post("merged", reply=self.tid)
        self.assertEqual(progress.sweep(), quiet)
        self.age()
        self.assertEqual(progress.sweep()["nudged"], [self.tid])

    def test_a_resolved_or_dismissed_ask_no_longer_holds_the_nudge(self):
        self.ask()
        self.client.post("/api/messages", headers=self.bot, json={
            "channel": "sg", "reply_to": self.tid, "body": "sorted", "meta": {"resolved": True}})
        self.age()
        self.assertEqual(progress.sweep()["nudged"], [self.tid])
        self.post("again", auth=self.bot, reply=self.tid)
        mid = self.ask()["message"]["id"]
        self.age()
        self.assertEqual(progress.sweep()["nudged"], [])
        r = self.client.post("/api/status/dismissed", headers=self.human, json={"key": f"m{mid}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(progress.sweep()["nudged"], [self.tid])

    def test_naming_a_bot_or_nobody_parks_nothing(self):
        for needs in ("edie", "nobody-at-all"):
            self.ask(needs)
            self.age()
            self.assertEqual(progress.sweep()["nudged"], [self.tid])
            self.post("next step", auth=self.bot, reply=self.tid)

    def test_a_human_naming_themselves_parks_nothing(self):
        self.client.post("/api/messages", headers=self.human, json={
            "channel": "sg", "reply_to": self.tid, "body": "note to self", "meta": {"needs": "op"}}).raise_for_status()
        self.age()
        self.assertEqual(progress.sweep()["nudged"], [self.tid])

    def test_a_stopped_worker_is_released_even_with_an_open_ask(self):
        self.ask()
        self.report("edie", "stopped")
        self.silent("edie")
        self.assertEqual(progress.sweep()["released"], [self.tid])

    def test_what_the_sweep_cannot_settle_goes_to_the_moderator(self):
        def age():
            db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid)); db.conn().commit()
        def post(mid):
            return self.client.get(f"/api/threads/{mid}").json()["messages"][0]
        age(); progress.sweep()                                            # the nudge
        age(); done = progress.sweep()                                     # released: nobody answered
        self.assertEqual(done["released"], [self.tid])
        # that is the moderator's: one thread in the admin channel (created with the first human)
        first = post(done["attention"])
        self.assertEqual((first["channel"], first["author"]), ("admin", "board"))
        self.assertTrue(first["body"].startswith("Needs a moderator: 1 thing the board could not settle."))
        self.assertIn(f"thread {self.tid} (build the thing): @edie did not answer the nudge; released", first["body"])
        self.assertEqual(first["meta"], {"kind": "attention", "threads": [self.tid], "bots": []})
        self.assertIsNone(progress.sweep()["attention"])                   # not again for the same state
        # still unclaimed hours later: that is a new state, reported once
        age(); done = progress.sweep()
        self.assertEqual(done["attention"], first["id"])                   # the open report takes it (#313)
        second = self.client.get(f"/api/threads/{first['id']}").json()["messages"][-1]
        self.assertIn("no bot picked it up", second["body"])
        self.assertIsNone(progress.sweep()["attention"])
        # the moderator, a bot of the admin workspace, is woken by it like any new thread
        mod = self.register("ada", "claude-code", "admin")
        self.assertEqual(self.claim(done["attention"], mod).status_code, 200)
        # once someone takes the stuck thread, nothing is left to report
        self.assertEqual(self.claim(self.tid).status_code, 200)
        self.assertIsNone(progress.sweep()["attention"])

    def test_a_bot_that_needs_a_person_is_reported_once_per_spell(self):
        def report(health):
            r = self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": [
                {"handle": "edie", "harness": "claude", "status": "waiting", "serves": "/pool", "pool": True,
                 "health": health}]})
            self.assertEqual(r.status_code, 200, r.text)
        def aged():
            db.conn().execute("UPDATE progress_health SET since=?", (LONG_AGO,)); db.conn().commit()
        report("logged out of claude: run claude login on the box")
        self.assertIsNone(progress.sweep()["attention"])                   # a login may be seconds away
        self.assertEqual(self.worker(self.tid), "edie")                    # its launcher holds the messages
        aged()
        done = progress.sweep()
        post = self.client.get(f"/api/threads/{done['attention']}").json()["messages"][0]
        self.assertEqual(post["channel"], "admin")
        self.assertIn("@edie on box has needed a login since 2026-01-01 00:00 UTC; its launcher says: logged out of claude",
                      post["body"])
        self.assertIn("Tell an operator, and mark the message so it waits for them.", post["body"])
        self.assertIn("For a bot: tell an operator what it needs", post["body"])
        self.assertEqual(post["meta"], {"kind": "attention", "threads": [], "bots": ["edie"]})
        self.assertIsNone(progress.sweep()["attention"])                   # once per spell
        report("")                                                         # logged in again
        self.assertIsNone(progress.sweep()["attention"])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM progress_health").fetchone()[0], 0)
        report("out of usage until 18:00")                                 # a new spell, a new state
        self.assertIsNone(progress.sweep()["attention"])                   # the spell starts now
        aged()
        done = progress.sweep()
        self.assertIn("has been out of usage since", self.client.get(f"/api/threads/{done['attention']}").json()["messages"][-1]["body"])

    def report_health(self, health):
        r = self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={"bots": [
            {"handle": "edie", "harness": "claude", "status": "waiting", "serves": "/pool", "pool": True, "health": health}]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_one_outage_that_flaps_is_one_report(self):
        # #313: a bot in and out of a login outage opened a thread for every spell
        def spell(day):
            self.report_health("logged out of claude: run claude login on the box")
            progress.sweep()
            db.conn().execute("UPDATE progress_health SET since=?", (f"2026-01-0{day}T00:00:00+00:00",)); db.conn().commit()
            return progress.sweep()["attention"]
        first = spell(1)
        self.assertIsNotNone(first)
        self.report_health(""); progress.sweep()
        self.assertIsNone(spell(2))                                        # back within a day: quiet
        # a day on it is news again, in a new thread if the old one was dealt with
        last = self.client.get(f"/api/threads/{first}").json()["messages"][-1]["id"]
        mod = self.register("ada", "claude-code", "admin")
        self.claim(first, mod).raise_for_status()
        self.client.post(f"/api/threads/{first}/complete", headers=mod, json={"through": last}).raise_for_status()
        db.conn().execute("UPDATE progress_attention SET at=?", (LONG_AGO,)); db.conn().commit()
        self.report_health(""); progress.sweep()
        again = spell(3)
        self.assertIsNotNone(again)
        self.assertNotEqual(again, first)

    def test_an_item_joining_a_report_nobody_holds_is_offered_again(self):
        # #315 review: a plain reply reaches no bot unless it is the worker
        report = lambda: progress.attend([("bot:x", "needs login:1", "- x")])
        tid = report()
        mod = self.register("ada", "claude-code", "admin")
        self.claim(tid, mod).raise_for_status()
        held = progress.attend([("bot:y", "needs login:1", "- y")])
        self.assertEqual(held, tid)
        last = lambda: self.client.get(f"/api/threads/{tid}").json()["messages"][-1]["meta"]
        self.assertNotIn("thread_reopened", last())                         # its worker has it
        self.client.delete(f"/api/threads/{tid}/worker", headers=mod).raise_for_status()
        self.assertEqual(progress.attend([("bot:z", "needs login:1", "- z")]), tid)
        self.assertTrue(last()["thread_reopened"])

    def test_an_old_open_report_does_not_take_new_items(self):
        self.report_health("logged out of claude: run claude login on the box")
        progress.sweep()
        db.conn().execute("UPDATE progress_health SET since=?", (LONG_AGO,)); db.conn().commit()
        first = progress.sweep()["attention"]
        db.conn().execute("UPDATE messages SET created_at=? WHERE id=?", (LONG_AGO, first)); db.conn().commit()
        self.report_health("out of usage until 18:00")
        progress.sweep()
        db.conn().execute("UPDATE progress_health SET since=?", (LONG_AGO,)); db.conn().commit()
        self.assertNotEqual(progress.sweep()["attention"], first)

    def test_the_moderators_own_report_is_not_reported_as_unclaimed(self):
        self.report_health("logged out of claude: run claude login on the box")
        progress.sweep()
        db.conn().execute("UPDATE progress_health SET since=?", (LONG_AGO,)); db.conn().commit()
        tid = progress.sweep()["attention"]
        mod = self.register("ada", "claude-code", "admin")
        self.claim(tid, mod).raise_for_status()
        self.assertTrue(progress.release(tid, "ada", "away"))
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, tid)); db.conn().commit()
        self.assertNotIn(tid, [t for t, _, _ in progress.unclaimed()])

    def test_a_completed_released_thread_is_not_reported_as_unclaimed(self):
        self.assertTrue(progress.release(self.tid, "edie", "away"))
        last = self.messages(self.tid)[-1]["id"]
        db.conn().execute("UPDATE messages SET created_at=? WHERE thread_id=?", (LONG_AGO, self.tid)); db.conn().commit()
        self.assertIn(self.tid, [t for t, _, _ in progress.unclaimed()])
        self.claim(self.tid).raise_for_status()
        self.client.post(f"/api/threads/{self.tid}/complete", headers=self.bot, json={"through": last}).raise_for_status()
        progress.release(self.tid, "edie", "away")
        self.assertNotIn(self.tid, [t for t, _, _ in progress.unclaimed()])

    def test_completed_threads_are_not_touched(self):
        last = self.messages(self.tid)[-1]["id"]
        self.client.post(f"/api/threads/{self.tid}/complete", headers=self.bot,
                         json={"through": last}).raise_for_status()
        self.report("edie", "stopped")
        self.silent("edie")
        self.assertEqual(progress.sweep(), {"released": [], "nudged": [], "attention": None})
        self.assertEqual(self.worker(self.tid), "edie")


if __name__ == "__main__":
    unittest.main()
