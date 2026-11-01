"""Skills are board-only (#328): a launcher writes its profile's skills from the board when
it starts, links them where the harnesses load skills from, and records when its session
began; when the board's skills revision moves, it writes them again and says so in the
session's next prompt, with no new session. Run with python -m unittest discover -s tests."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from fakes import harness as fh                  # noqa: E402
from fakes.board import FakeBoard                # noqa: E402
from test_launcher_e2e import Launcher           # noqa: E402
from test_fleet_kill import load_keeper          # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402
from app import db, fleet, main                  # noqa: E402

BOT = str(ROOT / "bin/bot")
HELLO = "---\nname: hello\ndescription: Says hello.\n---\n\nOne.\n"


class LauncherWritesProfileSkills(unittest.TestCase):
    def test_skills_are_written_from_the_board_at_start_and_the_session_start_is_recorded(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                home = Path(L.tmp.name)
                board.profile = {"profile": {"name": "dev"}, "skills": [
                    {"name": "hello", "mandatory": False, "description": "Says hello.", "revision": 1},
                    {"name": "gone", "mandatory": False, "description": "Not served.", "revision": 1}]}
                board.skills = {"hello": HELLO}
                board.skills_revision = "aaaa"
                # a link from before #328, into a clone the launcher made: replaced
                clone = home / ".config/bot_board/skills/acme/skills@main/hello"
                clone.mkdir(parents=True)
                (clone / "SKILL.md").write_text("old\n")
                (home / ".claude/skills").mkdir(parents=True)
                os.symlink(clone, home / ".claude/skills/hello")
                # a skill of that name the operator put there: left alone
                mine = home / "mine/hello"
                mine.mkdir(parents=True)
                (mine / "SKILL.md").write_text("mine\n")
                (home / ".agents/skills").mkdir(parents=True)
                os.symlink(mine, home / ".agents/skills/hello")
                L.plan(fh.step("claude", "ok", "read the rules"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1 and L.read("session-started"), "the first turn and session-started")
                self.assertRegex(L.read("session-started"), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                self.assertEqual(L.read("protocol"), "3")
                written = home / ".config/bot_board/skills/hello/SKILL.md"
                self.assertEqual(written.read_text(), HELLO)
                link = home / ".claude/skills/hello"
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.path.realpath(link), os.path.realpath(written.parent))
                self.assertEqual(os.path.realpath(home / ".agents/skills/hello"), os.path.realpath(mine))
                self.assertEqual(L.read("skills").split(), ["hello"])      # what keeps its files from gc
                self.assertEqual(L.read("skills-rev"), "aaaa")
                log = L.log()
                self.assertIn("skill hello: linked", log)
                self.assertIn("in place of an older link", log)
                self.assertIn("shadowed by", log)
                self.assertIn("skill gone: the board did not serve it", log)
                self.assertFalse((home / ".config/bot_board/skills/gone/SKILL.md").exists())
                L.stop()
            finally:
                L.close()

    def test_a_bot_without_a_profile_writes_nothing(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "read the rules"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
                self.assertEqual(L.read("skills"), "")
                self.assertFalse((Path(L.tmp.name) / ".claude/skills").exists())
                L.stop()
            finally:
                L.close()

    def test_a_skill_written_on_the_board_reaches_the_session_at_its_next_prompt(self):
        # #328: like agents.md (#241): the files are written again and one line in the prompt
        # says so, once per change; the session goes on. An old board says nothing.
        with FakeBoard() as board:
            board.profile = {"profile": {"name": "dev"}, "skills": [
                {"name": "hello", "mandatory": True, "description": "Says hello.", "revision": 1}]}
            board.skills = {"hello": HELLO}
            board.skills_revision = "r1"
            L = Launcher(board, "claude")
            try:
                home = Path(L.tmp.name)
                L.plan(*[fh.step("claude", "ok", "done")] * 4)
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1 and L.read("skills-rev") == "r1", "the first turn to record r1")
                board.post_root("ws-general", "first")
                L.wait_for(lambda: len(L.prompts()) >= 2, "the first wake")
                self.assertNotIn("skills changed", L.prompts()[1])
                board.skills["hello"] = HELLO.replace("One.", "Two.")
                board.skills_revision = "r2"
                board.post_root("ws-general", "second")
                L.wait_for(lambda: len(L.prompts()) >= 3, "the wake after the change")
                self.assertIn("Your profile's skills changed since you read them; read your profile again", L.prompts()[2])
                self.assertIn("Two.", (home / ".config/bot_board/skills/hello/SKILL.md").read_text())
                self.assertEqual(L.read("skills-rev"), "r2")
                board.skills_revision = ""
                board.post_root("ws-general", "third")
                L.wait_for(lambda: len(L.prompts()) >= 4, "a wake from a board that says nothing")
                self.assertNotIn("skills changed", L.prompts()[3])
                self.assertEqual(len(set(fh.runs(str(L.scenario), "claude")[-1]) & {"--resume"}), 1, "the same session")
                L.stop()
            finally:
                L.close()

    def test_bot_skill_is_gone(self):
        r = subprocess.run([BOT, "skill", "list"], capture_output=True, text=True,
                           env={**os.environ, "HOME": tempfile.mkdtemp(), "BOT_CONFIG": "/nonexistent"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("skills live on the board", r.stderr)


class KeeperReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("HOME"), os.environ.get("BOT_CONFIG")
        self.k = load_keeper(self.tmp.name)

    def tearDown(self):
        for k, v in zip(("HOME", "BOT_CONFIG"), self.saved):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_session_started_is_reported_and_bot_skills_is_not(self):
        state = Path(self.k.CFG) / "bots" / "ann"
        state.mkdir(parents=True)
        (state / "session-started").write_text("2026-10-24T10:00:00Z\n")
        self.k.out = lambda *cmd: "ann\tclaude\twaiting\t1\t\t\t\t\n" if cmd[1:2] == ("ps",) else ""
        self.k.git = lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "")
        self.assertEqual(self.k.bots()[0]["session_started"], "2026-10-24T10:00:00Z")
        cfg = Path(self.tmp.name) / "config"
        cfg.write_text('BOARD=http://x\nBOT_SKILLS="https://github.com/a/b/tree/main/skills/x"\n')
        os.environ["BOT_CONFIG"] = str(cfg)
        self.assertNotIn("bot_skills", self.k.snapshot())      # #328: the config line means nothing now


class BoardSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        r = self.client.post("/api/agents", json={"handle": "keeper-box", "kind": "keeper"})
        self.keeper = {"Authorization": "Bearer " + r.json()["token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def test_an_older_keepers_bot_skills_is_ignored(self):
        r = self.client.put("/api/fleet/hosts/box", headers=self.keeper, json={
            "bots": [{"handle": "ann", "status": "waiting", "harness": "claude", "protocol": 2,
                      "session_started": "2026-10-24T10:00:00Z"}],
            "bot_skills": ["https://github.com/a/b/tree/main/skills/x"]})
        self.assertEqual(r.status_code, 200, r.text)
        h = fleet.hosts()[0]
        self.assertEqual(h["bots"][0]["session_started"], "2026-10-24T10:00:00Z")
        self.assertNotIn("bot_skills", h)
        page = self.client.get("/fleet").text
        self.assertNotIn("still lists skills", page)
        self.assertIn("predates board-only skills (protocol 3)", page)


if __name__ == "__main__":
    unittest.main()
