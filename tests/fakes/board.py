"""A fake board for bin/bot tests: the endpoints the launcher calls, in memory, in a thread.

    with FakeBoard() as board:
        board.post_root("ch", "do the thing", author="jac")   # a thread for a bot to claim
        ... run bin/bot against board.url ...
        board.requests                                      # every (method, path, body) seen
        board.worker(1)                                     # who holds thread 1

Long-polls (`wait=`) return as soon as a message arrives, so tests run at the speed of
the launcher, not of its poll window.
"""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeBoard:
    def __init__(self, handle_token="tok-bot", workspace="ws"):
        self.lock = threading.Condition()
        self.messages = []          # dicts in board shape, id ascending
        self.workers = {}           # thread id -> handle
        self.requests = []          # (method, path, body dict or None)
        self.tokens = {}            # token -> handle
        self.default_token = handle_token
        self.workspace = workspace
        self.claim_status = {}      # thread id -> HTTP status a claim gets instead of success
        self.agents_rev = ""        # the X-Agents-Revision /agents.md carries; "" sends none (an old board)
        self.profile = None        # what GET /api/me/profile?json answers; None: no profile
        self.skills = {}           # name -> the SKILL.md text GET /api/skills/<name> serves
        self.skills_revision = ""  # skills_revision in the profile answer; "" for a board that has none
        self.server = None

    # ---- lifecycle -------------------------------------------------------------------
    def __enter__(self):
        board = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self): board.handle(self, "GET")
            def do_POST(self): board.handle(self, "POST")
            def do_DELETE(self): board.handle(self, "DELETE")
            def do_PUT(self): board.handle(self, "PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.lock.notify_all()
        self.server.shutdown()
        self.server.server_close()

    # ---- what tests do ---------------------------------------------------------------
    def add(self, channel, body, author="jac", reply_to=None, meta=None, mentions=(), kind="channel", unreachable=()):
        with self.lock:
            mid = len(self.messages) + 1
            tid = reply_to and (self.get(reply_to)["thread_id"] or reply_to) or mid
            m = {"id": mid, "board": channel, "channel": channel, "thread_id": tid if reply_to else None,
                 "reply_to": reply_to, "author": author, "body": body, "kind": kind,
                 "meta": meta or {}, "mentions": list(mentions), "worker": self.workers.get(tid),
                 "completed_through": None,
                 **({"unreachable": [{"mention": h, "state": "stopped", "last_seen_at": None} for h in unreachable]}
                    if unreachable else {}),
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.messages.append(m)
            self.lock.notify_all()
            return mid

    def post_root(self, channel, body, **kw):
        return self.add(channel, body, **kw)

    def reply(self, root, body, **kw):
        return self.add(self.get(root)["board"], body, reply_to=root, **kw)

    def get(self, mid):
        return self.messages[mid - 1]

    def worker(self, tid):
        return self.workers.get(tid)

    def by(self, author):
        return [m for m in self.messages if m["author"] == author]

    def seen(self, method, path_prefix):
        return [r for r in self.requests if r[0] == method and r[1].startswith(path_prefix)]

    # ---- the API ---------------------------------------------------------------------
    def handle(self, h, method):
        u = urlparse(h.path); q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        n = int(h.headers.get("content-length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        with self.lock:
            self.requests.append((method, h.path, body))
        auth = h.headers.get("authorization", "")
        handle = self.tokens.get(auth.replace("Bearer ", "").strip(), "")
        try:
            status, out = self.route(method, u.path, q, body, handle)
        except Exception as e:      # a bug in the fake must show up as a 500, not a hang
            status, out = 500, {"detail": repr(e)}
        text = isinstance(out, str)           # a SKILL.md, served as text
        data = out.encode() if text else json.dumps(out, separators=(",", ":")).encode()   # bin/bot greps the compact form
        h.send_response(status)
        h.send_header("content-type", "text/plain; charset=utf-8" if text else "application/json")
        h.send_header("content-length", str(len(data)))
        if u.path == "/agents.md" and self.agents_rev:
            h.send_header("X-Agents-Revision", self.agents_rev)
        h.end_headers()
        h.wfile.write(data)

    def route(self, method, path, q, body, handle):
        parts = path.strip("/").split("/")
        if path == "/healthz":
            return 200, {"ok": True, "version": "fake"}
        if path == "/agents.md":
            return 200, {"guide": "the rules"}
        if path == "/api/agents" and method == "POST":
            tok = self.default_token
            self.tokens[tok] = body["handle"]
            return 200, {"token": tok, "handle": body["handle"]}
        if path == "/api/agents" and method == "GET":
            return 200, {"agents": [{"handle": hd, "last_seen_at": None} for hd in self.tokens.values()]}
        if not handle:
            return 401, {"detail": "no token"}
        if path == "/api/me/profile":
            # the bot's profile, as the board renders it for a launcher (?json): set board.profile
            p = self.profile if self.profile is not None else {"profile": None, "skills": []}
            return 200, {"skills_revision": self.skills_revision, **p}
        if len(parts) == 3 and parts[1] == "skills" and method == "GET":
            if parts[2] in self.skills:
                return 200, self.skills[parts[2]]
            return 404, {"detail": "no skill named " + parts[2]}
        if path == "/api/me":
            with self.lock:
                cursor = len(self.messages)
            return 200, {"handle": handle, "cursor": cursor, "workspace": {"slug": self.workspace},
                         "agent": {"handle": handle, "personality": ""}}
        if path == "/api/messages" and method == "GET":
            return 200, self.feed(q, handle)
        if path == "/api/messages" and method == "POST":
            mid = self.add(body.get("channel") or body.get("board"), body["body"], author=handle,
                           reply_to=body.get("reply_to"), meta=body.get("meta"))
            return 200, {"message": self.get(mid)}
        if path == "/api/message-receipts":
            return 200, {"ok": True}
        if len(parts) >= 3 and parts[1] == "threads":
            tid = int(parts[2])
            if len(parts) == 4 and parts[3] == "worker":
                if method == "POST":
                    if tid in self.claim_status:
                        return self.claim_status[tid], {"detail": "refused by test"}
                    with self.lock:
                        cur = self.workers.get(tid)
                        if cur and cur != handle:
                            return 409, {"detail": "taken by " + cur}
                        self.workers[tid] = handle
                    return 200, {"thread_id": tid, "worker": handle}
                if method == "DELETE":
                    with self.lock:
                        self.workers.pop(tid, None)
                    return 200, {"thread_id": tid, "worker": None}
                return 200, {"thread_id": tid, "worker": self.workers.get(tid)}
            if len(parts) == 3 and method == "GET":
                since = int(q.get("since", 0))
                with self.lock:
                    msgs = [m for m in self.messages if (m["thread_id"] or m["id"]) == tid and m["id"] > since]
                return 200, {"thread_id": tid, "worker": self.workers.get(tid), "messages": msgs, "has_more": False}
        return 404, {"detail": "no route in the fake for %s %s" % (method, path)}

    def feed(self, q, handle):
        with self.lock:
            if "before" in q:       # the startup catch-up: one descending page, no waiting
                before = int(q["before"])
                msgs = [m for m in self.messages if m["id"] < before and m["kind"] == "channel"]
                return {"messages": list(reversed(msgs)), "has_more": False, "next_before": 0}
            since = int(q.get("since", 0)); wait = float(q.get("wait", 0))
            end = time.monotonic() + wait
            while not any(m["id"] > since for m in self.messages) and time.monotonic() < end:
                self.lock.wait(min(0.5, max(0.0, end - time.monotonic())))
            msgs = [dict(m, worker=self.workers.get(m["thread_id"] or m["id"])) for m in self.messages if m["id"] > since]
            return {"messages": msgs, "has_more": False, "cursor": len(self.messages)}
