"""bin/bot end to end against a fake board and fake harnesses (tests/fakes): the launcher
starts, registers, claims a thread, prompts the harness and stops. Run with
python -m unittest discover -s tests."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import harness as fh                  # noqa: E402
from fakes.board import FakeBoard                # noqa: E402

BOT = str(ROOT / "bin/bot")


class Launcher:
    """One bin/bot run in a scratch home, against a FakeBoard, with fake harnesses on PATH."""

    def __init__(self, board, harness="claude", name="tbot", env=None):
        # a child still exiting may write into the fake HOME while it is removed (macOS's
        # python3 caches bytecode under ~/Library/Caches): not a failure of the test
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        home = Path(self.tmp.name)
        self.bin = home / "fakebin"; self.scenario = home / "scenario"
        fh.install(str(self.bin), str(self.scenario))
        (self.scenario / "login").write_text("")          # logged in unless a test says otherwise
        self.state = home / ".config/bot_board/bots" / name
        self.harness, self.name, self.board = harness, name, board
        self.env = {**os.environ, "HOME": self.tmp.name, "BOT_CONFIG": str(home / "missing"), "PYTHONDONTWRITEBYTECODE": "1",
                    "BOARD": board.url, "BOT_WAIT": "1", "BOT_POOL": str(home / "work"),
                    "FAKE_SCENARIO_DIR": str(self.scenario),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], **(env or {})}
        for k in ("BOT_CLAUDE_FLAGS", "BOT_CODEX_FLAGS", "BOT_COPILOT_FLAGS", "BOT_OPENCODE_FLAGS"):
            self.env.pop(k, None) if not (env and k in env) else None
        self.proc = None

    def plan(self, *steps):
        fh.plan(str(self.scenario), self.harness, list(steps))

    def start(self, *args):
        self.out = open(Path(self.tmp.name) / "launcher.out", "w+")
        self.proc = subprocess.Popen([BOT, self.harness, "--workspace", "ws", "--name", self.name, *args],
                                     env=self.env, stdout=self.out, stderr=subprocess.STDOUT)
        return self

    def wait_for(self, cond, what, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None and not cond():
                raise AssertionError("launcher exited %s before %s:\n%s" % (self.proc.returncode, what, self.output()))
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for %s:\n%s\nlog:\n%s" % (what, self.output(), self.log()))

    def prompts(self):
        return fh.prompts(str(self.scenario), self.harness)

    def runs(self):
        return [a for a in fh.runs(str(self.scenario), self.harness)
                if a[:2] not in (["auth", "status"], ["login", "status"])]

    def output(self):
        self.out.flush(); return Path(self.out.name).read_text()

    def log(self):
        try:
            return (self.state / "log").read_text()
        except OSError:
            return ""

    def read(self, name, default=""):
        try:
            return (self.state / name).read_text().strip()
        except OSError:
            return default

    def stop(self, timeout=15):
        if self.proc and self.proc.poll() is None:
            r = subprocess.run([BOT, "stop", self.name], env=self.env, capture_output=True, text=True, timeout=timeout)
            self.proc.wait(timeout=5)
            return r
        return None

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill(); self.proc.wait(timeout=5)
        finally:
            self.out.close(); self.tmp.cleanup()


class StartsAndWorks(unittest.TestCase):
    """The fixtures against bin/bot as it is: a claimed thread reaches the harness."""

    def run_one(self, harness):
        with FakeBoard() as board:
            L = Launcher(board, harness)
            try:
                L.plan(fh.step(harness, "ok", "read the rules"), fh.step(harness, "ok", "done"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
                self.assertIn("This is your AGENTS.md", L.prompts()[0])
                self.assertIn("the rules", L.prompts()[0])
                root = board.post_root("ws-general", "please look at issue 12")
                L.wait_for(lambda: len(L.prompts()) >= 2, "the work prompt")
                self.assertIn("please look at issue 12", L.prompts()[1])
                self.assertIn("new thread: message %d" % root, L.prompts()[1])
                self.assertEqual(board.worker(root), "tbot")
                L.wait_for(lambda: not (L.state / "busy").exists() and L.read("runs") == "2", "the run to end")
                self.assertTrue(L.read("session"), "a session id is kept after a successful run")
                r = L.stop()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(L.proc.returncode, 0, L.output())
            finally:
                L.close()

    def test_claude(self): self.run_one("claude")
    def test_codex(self): self.run_one("codex")
    def test_copilot(self): self.run_one("copilot")
    def test_opencode(self): self.run_one("opencode")


if __name__ == "__main__":
    unittest.main()


class Restart(unittest.TestCase):
    """bot restart: the launcher starts itself again on the bin/bot on disk, as the same bot."""

    def test_restart_keeps_pid_token_session_and_work(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules read"), fh.step("claude", "ok", "done"), fh.step("claude", "ok", "done again"))
                L.start()
                L.wait_for(lambda: L.read("runs") == "1" and "startup catch-up" in L.log(), "the first turn and the loop")
                pid, session = L.read("pid"), L.read("session")
                self.assertTrue(session)
                r = subprocess.run([BOT, "restart", "tbot"], env=L.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("restarting", r.stdout)
                L.wait_for(lambda: "restarting on" in L.log() and L.log().count("standing by") == 2, "the launcher to start again")
                self.assertEqual(L.read("pid"), pid, "the same process")
                self.assertEqual(L.proc.poll(), None)
                self.assertEqual(L.read("session"), session, "the same session")
                self.assertFalse((L.state / "restart").exists())
                root = board.post_root("ws-general", "after the restart")
                L.wait_for(lambda: len(L.prompts()) >= 2, "work after the restart")
                self.assertIn("after the restart", L.prompts()[1])
                self.assertIn("--resume", fh.runs(str(L.scenario), "claude")[-1], "the session was resumed, not recreated")
                self.assertEqual(board.worker(root), "tbot")
                self.assertEqual(len(board.seen("POST", "/api/agents")), 1, "no second registration")
                r = L.stop()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            finally:
                L.close()

    def test_restart_reads_rotated_fleet_secrets(self):
        # #297: the secrets a launcher exported from fleet.env must not survive its self-exec, or the
        # next process takes them for the operator's own and never sees a rotation of them again
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                fleet_env = Path(L.tmp.name) / ".config/bot_board/fleet.env"
                fleet_env.parent.mkdir(parents=True, exist_ok=True)
                fleet_env.write_text("ROTATED_TOKEN=one\n")
                L.plan(fh.step("claude", "ok", "rules read"), fh.step("claude", "ok", "done"))
                L.start()
                L.wait_for(lambda: L.read("runs") == "1" and "startup catch-up" in L.log(), "the first turn and the loop")
                self.assertEqual(fh.secrets(str(L.scenario), "claude")[-1].get("ROTATED_TOKEN"), "one")
                r = subprocess.run([BOT, "restart", "tbot"], env=L.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                L.wait_for(lambda: "restarting on" in L.log() and L.log().count("standing by") == 2, "the launcher to start again")
                fleet_env.write_text("ROTATED_TOKEN=two\n")
                board.post_root("ws-general", "after the rotation")
                L.wait_for(lambda: len(L.prompts()) >= 2, "work after the restart")
                self.assertEqual(fh.secrets(str(L.scenario), "claude")[-1].get("ROTATED_TOKEN"), "two",
                                 "the restarted launcher reads fleet.env's rotated value")
                r = L.stop()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            finally:
                L.close()

    def test_restart_waits_for_a_busy_run(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules read"), fh.step("claude", "ok", "slow", sleep=3), fh.step("claude", "ok"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
                board.post_root("ws-general", "take your time")
                L.wait_for(lambda: len(L.prompts()) >= 2 and (L.state / "busy").exists(), "the slow run to start")
                r = subprocess.run([BOT, "restart", "tbot"], env=L.env, capture_output=True, text=True, timeout=10)
                self.assertIn("will restart when its run", r.stdout)
                L.wait_for(lambda: L.read("runs") == "2" and L.log().count("standing by") == 2, "the run to end and the restart to follow")
                self.assertEqual(L.read("handled"), "2", "the finished run was acted on before the restart")
                L.stop()
            finally:
                L.close()


class Mentions(unittest.TestCase):
    def test_a_thread_for_a_stopped_agent_only_is_offered_for_pickup(self):
        # #216: a mention of someone who will never read it does not keep the thread from others
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules"), fh.step("claude", "ok", "done"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
                board.post_root("ws-general", "@milo please look", mentions=["milo"])        # milo will read it
                board.post_root("ws-general", "@finn please look", mentions=["finn"], unreachable=["finn"])
                L.wait_for(lambda: len(L.prompts()) >= 2, "the pickup")
                self.assertIn("@finn please look", L.prompts()[1])
                self.assertNotIn("@milo please look", L.prompts()[1])
            finally:
                L.close()


class RoutineItems(unittest.TestCase):
    def test_one_routine_announcement_is_claimed_per_wake(self):
        # #234: a catch-up batch claimed every announced issue at once and sat on them
        with FakeBoard() as board:
            item = lambda n: board.post_root("ws-general", "New issue to take: #%d" % n, author="routines",
                                             meta={"routine": 1, "routine_item": {"repo": "o/r", "number": n}})
            items = [item(n) for n in (1, 2, 3)]
            plain = board.post_root("ws-general", "a plain question")
            L = Launcher(board, "claude")
            try:
                L.plan(*[fh.step("claude", "ok", "done")] * 4)
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 2, "the catch-up batch")
                self.assertIn("a plain question", L.prompts()[1])
                self.assertEqual(board.worker(plain), "tbot", "other new threads are not limited")
                mine = [m for m in items if board.worker(m) == "tbot"]
                self.assertEqual(len(mine), 1, "one announcement per wake")
                left = [m for m in items if m not in mine]
                with board.lock:
                    board.workers[left[0]] = "other"      # another bot takes one meanwhile
                L.wait_for(lambda: len(L.prompts()) >= 3, "the next announcement")
                self.assertIn("New issue to take", L.prompts()[2])
                self.assertEqual((board.worker(left[0]), board.worker(left[1])), ("other", "tbot"))
                time.sleep(2.5)
                self.assertEqual(len(L.prompts()), 3, "the one another bot took is skipped")
                L.stop()
            finally:
                L.close()


class AgentsGuide(unittest.TestCase):
    def test_a_changed_agents_md_is_read_again_without_a_new_session(self):
        # #241: one line in the next wake prompt, once per change; an old board says nothing
        with FakeBoard() as board:
            board.agents_rev = "rev1"
            L = Launcher(board, "claude")
            try:
                L.plan(*[fh.step("claude", "ok", "done")] * 4)
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1 and L.read("agents-rev") == "rev1", "the first turn to record rev1")
                board.post_root("ws-general", "first")
                L.wait_for(lambda: len(L.prompts()) >= 2, "the first wake")
                self.assertNotIn("agents.md changed", L.prompts()[1])
                board.agents_rev = "rev2"
                board.post_root("ws-general", "second")
                L.wait_for(lambda: len(L.prompts()) >= 3, "the wake after the change")
                self.assertIn("Your AGENTS.md changed since you read it; this is the current one", L.prompts()[2])
                self.assertIn("the rules", L.prompts()[2])       # the new text itself, ahead of the messages
                self.assertLess(L.prompts()[2].index("the rules"), L.prompts()[2].index("second"))
                L.wait_for(lambda: L.read("agents-rev") == "rev2", "rev2 recorded")
                board.agents_rev = ""
                board.post_root("ws-general", "third")
                L.wait_for(lambda: len(L.prompts()) >= 4, "a wake from a board that says nothing")
                self.assertNotIn("agents.md changed", L.prompts()[3])
                self.assertEqual(len(set(fh.runs(str(L.scenario), "claude")[-1]) & {"--resume"}), 1, "the same session")
                L.stop()
            finally:
                L.close()
