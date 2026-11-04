"""bin/bot as a workspace bot: identity by environment, a fixed first prompt, envelope
prompts, claims before waking, and a one-day startup catch-up.

Runs the real launcher against a small fake board and a fake `claude` that records
its environment and what it was told. Needs bash and python3; no dependencies.
`python3 tests/test_bot_workspace.py` or pytest.
"""
import datetime, json, os, shutil, signal, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bin", "bot")
ME = "tst"


def msg(mid, board, body, author="jac", mention=False, reply_to=None, thread_id=None, age=0, meta=None, kind="channel"):
    at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
    return {"id": mid, "board": board, "channel": board, "kind": kind, "thread_id": thread_id or mid, "author": author,
            "body": body, "meta": meta or {}, "reply_to": reply_to, "mentions": [ME] if mention else [],
            "created_at": at.isoformat()}


class FakeBoard:
    """Registration, /api/me, the message feed, threads and claims; what the launcher needs."""

    def __init__(self, messages, cursor=0, workers=None, taken=()):
        self.messages = messages
        self.cursor = cursor
        self.workers = dict(workers or {})
        self.taken = set(taken)
        self.registrations = []
        self.requests = []
        self.receipts = []
        self.tokens = {}
        self.lock = threading.Lock()
        board = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def send(self, code, data):
                b = json.dumps(data, separators=(",", ":")).encode()
                self.send_response(code); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def who(self):
                return board.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or "{}")
                board.requests.append(("POST", self.path, payload))
                if self.path == "/api/message-receipts":
                    board.receipts.append(payload)
                    return self.send(200, {"receipts": {}})
                if self.path == "/api/agents":
                    board.registrations.append(payload)
                    if payload["handle"] in board.taken:
                        return self.send(409, {"error": "taken", "status": 409})
                    if payload.get("workspace") == "nowhere":
                        return self.send(404, {"error": "no workspace named nowhere", "status": 404})
                    token = payload["handle"] + "-token"
                    board.tokens[token] = payload["handle"]
                    return self.send(201, {"token": token})
                if self.path.startswith("/api/threads/") and self.path.endswith("/worker"):
                    tid = int(self.path.split("/")[3])
                    with board.lock:
                        worker = board.workers.setdefault(tid, self.who())
                        if worker != self.who():
                            return self.send(409, {"error": "already claimed", "status": 409})
                        return self.send(200, {"thread_id": tid, "worker": worker})
                return self.send(201, {"message": {"id": 999}})

            def do_GET(self):
                u = urlparse(self.path); q = parse_qs(u.query); path = u.path
                board.requests.append(("GET", self.path, None))
                handle = self.who()
                since = int(q.get("since", ["0"])[0])
                if path == "/agents.md":      # composed for the token (#344); the launcher puts it in the prompt
                    b = ("the rules\n\n# Your identity\n\nYou are @%s, a bot of the workspace game.\n" % handle).encode()
                    self.send_response(200); self.send_header("content-type", "text/plain; charset=utf-8")
                    self.send_header("content-length", str(len(b))); self.send_header("X-Agents-Revision", "rev-a")
                    self.end_headers(); return self.wfile.write(b)
                if path == "/api/me":
                    return self.send(200, {"agent": {"handle": handle}, "workspace": {"slug": "game"}, "cursor": board.cursor})
                if path.startswith("/api/threads/"):
                    tid = int(path.split("/")[3])
                    messages = [dict(m, worker=board.workers.get(tid)) for m in board.messages
                                if m["thread_id"] == tid and m["id"] > since]
                    return self.send(200, {"messages": messages, "worker": board.workers.get(tid), "has_more": False,
                                          "next_since": messages[-1]["id"] if messages else since})
                if path == "/api/messages":
                    new = [dict(m, worker=board.workers.get(m["thread_id"])) for m in board.messages if m["id"] > since]
                    if q.get("order") == ["desc"]:
                        before = int(q.get("before", [str(board.cursor + 1)])[0])
                        new = sorted((m for m in new if m["id"] < before), key=lambda m: m["id"], reverse=True)
                        return self.send(200, {"messages": new, "has_more": False, "next_before": 0})
                    if not new and q.get("wait"): time.sleep(1)
                    return self.send(200, {"messages": new, "cursor": max([m["id"] for m in new], default=since)})
                return self.send(404, {"error": "Not Found", "status": 404})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


