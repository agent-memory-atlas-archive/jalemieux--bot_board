"""What bin/bot does after each kind of harness failure, end to end against the fake board
and fake harnesses (tests/fakes): the batch is held or given up, the thread claim is kept
or released, the health line says what is wrong, the harness is tried again when the
design says so. Run with python -m unittest discover -s tests."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import harness as fh                  # noqa: E402
from fakes.board import FakeBoard                # noqa: E402
from test_launcher_e2e import Launcher, BOT      # noqa: E402

FAST = {"BOT_LIMIT_WAIT": "2", "BOT_BAD_WAIT": "2", "BOT_RETRY_WAITS": "1 1", "BOT_UNKNOWN_WAIT": "1", "BOT_AUTH_WAIT": "1"}


class Outcomes(unittest.TestCase):
    def launch(self, board, harness, *steps, mention=False, before_start=None):
        """A launcher past its first turn, with one message posted for it. Returns (L, root)."""
        L = Launcher(board, harness, env=FAST)
        self.addCleanup(L.close)
        L.plan(fh.step(harness, "ok", "rules read"), *steps)
        if before_start: before_start(L)
        L.start()
        L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
        root = board.post_root("ws-general", "please fix the widget", mentions=["tbot"] if mention else ())
        return L, root

    def settled(self, L, prompts):
        """The harness has been prompted `prompts` times and the launcher has acted on the last run."""
        L.wait_for(lambda: len(L.prompts()) >= prompts and L.read("handled") == str(prompts),
                   "%d prompts and the launcher done with the last" % prompts, timeout=30)

    # ---- unknown: one more try, then the thread goes back --------------------------------
    def test_unknown_is_tried_once_more_then_released(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness), FakeBoard() as board:
                L, root = self.launch(board, harness, fh.step(harness, "unknown"), fh.step(harness, "unknown"))
                self.settled(L, 3)
                L.wait_for(lambda: board.worker(root) is None, "the claim to be released")
                self.assertTrue(L.read("health").startswith("last run failed (exit"), L.read("health"))
                self.assertIn("twice", L.read("health"))
                self.assertFalse((L.state / "held").exists(), "nothing is held once the batch is given up")
                self.assertFalse((L.state / "unknown").exists())
                self.assertEqual(len(board.seen("DELETE", "/api/threads/%d/worker" % root)), 1)
                self.assertIn("please fix the widget", L.prompts()[2], "the same batch was offered again")

    def test_unknown_then_ok_keeps_the_thread(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "claude", fh.step("claude", "unknown"), fh.step("claude", "ok", "done"))
            self.settled(L, 3)
            self.assertEqual(board.worker(root), "tbot")
            self.assertEqual(L.read("health"), "", "a run that completes clears the line")
            self.assertEqual(board.seen("DELETE", "/api/threads/"), [])

    # ---- retry: growing waits, then given up -------------------------------------------
    def test_transient_failures_are_retried_then_the_batch_goes_through(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness), FakeBoard() as board:
                L, root = self.launch(board, harness, fh.step(harness, "retry"), fh.step(harness, "retry"), fh.step(harness, "ok", "done"))
                self.settled(L, 4)
                self.assertEqual(board.worker(root), "tbot")
                self.assertEqual(L.read("health"), "")
                self.assertFalse((L.state / "retries").exists())
                self.assertEqual(board.by("tbot"), [], "no notice for a failure that passed")

    def test_transient_failures_beyond_the_waits_give_the_batch_up(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "claude", fh.step("claude", "retry"), fh.step("claude", "retry"), fh.step("claude", "retry"))
            self.settled(L, 4)
            L.wait_for(lambda: board.worker(root) is None, "the claim to be released")
            self.assertIn("after 3 tries", L.read("health"))

    # ---- session_lost: a new session, the same batch, once -----------------------------
    def test_a_lost_session_starts_a_new_one_and_runs_the_batch_again(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness), FakeBoard() as board:
                L, root = self.launch(board, harness, fh.step(harness, "session_lost"), fh.step(harness, "ok", "done", new_session="fresh-2"))
                old = L.read("session")
                self.settled(L, 3)
                self.assertTrue(L.prompts()[2].startswith("Read "), "the new session reads the rules first")
                self.assertIn("please fix the widget", L.prompts()[2])
                self.assertNotEqual(L.read("session"), old)
                self.assertTrue(L.read("session"))
                self.assertEqual(board.worker(root), "tbot")
                self.assertEqual(L.read("health"), "")
                self.assertIn("lost the session", L.log())

    def test_a_session_lost_twice_is_treated_as_unknown(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "codex", fh.step("codex", "session_lost"), fh.step("codex", "session_lost"))
            self.settled(L, 3)
            self.assertTrue((L.state / "held").exists() and (L.state / "unknown").exists(), "held for one more try")

    # ---- context_overflow: the session is set aside, the same batch runs in a new one, once --
    def test_an_overflowed_session_is_retired_and_the_batch_runs_in_a_new_one(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness), FakeBoard() as board:
                L, root = self.launch(board, harness, fh.step(harness, "context_overflow"), fh.step(harness, "ok", "done", new_session="fresh-2"))
                L.wait_for(lambda: L.read("session"), "the first turn's session")
                old = L.read("session")
                self.settled(L, 3)
                self.assertTrue(L.prompts()[2].startswith("Read "), "the new session reads the rules first")
                self.assertIn("please fix the widget", L.prompts()[2])
                self.assertNotEqual(L.read("session"), old)
                self.assertEqual([p.read_text() for p in (L.state / "old-sessions").glob("*/session")], [old + "\n"])
                self.assertEqual(board.worker(root), "tbot")
                self.assertEqual(L.read("health"), "")
                self.assertIn("overflowed its context", L.log())

    def test_an_overflow_in_the_new_session_is_held_once_then_released(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "opencode", *[fh.step("opencode", "context_overflow")] * 3)
            self.settled(L, 4)
            L.wait_for(lambda: board.worker(root) is None, "the claim to be released")
            self.assertEqual(len(list((L.state / "old-sessions").glob("*/session"))), 1, "only the session that grew is retired")
            self.assertIn("Context size has been exceeded", L.read("health"))

    # ---- exhausted: over, unfinished, released -------------------------------------------
    def test_a_run_at_its_turn_limit_releases_the_thread(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), FakeBoard() as board:
                L, root = self.launch(board, harness, fh.step(harness, "exhausted"), mention=True)
                self.settled(L, 2)
                self.assertTrue(L.read("health").startswith("last run exhausted"), L.read("health"))
                notices = board.by("tbot")
                self.assertEqual(len(notices), 1)
                self.assertEqual(board.seen("DELETE", "/api/threads/"), [], "a mention was never ours to release")
                self.assertIn("turn or budget limit", notices[0]["body"])
                self.assertEqual(notices[0]["meta"].get("launcher"), "bin/bot")

    # ---- limited, capacity, logged out, cannot run: held, then tried again ---------------
    def check_outage(self, harness, scenario, health_starts, notice_words, mention=True, after=None):
        with FakeBoard() as board:
            L, root = self.launch(board, harness, fh.step(harness, scenario), fh.step(harness, "ok", "done"), mention=mention)
            self.settled(L, 2)
            self.assertTrue(L.read("health").startswith(health_starts.format(h=harness)), L.read("health"))
            self.assertTrue((L.state / "held").exists(), "the batch is held")
            if mention:
                L.wait_for(lambda: len(board.by("tbot")) == 1, "one notice")
                self.assertIn(notice_words, board.by("tbot")[0]["body"])
            if after: after(L)
            self.settled(L, 3)
            self.assertIn("please fix the widget", L.prompts()[2], "the held batch went through")
            self.assertEqual(L.read("health"), "")
            self.assertFalse((L.state / "held").exists())
            self.assertEqual(len(board.by("tbot")), 1 if mention else 0, "no second notice")

    def test_a_usage_limit_holds_the_batch_and_tries_again_later(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness):
                self.check_outage(harness, "limited", "out of {h} usage since", "temporarily unavailable")

    def test_a_capacity_outage_holds_the_batch_and_tries_again_later(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                self.check_outage(harness, "capacity", "out of {h} capacity since", "temporarily unavailable")

    def test_a_harness_that_cannot_be_run_holds_everything(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness):
                self.check_outage(harness, "bad_invocation", "cannot run {h} since", "cannot be run", mention=harness == "claude")

    def test_logged_out_by_a_runs_output_is_tried_again_later(self):
        # copilot has no auth check: the run's own words are all there is
        self.check_outage("copilot", "logged_out", "logged out of {h} since", "logged out")

    def test_logged_out_waits_for_the_login_then_resumes(self):
        # logged_out_refresh: the stored login's refresh token has expired, so claude prints one line and no
        # result object (#295); it must be held for a login like any other logged-out run, not given up
        for harness, scenario in (("claude", "logged_out"), ("claude", "logged_out_refresh"), ("codex", "logged_out")):
            with self.subTest(harness=harness, scenario=scenario):
                def log_back_in(L):
                    (L.scenario / "login").write_text("")
                with FakeBoard() as board:
                    L, root = self.launch(board, harness, fh.step(harness, scenario), fh.step(harness, "ok", "done"),
                                          mention=True, before_start=lambda L: (L.scenario / "login").unlink())
                    self.settled(L, 2)
                    self.assertTrue(L.read("health").startswith("logged out of %s since" % harness), L.read("health"))
                    L.wait_for(lambda: len(board.by("tbot")) == 1, "one notice")
                    self.assertIn("logged out", board.by("tbot")[0]["body"])
                    import time; time.sleep(3)
                    self.assertEqual(len(L.prompts()), 2, "nothing is prompted while the login is missing")
                    log_back_in(L)
                    self.settled(L, 3)
                    self.assertIn("please fix the widget", L.prompts()[2])
                    self.assertEqual(L.read("health"), "")

    def test_a_first_turn_out_of_usage_waits_then_works(self):
        with FakeBoard() as board:
            L = Launcher(board, "claude", env=FAST); self.addCleanup(L.close)
            L.plan(fh.step("claude", "limited"), fh.step("claude", "ok", "done"))
            root = board.post_root("ws-general", "please fix the widget")
            L.start()
            L.wait_for(lambda: L.read("health").startswith("out of claude usage"), "the first turn's outage")
            self.settled(L, 2)
            self.assertTrue(L.prompts()[1].startswith("Read "), "a session that never read the rules reads them first")
            self.assertIn("please fix the widget", L.prompts()[1])
            self.assertEqual(board.worker(root), "tbot")
            self.assertEqual(L.read("health"), "")

    # ---- what the box can see --------------------------------------------------------
    def test_ps_shows_the_last_outcome_and_the_launcher_version_is_kept(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "claude", fh.step("claude", "unknown"))
            self.settled(L, 2)
            r = subprocess.run([BOT, "ps", "-a"], env=L.env, capture_output=True, text=True)
            self.assertIn("LAST", r.stdout.splitlines()[0])
            self.assertIn("unknown", r.stdout)
            tsv = subprocess.run([BOT, "ps", "-a", "--tsv"], env=L.env, capture_output=True, text=True).stdout
            fields = tsv.strip().split("\t")
            self.assertEqual(fields[0], "tbot")
            self.assertTrue(fields[6].startswith("unknown"), fields)
            self.assertTrue(fields[7].startswith("last run failed"), fields)
            self.assertTrue(L.read("version"))

    def test_stop_while_a_batch_is_held_keeps_it_for_the_next_start(self):
        with FakeBoard() as board:
            L, root = self.launch(board, "claude", fh.step("claude", "limited"))
            self.settled(L, 2)
            r = L.stop()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((L.state / "held").exists())
            self.assertTrue(L.read("health").startswith("out of claude usage"))


if __name__ == "__main__":
    unittest.main()
