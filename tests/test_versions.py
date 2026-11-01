"""#239: the Versions section on /fleet compares what runs with what should, and the bell lists
what only a person can fix. Run with python -m unittest discover -s tests."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient     # noqa: E402
from app import db, main, status, versions, web     # noqa: E402

LOADED = "a" * 40


def keeper(**k):
    return {"loaded": LOADED, "started": "2026-10-22T00:00:00+00:00", "protocol": 1, **k}


def bot(handle, **extra):
    return {"handle": handle, "harness": "claude", "status": "waiting", "launcher": "v1.0.1",
            "behind": False, "restartable": True, "protocol": versions.LAUNCHER_PROTOCOL,
            "session_gen": str(versions.SESSION_GEN), **extra}


def host(name, bots=(), **extra):
    return {"name": name, "reporting": True, "reported_at": db.now(), "bots": list(bots),
            "update": {"revision": "v1.0.1", "result": "up to date"}, "keeper": keeper(), **extra}


class Rows(unittest.TestCase):
    def setUp(self):
        self.saved = versions.BOARD_COMMIT
        versions.BOARD_COMMIT = LOADED

    def tearDown(self):
        versions.BOARD_COMMIT = self.saved

    def by(self, rows, what, name):
        return next(r for r in rows if r["what"] == what and r["name"] == name)

    def test_session_gen_is_read_from_the_shipped_bin_bot(self):
        self.assertIsInstance(versions.SESSION_GEN, int)

    def test_current_pieces_have_no_note(self):
        rows = versions.rows([host("box", [bot("ann")])])
        self.assertTrue(all(r["current"] for r in rows), rows)
        self.assertEqual(versions.attention(rows), [])

    def test_each_kind_of_stale_is_said_and_only_a_person_fix_rings(self):
        rows = versions.rows([
            host("old", update=None, keeper=None),                          # before #107
            host("pre238", keeper="keeper-pre238"),                          # self-updates, no loaded yet
            host("ahead", keeper=keeper(loaded="b" * 40)),
            host("box", [bot("ann", restartable=False, launcher=""), bot("bob", behind=True),
                         bot("cal", session_gen="1"), bot("dot", status="stopped", behind=True)]),
        ], [{"id": 2, "name": "issues", "recipe_version": 1}])
        self.assertIn("predates self-update", self.by(rows, "keeper", "old")["note"])
        self.assertIn("bot keeper install", self.by(rows, "keeper", "old")["note"])
        self.assertIn("before #238", self.by(rows, "keeper", "pre238")["note"])
        self.assertIn("not the board's commit", self.by(rows, "keeper", "ahead")["note"])
        self.assertIn("predates bot restart", self.by(rows, "launcher", "ann")["note"])
        self.assertEqual(self.by(rows, "launcher", "ann")["running"], "no version file")
        self.assertIn("behind", self.by(rows, "launcher", "bob")["note"])
        self.assertIn("older first prompt", self.by(rows, "session", "cal")["note"])
        self.assertFalse([r for r in rows if r["name"] == "dot"], "a stopped bot runs nothing")
        self.assertIn("older recipe", self.by(rows, "routine", "issues")["note"])
        self.assertEqual([t for t, _ in versions.attention(rows)],
                         ["keeper on old: predates self-update (#107): on old, run `"
                          + versions.FIX_KEEPER + "`"])

    def test_a_keeper_below_min_protocol_rings(self):
        rows = versions.rows([host("box", keeper=keeper(protocol=0))])
        self.assertEqual(len(versions.attention(rows)), 1)

    def test_section_lists_stale_bots_and_counts_the_rest(self):
        rows = versions.rows([host("box", [bot("ann"), bot("bob", behind=True)])])
        html = web._versions_section(rows)
        self.assertIn('id="fleet-versions"', html)
        self.assertIn("1 of 2 running bot(s) on the current launcher and session", html)
        self.assertIn(">bob</a>", html)
        self.assertNotIn(">ann</a>", html)


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, 'board.db')
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        r = self.client.post('/api/agents', json={'handle': 'keeper-box', 'kind': 'keeper'})
        self.keeper = {'Authorization': 'Bearer ' + r.json()['token']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
        self.tmp.cleanup()

    def test_report_reply_publishes_min_protocol_and_a_pre_107_keeper_reaches_the_bell(self):
        r = self.client.put('/api/fleet/hosts/box', headers=self.keeper, json={'bots': []})
        self.assertEqual(r.json()['min_protocol'], versions.MIN_PROTOCOL)
        texts = [a['text'] for a in status.rollup()['attention']]
        self.assertTrue(any('predates self-update' in t for t in texts), texts)
        self.assertIn('id="fleet-versions"', self.client.get('/fleet').text)


if __name__ == "__main__":
    unittest.main()
