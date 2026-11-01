"""run_harness and classify_run against fake claude, codex, copilot and opencode binaries:
one case per outcome the launcher knows, with the output each real harness prints for it
(tests/fakes/harness.py). Run with python -m unittest discover -s tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import harness as fh      # noqa: E402

SOURCE = (ROOT / "bin/bot").read_text()
RUNNER = SOURCE[SOURCE.index("BOT_LIMIT_WAIT="):SOURCE.index("# ---- where a message")]
LIMIT = next(l for l in SOURCE.splitlines() if l.startswith("LIMIT_RE="))
HINT = SOURCE[SOURCE.index("login_hint() {"):SOURCE.index("\n}", SOURCE.index("login_hint() {")) + 2]
PRELUDE = """set -euo pipefail
STATE=$TEST_STATE; LOG=$STATE/log; : >> "$LOG"
HANDLE=tbot; TOKFILE=$STATE/token; echo tok > "$TOKFILE"; BOARD=http://board.invalid; WORKSPACE=ws
load_fleet_env() { :; }
log() { echo "$*" >&2; }
""" + LIMIT + "\n" + HINT + "\n" + RUNNER


class Classify(unittest.TestCase):
    def run_steps(self, harness, steps, resumed=False, health=None, flags=""):
        """Run run_harness once per step; return [(rc, outcome, detail, sid)] and the state dir."""
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = Path(tmp.name); state = home / "state"; state.mkdir()
        bindir, scenario = home / "bin", home / "scenario"
        fh.install(str(bindir), str(scenario)); (scenario / "login").write_text("")
        fh.plan(str(scenario), harness, steps)
        if resumed: (state / "session").write_text("old-session\n")
        if health: (state / "health").write_text(health)
        body = "\n".join('RUN_MESSAGES="7 8"; LAUNCHER_VERSION=v-test; rc=0\n'
                         'run_harness "$STATE" "$STATE/session" "prompt %d" || rc=$?\n'
                         'printf \'%%s\\t%%s\\t%%s\\t%%s\\n\' "$rc" "$OUTCOME" "$RUN_SID" "$DETAIL" >> "$STATE/results"' % i
                         for i in range(len(steps)))
        env = {**os.environ, "TEST_STATE": str(state), "HOME": tmp.name, "FAKE_SCENARIO_DIR": str(scenario),
               "PATH": str(bindir) + os.pathsep + os.environ["PATH"], "HARNESS": harness,
               "BOT_CLAUDE_FLAGS": flags, "BOT_CODEX_FLAGS": flags, "BOT_COPILOT_FLAGS": flags, "BOT_OPENCODE_FLAGS": flags}
        r = subprocess.run(["bash", "-c", PRELUDE + "\nHARNESS=" + harness + "\n" + body], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = [l.split("\t", 3) for l in (state / "results").read_text().splitlines()]
        return [(int(a), b, d, c) for a, b, c, d in rows], state, scenario

    EXPECT = {   # outcome -> (health line starts with, or None for no new line)
        "ok": None, "retry": None, "session_lost": None,
        "limited": "out of {h} usage since", "capacity": "out of {h} capacity since",
        "logged_out": "logged out of {h} since", "bad_invocation": "cannot run {h} since",
        "context_overflow": "last run overflowed its context at",
        "exhausted": "last run exhausted its turn or budget limit at", "unknown": "last run failed (exit",
    }
    CASES = {
        "claude": [("ok", "ok"), ("text_ok", "ok"), ("warned_overflow", "ok"), ("session_lost", "session_lost"), ("context_overflow", "context_overflow"), ("bad_invocation", "bad_invocation"),
                   ("logged_out", "logged_out"), ("logged_out_refresh", "logged_out"), ("limited", "limited"), ("limited_zero_exit", "limited"),
                   ("capacity", "capacity"), ("retry", "retry"), ("retry_network", "retry"),
                   ("exhausted", "exhausted"), ("exhausted_budget", "exhausted"), ("unknown", "unknown"),
                   ("unknown_no_result", "unknown")],
        "codex": [("ok", "ok"), ("quoted_limit", "ok"), ("session_lost", "session_lost"), ("context_overflow", "context_overflow"), ("recovered_overflow", "ok"), ("context_overflow_wording", "context_overflow"), ("warned_overflow", "ok"), ("bad_invocation", "bad_invocation"),
                  ("logged_out", "logged_out"), ("limited", "limited"), ("capacity", "capacity"), ("retry", "retry"),
                  ("exhausted", "exhausted"), ("unknown", "unknown")],
        "opencode": [("ok", "ok"), ("warned_overflow", "ok"), ("recovered_overflow", "ok"), ("session_lost", "session_lost"), ("context_overflow", "context_overflow"), ("context_overflow_wording", "context_overflow"), ("bad_invocation", "bad_invocation"),
                     ("limited", "limited"), ("retry", "retry"), ("unknown", "unknown")],
        "copilot": [("ok", "ok"), ("quoted_overflow", "ok"), ("session_lost", "session_lost"), ("context_overflow", "context_overflow"), ("bad_invocation", "bad_invocation"),
                    ("logged_out", "logged_out"), ("logged_out_token", "logged_out"), ("limited", "limited"), ("retry", "retry"), ("unknown", "unknown")],
    }

    def test_every_outcome_for_every_harness(self):
        for harness, cases in self.CASES.items():
            for scenario_name, expected in cases:
                with self.subTest(harness=harness, case=scenario_name):
                    resumed = scenario_name == "session_lost"
                    rows, state, _ = self.run_steps(harness, [fh.step(harness, scenario_name)],
                                                    resumed=resumed, health="previous line\n")
                    rc, outcome, detail, sid = rows[0]
                    self.assertEqual(outcome, expected, (rc, detail))
                    self.assertEqual(rc == 0, outcome == "ok", "rc is 0 exactly when the run completed")
                    want = self.EXPECT[expected]
                    health = (state / "health").read_text() if (state / "health").exists() else ""
                    if expected == "ok":
                        self.assertEqual(health, "", "ok clears the health line")
                        self.assertTrue((state / "session").read_text().strip(), "a session is kept after ok")
                    elif want is None:
                        self.assertEqual(health, "previous line\n", "%s leaves the health line alone" % expected)
                    else:
                        self.assertTrue(health.startswith(want.format(h=harness)), health)
                        if expected not in ("logged_out",):
                            self.assertIn(detail, health)
                    if expected != "ok" and not resumed:
                        self.assertFalse((state / "session").exists(), "no session is kept after a failed first run")
                    last = json.loads((state / "last.json").read_text())
                    self.assertEqual((last["outcome"], last["messages"], last["launcher"]), (expected, [7, 8], "v-test"))
                    self.assertEqual(last["detail"], detail)
                    self.assertEqual(len((state / "runs.jsonl").read_text().splitlines()), 1)

    def test_details_come_from_the_harness_not_the_reply(self):
        rows, state, _ = self.run_steps("claude", [fh.step("claude", "ok", "I hit a rate limit yesterday; fixed it")])
        self.assertEqual(rows[0][1], "ok")
        rows, state, _ = self.run_steps("claude", [fh.step("claude", "session_lost")], resumed=True)
        self.assertIn("No conversation found", rows[0][2])
        rows, state, _ = self.run_steps("codex", [fh.step("codex", "limited")])
        self.assertIn("exceeded your quota", rows[0][2])
        self.assertIn("exceeded your quota", (state / "health").read_text())
        rows, state, _ = self.run_steps("claude", [fh.step("claude", "limited_zero_exit")])
        self.assertIn("resets Oct 26", (state / "health").read_text(), "the zero-exit refusal keeps its words")
        # copilot's long reply mentioning a limit is a reply, not a limit
        long_reply = "\n".join(["Here is what I did.", "The rate limit note in the docs is fine.", "Done.", "Really."])
        rows, state, _ = self.run_steps("copilot", [{"stdout": long_reply + "\n", "stderr": "", "exit": 0}])
        self.assertEqual(rows[0][1], "ok")

    def test_log_is_a_transcript_and_the_result_object_stays_out_of_it(self):
        rows, state, _ = self.run_steps("claude", [fh.step("claude", "ok", "the reply text"), fh.step("claude", "retry_network")])
        log = (state / "log").read_text()
        self.assertIn("the reply text", log)
        self.assertNotIn('"type": "result"', log)
        self.assertIn("ENOTFOUND", log, "the error stream is in the log too")

    def test_sessions_are_resumed_with_the_flags_each_harness_takes(self):
        for harness, first, again in (("claude", "--session-id", "--resume"), ("copilot", "--session-id", "--resume"),
                                       ("opencode", None, "--session"), ("codex", None, "resume")):
            with self.subTest(harness=harness):
                rows, state, scenario = self.run_steps(harness, [fh.step(harness, "ok"), fh.step(harness, "ok")], flags="--flag-a")
                self.assertEqual([r[1] for r in rows], ["ok", "ok"])
                sid = (state / "session").read_text().strip()
                calls = [a for a in fh.runs(str(scenario), harness) if a[:2] not in (["auth", "status"], ["login", "status"])]
                self.assertEqual(len(calls), 2)
                if first: self.assertEqual(calls[0][calls[0].index(first) + 1], sid)
                self.assertEqual(calls[1][calls[1].index(again) + 1], sid)
                self.assertIn("--flag-a", calls[1])
                if harness == "claude": self.assertIn("--output-format", calls[0])
                if harness == "codex": self.assertLess(calls[1].index("--flag-a"), calls[1].index("resume"), "flags go before resume")

    def test_a_failed_first_run_keeps_no_session_and_the_next_is_a_first_run_again(self):
        rows, state, scenario = self.run_steps("claude", [fh.step("claude", "unknown"), fh.step("claude", "ok")])
        self.assertEqual([r[1] for r in rows], ["unknown", "ok"])
        calls = [a for a in fh.runs(str(scenario), "claude") if a[:2] != ["auth", "status"]]
        self.assertIn("--session-id", calls[1])
        self.assertEqual((state / "runs").read_text().strip(), "2")


if __name__ == "__main__":
    unittest.main()
