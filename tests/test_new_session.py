"""#221: a bot gets a new harness session, by hand (bot reset, or New session on /fleet through
its keeper) or because its session was started under an older first prompt.
Run with python -m unittest discover -s tests."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from fastapi.testclient import TestClient       # noqa: E402
from app import db, main                         # noqa: E402
from fakes import harness as fh                  # noqa: E402
from fakes.board import FakeBoard                # noqa: E402
from test_launcher_e2e import BOT, Launcher      # noqa: E402


def old_sessions(L):
    d = L.state / "old-sessions"
    return sorted(p.read_text().strip() for p in d.glob("*/session")) if d.exists() else []


class Launcher_(unittest.TestCase):
    def test_reset_gives_a_running_bot_a_new_session(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules read"), fh.step("claude", "ok", "rules read again"))
                L.start()
                L.wait_for(lambda: L.read("runs") == "1" and "startup catch-up" in L.log(), "the first turn")
                first = L.read("session")
                self.assertEqual(L.read("session-gen"), "3")
                r = subprocess.run([BOT, "reset", "tbot"], env=L.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("new session", r.stdout)
                L.wait_for(lambda: L.read("runs") == "2", "a new first turn")
                self.assertIn("This is your AGENTS.md", L.prompts()[1], "the new session reads the rules")
                self.assertNotEqual(L.read("session"), first)
                self.assertEqual(old_sessions(L), [first], "the old session is kept aside")
                self.assertIn("asked by bot reset", L.log())
                self.assertFalse((L.state / "reset").exists())
                L.stop()
            finally:
                L.close()

    def test_a_session_from_an_older_first_prompt_is_retired(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules read"), fh.step("claude", "ok", "rules read again"))
                L.start()
                L.wait_for(lambda: L.read("runs") == "1" and "startup catch-up" in L.log(), "the first turn")
                first = L.read("session")
                L.stop()
                (L.state / "session-gen").unlink()     # as every session started before #221
                L.start()
                L.wait_for(lambda: L.read("runs") == "2", "the first turn of a new session")
                self.assertIn("This is your AGENTS.md", L.prompts()[1])
                self.assertNotEqual(L.read("session"), first)
                self.assertIn("older first prompt", L.log())
                L.stop()
            finally:
                L.close()

    def test_a_stopped_bot_is_reset_now_and_once_per_request(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude")
            try:
                L.plan(fh.step("claude", "ok", "rules read"))
                L.start()
                L.wait_for(lambda: L.read("runs") == "1" and "startup catch-up" in L.log(), "the first turn")
                first = L.read("session")
                L.stop()
                reset = lambda: subprocess.run([BOT, "reset", "tbot", "--asked", "2026-10-21T22:00:00+00:00"],
                                               env=L.env, capture_output=True, text=True, timeout=10)
                self.assertIn("when it next starts", reset().stdout)
                self.assertEqual(L.read("session"), "")
                self.assertEqual(old_sessions(L), [first])
                (L.state / "session").write_text("kept\n")
                reset()     # the keeper passing the same request again
                self.assertEqual(L.read("session"), "kept", "a request is carried out once")
            finally:
                L.close()


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register('op', 'human')
        self.keeper = self.register('keeper-box', 'keeper')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post('/api/agents', json={'handle': handle, 'kind': kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['token']}

    def report(self, last_run=''):
        r = self.client.put('/api/fleet/hosts/box', headers=self.keeper, json={'bots': [
            {'handle': 'tarzan', 'harness': 'claude', 'status': 'waiting', 'pool': True, 'last_run': last_run}]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['want']

    def test_new_session_reaches_the_keeper_and_shows_until_the_next_run(self):
        self.report()
        page = self.client.get('/fleet').text
        self.assertIn('class="reset" data-handle="tarzan"', page)
        r = self.client.post('/api/fleet/bots/tarzan/reset', headers=self.human)
        self.assertEqual(r.status_code, 200, r.text)
        want = self.report()
        self.assertTrue(want[0]['reset'])
        self.assertTrue(want[0]['run'], 'a bot nobody had asked anything of stays as it is')
        self.assertIn('new session asked', self.client.get('/fleet').text)
        self.report(last_run='2999-01-01T00:00:00+00:00')     # it has run since
        self.assertNotIn('new session asked', self.client.get('/fleet').text)

    def test_the_keeper_passes_each_request_to_bin_bot_once(self):
        from test_fleet_kill import load_keeper
        home = os.environ.get('HOME'), os.environ.get('BOT_CONFIG')
        try:
            keeper = load_keeper(self.tmp.name)
        finally:
            for k, v in zip(('HOME', 'BOT_CONFIG'), home):
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
        hands = keeper.Hands()
        spawned = []
        hands.spawn = lambda h, cmd: spawned.append(cmd[1:])
        seen = [{'handle': 'tarzan', 'status': 'waiting'}]
        want = [{'handle': 'tarzan', 'harness': 'claude', 'run': True, 'reset': '2026-10-21T22:00:00Z', 'set_by': 'op',
                 'set_at': '2026-10-21T21:00:00Z', 'move_to': None, 'fetch': None}]
        hands.reconcile(want, seen)
        hands.reconcile(want, seen)     # the next report
        self.assertEqual(spawned, [['reset', 'tarzan', '--asked', '2026-10-21T22:00:00Z']])
        hands.reconcile([{**want[0], 'reset': '2026-10-21T23:00:00Z'}], seen)     # asked again later
        self.assertEqual(len(spawned), 2)

    def test_only_a_human_and_only_a_known_bot(self):
        self.report()
        self.assertEqual(self.client.post('/api/fleet/bots/tarzan/reset', headers=self.keeper).status_code, 403)
        self.assertEqual(self.client.post('/api/fleet/bots/nobody/reset', headers=self.human).status_code, 409)


if __name__ == "__main__":
    unittest.main()