FAKE_CLAUDE = """#!/usr/bin/env python3
import sys, os, json
a = sys.argv[1:]
sid = a[a.index("--session-id") + 1] if "--session-id" in a else a[a.index("--resume") + 1]
with open(os.environ["WS_TEST_RUNS"], "a") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "sid": sid, "prompt": a[-1],
                        "env": {k: os.environ.get(k) for k in ("BOARD", "BOT_HANDLE", "BOARD_TOKEN")}}) + "\\n")
"""


class WorkspaceBot(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wsbot-"))
        self.home, self.pool = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "pool")
        bindir = os.path.join(self.tmp, "bin")
        for d in (self.home, self.pool, bindir): os.makedirs(d)
        with open(os.path.join(bindir, "claude"), "w") as f: f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.runs = os.path.join(self.tmp, "runs.jsonl")
        self.env = dict(os.environ, HOME=self.home, PATH=bindir + os.pathsep + os.environ["PATH"],
                        WS_TEST_RUNS=self.runs, BOT_WAIT="1", BOT_CONFIG=os.path.join(self.tmp, "none"), BOT_POOL=self.pool)
        for k in ("BOT_SKILLS", "BOARD", "BOARD_TOKEN", "BOT_HANDLE"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bot(self, board, until, timeout=40, args=("--workspace", "game"), name=ME):
        p = subprocess.Popen([BOT, "claude", "--name", name, *args], env=dict(self.env, BOARD=board.url),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, text=True)
        runs, end = [], time.time() + timeout
        try:
            while time.time() < end:
                time.sleep(0.5)
                if os.path.exists(self.runs):
                    with open(self.runs) as f: runs = [json.loads(l) for l in f]
                if until(runs): break
                if p.poll() is not None: break
        finally:
            if p.poll() is None: os.killpg(p.pid, signal.SIGTERM)
            self.out = p.communicate(timeout=10)[0]
        log = os.path.join(self.home, ".config/bot_board/bots", name, "log")
        self.log = ""
        if os.path.exists(log):
            with open(log) as f:
                self.log = f.read()
        return runs

    def test_first_prompt_points_at_the_guide_and_identity_is_in_the_environment(self):
        board = FakeBoard([msg(1, "game", "first task")])
        try:
            runs = self.run_bot(board, lambda rs: any("first task" in r["prompt"] for r in rs))
        finally:
            board.close()
        self.assertEqual(board.registrations[0]["workspace"], "game")
        self.assertEqual(board.registrations[0]["handle"], ME)
        self.assertIn("workspace game", board.registrations[0]["description"])
        # the launcher fetched agents.md with the bot's token and the prompt carries it (#344)
        self.assertTrue(runs[0]["prompt"].startswith("This is your AGENTS.md, composed for you by the board. Read it and return."
                                                     " Your environment holds BOARD, BOT_HANDLE and BOARD_TOKEN.\n\nthe rules\n"), runs[0]["prompt"])
        self.assertIn(f"You are @{ME}, a bot of the workspace game.", runs[0]["prompt"])
        self.assertNotIn("/agents.md", runs[0]["prompt"])
        state = os.path.join(self.home, ".config/bot_board/bots", ME)
        self.assertEqual(open(os.path.join(state, "agents-rev")).read().strip(), "rev-a")
        self.assertIn("You are @", open(os.path.join(state, "agents.md")).read())
        self.assertEqual(runs[0]["env"], {"BOARD": board.url, "BOT_HANDLE": ME, "BOARD_TOKEN": ME + "-token"})
        # one directory per bot, under the pool root, named by handle
        self.assertEqual({r["cwd"] for r in runs}, {os.path.join(self.pool, ME)})
        self.assertEqual(len({r["sid"] for r in runs}), 1, "not one resumed session")
        state = os.path.join(self.home, ".config/bot_board/bots", ME)
        self.assertEqual(open(os.path.join(state, "workspace")).read().strip(), "game")
        self.assertEqual(open(os.path.join(state, "dir")).read().strip(), os.path.join(self.pool, ME))
        # the token stays out of the prompt
        self.assertNotIn(ME + "-token", "\n".join(r["prompt"] for r in runs))

    def test_prompts_are_envelopes_and_only_roots_are_claimed(self):
        board = FakeBoard([
            msg(1, "game", "please do this"),
            msg(2, "game", "@tst what do you think?", mention=True),
            msg(3, "game", "@someone else's", meta={}, mention=False) | {"mentions": ["someone"]},
            msg(4, "game", "a reply in a stranger's thread", reply_to=3, thread_id=3),
        ], workers={3: "someone"})
        try:
            runs = self.run_bot(board, lambda rs: any("please do this" in r["prompt"] for r in rs))
        finally:
            board.close()
        prompt = next(r["prompt"] for r in runs if "please do this" in r["prompt"])
        self.assertTrue(prompt.startswith("New message(s) on the board. Handle them as agents.md says, then return."))
        self.assertIn("[new thread: message 1, channel game, thread 1, from @jac]\nplease do this", prompt)
        self.assertIn("[mention: message 2, channel game, thread 2, from @jac]\n@tst what do you think?", prompt)
        self.assertNotIn("someone else's", prompt)
        self.assertNotIn("stranger's thread", prompt)
        claims = [p for m, p, _ in board.requests if m == "POST" and p.endswith("/worker")]
        self.assertEqual(claims, ["/api/threads/1/worker"])
        self.assertTrue(board.receipts and board.receipts[0]["messages"] == [1, 2])

    def test_replies_in_owned_threads_arrive_as_replies_and_claimed_threads_are_skipped(self):
        board = FakeBoard([msg(1, "game", "taken already"), msg(2, "game", "ours")], workers={1: "other"})
        board.messages.append(msg(5, "game", "thanks, one more thing", reply_to=2, thread_id=2))
        try:
            runs = self.run_bot(board, lambda rs: any("one more thing" in r["prompt"] for r in rs))
        finally:
            board.close()
        prompts = "\n".join(r["prompt"] for r in runs)
        self.assertNotIn("taken already", prompts)
        self.assertIn("[new thread: message 2, channel game, thread 2, from @jac]\nours", prompts)
        self.assertIn("[reply: message 5, thread 2, from @jac]\nthanks, one more thing", prompts)

    def test_startup_catch_up_looks_back_one_day(self):
        board = FakeBoard([msg(1, "game", "last week", age=3 * 86400), msg(2, "game", "this morning", age=3600),
                           msg(3, "game", "done already", age=60, meta={"done": True})], cursor=3)
        try:
            runs = self.run_bot(board, lambda rs: any("this morning" in r["prompt"] for r in rs))
        finally:
            board.close()
        prompts = "\n".join(r["prompt"] for r in runs)
        self.assertIn("this morning", prompts)
        self.assertNotIn("last week", prompts)
        self.assertNotIn("done already", prompts)
        self.assertIn("checking the last 24 hours", self.log)

    def test_startup_catch_up_offers_a_root_whose_mentions_reach_nobody(self):
        # #453: as the live pickup does (#216), a root for someone who cannot claim is offered
        stopped = [{"mention": "board", "state": "never claims"}]
        board = FakeBoard([
            msg(1, "game", "for the board, which never claims", age=600) | {"mentions": ["board"], "unreachable": stopped},
            msg(2, "game", "for kira, who is up", age=500) | {"mentions": ["kira"]},
            msg(3, "game", "for us and kira", age=400) | {"mentions": [ME, "kira"]},
            msg(4, "game", "for a stopped bot and a running one", age=300) | {
                "mentions": ["kira", "zed"], "unreachable": [{"mention": "zed", "state": "stopped"}]},
        ], cursor=4)
        try:
            runs = self.run_bot(board, lambda rs: any("for us and kira" in r["prompt"] for r in rs)
                                and any("which never claims" in r["prompt"] for r in rs))
        finally:
            board.close()
        prompts = "\n".join(r["prompt"] for r in runs)
        self.assertIn("for the board, which never claims", prompts)
        self.assertIn("for us and kira", prompts)
        self.assertNotIn("for kira, who is up", prompts)
        self.assertNotIn("for a stopped bot and a running one", prompts)

    def test_a_new_bot_needs_a_workspace(self):
        board = FakeBoard([])
        try:
            self.run_bot(board, lambda rs: False, timeout=5, args=())
        finally:
            board.close()
        self.assertIn("a new bot needs a workspace", self.out)
        self.assertEqual(board.registrations, [])

    def test_a_restarted_bot_remembers_its_workspace(self):
        board = FakeBoard([msg(1, "game", "one")])
        try:
            self.run_bot(board, lambda rs: any("one" in r["prompt"] for r in rs))
            board.messages.append(msg(2, "game", "two"))
            runs = self.run_bot(board, lambda rs: any("two" in r["prompt"] for r in rs), args=())
        finally:
            board.close()
        self.assertEqual(len(board.registrations), 1)
        self.assertTrue(any("two" in r["prompt"] for r in runs))
        self.assertEqual(len({r["sid"] for r in runs}), 1)
        self.assertEqual(len([r for r in runs if "This is your AGENTS.md" in r["prompt"]]), 1, "the first prompt ran again")


if __name__ == "__main__":
    unittest.main()
