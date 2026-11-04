"""bin/bot's container runner (#306/#477). With BOT_RUNTIME set, every harness turn and the
run-in-container subcommand go through the runtime, with the checkout bind-mounted at /work
and the state volume at /home/bot. These tests stand a fake runtime in for docker (RUNTIME,
below): it records its argv, then runs the wrapped command with cwd set to the /work source,
so the dispatch, the empty-checkout guard and the (fake) harness all behave as in a container.
Nothing here needs docker. Run with python -m unittest discover -s tests."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import harness as fh                  # noqa: E402
from fakes.board import FakeBoard                # noqa: E402
from test_launcher_e2e import Launcher, BOT      # noqa: E402

# A stand-in for docker/the box runtime. It records its argv (so a test can check the mounts
# and the image), then "runs the container": it finds the -v SRC:/work bind-mount and the
# command after the image, rewrites the empty-checkout guard's literal /work to SRC, and runs
# the command with cwd=SRC. So an empty checkout still trips the guard (exit 97) and a
# non-empty one runs the real (fake) harness, exactly as a container would.
RUNTIME = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
log = os.environ.get("FAKE_RUNTIME_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(argv) + "\n")
if not argv or argv[0] != "run":
    sys.stderr.write("fake runtime: expected 'run', got %r\n" % (argv,)); sys.exit(2)
takes_value = {"-e", "--user", "-w", "-v", "--env-file", "--name"}
i, mounts, workdir, image, cmd = 1, [], None, None, None
while i < len(argv):
    a = argv[i]
    if a == "-v":
        src, _, dest = argv[i + 1].partition(":")
        dest = dest.split(":")[0]
        mounts.append((src, dest)); i += 2; continue
    if a == "-w":
        workdir = argv[i + 1]; i += 2; continue
    if a in takes_value:
        i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    image = a; cmd = argv[i + 1:]; break
if image is None:
    sys.stderr.write("fake runtime: no image in %r\n" % (argv,)); sys.exit(2)
work_src = next((s for s, d in mounts if d == "/work"), None)
cwd = work_src if workdir == "/work" and work_src else None
# bot mode wraps the command as: sh -c '<guard>' sh <harness> <args>
if len(cmd) >= 4 and cmd[0] == "sh" and cmd[1] == "-c" and cmd[3] == "sh":
    guard = cmd[2].replace("/work", work_src or "/work")
    cmd = ["sh", "-c", guard, "sh"] + cmd[4:]
sys.exit(subprocess.run(cmd, cwd=cwd).returncode)
'''

RESUME_FLAG = {"claude": "--resume", "copilot": "--resume", "codex": "resume", "opencode": "--session"}


class ContainerRun(unittest.TestCase):
    def setUp(self):
        self.rt = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.rt, ignore_errors=True)
        self.runtime = Path(self.rt) / "fakeruntime"
        self.runtime.write_text(RUNTIME)
        self.runtime.chmod(self.runtime.stat().st_mode | stat.S_IEXEC)
        self.log = Path(self.rt) / "runtime.log"
        self.log.write_text("")

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    # ---- every harness turn goes through the runtime, with resume intact ------------------
    def test_each_harness_runs_in_the_container_and_resumes(self):
        for harness in ("claude", "codex", "opencode", "copilot"):
            with self.subTest(harness=harness), FakeBoard() as board:
                self.log.write_text("")
                L = Launcher(board, harness, env={"BOT_RUNTIME": str(self.runtime),
                                                  "FAKE_RUNTIME_LOG": str(self.log)})
                self.addCleanup(L.close)
                # a real checkout has files; seed one so the empty-/work guard passes
                checkout = Path(L.tmp.name) / "work" / L.name
                checkout.mkdir(parents=True, exist_ok=True)
                (checkout / ".keep").write_text("a non-empty checkout")
                L.plan(fh.step(harness, "ok", "rules read"),
                       fh.step(harness, "ok", "done"),
                       fh.step(harness, "ok", "again"))
                L.start()
                L.wait_for(lambda: len(L.prompts()) >= 1, "the first turn")
                root = board.post_root("ws-general", "please fix the widget")
                L.wait_for(lambda: len(L.prompts()) >= 2, "the work turn")
                L.wait_for(lambda: L.read("session"), "a session id kept")
                sid = L.read("session")
                board.reply(root, "and the other thing too")
                L.wait_for(lambda: len(L.prompts()) >= 3, "the resume turn")
                L.wait_for(lambda: not (L.state / "busy").exists() and L.read("runs") == "3",
                           "all three runs done")

                calls = self.calls()
                self.assertTrue(calls, "the runtime was invoked for %s" % harness)
                self.assertTrue(all(c and c[0] == "run" for c in calls), calls)
                img = "bot_board-harness-%s:latest" % harness
                self.assertTrue(all(any(img in a for a in c) for c in calls),
                                "every run used the %s image: %s" % (harness, calls))
                self.assertEqual(L.read("health"), "", "every turn completed in the container")
                self.assertIn("please fix the widget", "\n".join(L.prompts()),
                              "the prompt reached the harness through the container")
                flag = RESUME_FLAG[harness]
                resumed = [r for r in L.runs() if flag in r and r[r.index(flag) + 1] == sid]
                self.assertTrue(resumed, "a %s resume turn carried the kept session id %s: %s"
                                % (harness, sid, L.runs()))

    # ---- the opencode model and config cross into the container (#324 + #477) -------------
    def test_opencode_model_and_config_cross_into_the_container(self):
        with FakeBoard() as board:
            self.log.write_text("")
            L = Launcher(board, "opencode", env={"BOT_RUNTIME": str(self.runtime),
                                                 "FAKE_RUNTIME_LOG": str(self.log)})
            self.addCleanup(L.close)
            checkout = Path(L.tmp.name) / "work" / L.name
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / ".keep").write_text("a non-empty checkout")
            cfg = Path(L.tmp.name) / "local.json"
            cfg.write_text('{"provider": {"llama.cpp": {}}}')
            L.plan(fh.step("opencode", "ok", "rules read"), fh.step("opencode", "ok", "done"))
            L.start("--model", "llama.cpp/qwen", "--opencode-config", str(cfg))
            L.wait_for(lambda: L.read("runs") == "1", "the first turn")
            oc = [a for a in L.runs() if a[:1] == ["run"]]
            self.assertTrue(oc, "an opencode run reached the harness: %s" % L.runs())
            self.assertEqual(oc[0][oc[0].index("--model") + 1], "llama.cpp/qwen",
                             "the model crossed as an argument: %s" % oc[0])
            self.assertTrue(any("OPENCODE_CONFIG_CONTENT" in c for c in self.calls()),
                            "the runtime was told to forward the config env: %s" % self.calls())
            seen = {json.loads(l) for l in (L.scenario / "opencode.config").read_text().splitlines() if l.strip()}
            self.assertIn('{"provider": {"llama.cpp": {}}}', seen,
                          "the config content reached opencode inside the container")

    # ---- the subcommand, the way the keeper calls it -------------------------------------
    def sub_env(self, extra=None):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        bindir, scen = Path(home) / "bin", Path(home) / "scen"
        fh.install(str(bindir), str(scen))
        env = {**os.environ, "HOME": home, "BOT_CONFIG": str(Path(home) / "missing"),
               "BOT_RUNTIME": str(self.runtime), "FAKE_RUNTIME_LOG": str(self.log),
               "FAKE_SCENARIO_DIR": str(scen),
               "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
        env.update(extra or {})
        return env

    def run_sub(self, args, env):
        return subprocess.run([BOT, "run-in-container", *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_box_mode_mounts_the_shared_volume_and_no_checkout(self):
        self.log.write_text("")
        r = self.run_sub(["--box", "claude", "--", "--version"], self.sub_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        c = calls[0]
        self.assertIn("bot_board-box-state:/home/bot", c, c)
        self.assertFalse(any(a.endswith(":/work") for a in c), "box mode mounts no checkout: %s" % c)
        self.assertNotIn("BOARD", c, "box mode does not pass the board vars: %s" % c)

    def test_empty_checkout_fails_loud_with_exit_97(self):
        self.log.write_text("")
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        r = self.run_sub(["claude", "--handle", "tbot", "--dir", empty, "--", "--version"],
                         self.sub_env())
        self.assertEqual(r.returncode, 97, r.stdout + r.stderr)
        self.assertIn("did not bind-mount", r.stderr)

    def test_bot_mode_needs_a_checkout_dir(self):
        r = self.run_sub(["claude", "--handle", "tbot", "--", "--version"], self.sub_env())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("needs --dir", r.stderr)


if __name__ == "__main__":
    unittest.main()
