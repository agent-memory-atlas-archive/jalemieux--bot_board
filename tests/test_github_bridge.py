"""The check of the github-bridge routine (deploy/github-bridge/check, #424): a GitHub issue gets
a Forgejo twin once, comments cross both ways once, @mentions are defused, closing on Forgejo
closes on GitHub, and a pull request on the mirror is told where code goes. It is run as the
runner runs it, a script with its tokens in the environment, against one small HTTP server that
stands in for GitHub's API under /gh and Forgejo's under /fj."""
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "deploy" / "github-bridge" / "check"


class Forges:
    """Issues, comments and labels for o/mirror on GitHub and o/work on Forgejo, in memory."""
    def __init__(self):
        self.ids = iter(range(1000, 10**6))
        self.gh = {"issues": {}, "comments": {}, "pulls": {}}
        self.fj = {"issues": {}, "comments": {}, "labels": []}
        self.auth = []
        self.down = 0

    def gh_issue(self, n, title, body="", by="outsider", at="2026-11-04T10:00:00Z", pr=False):
        i = {"number": n, "title": title, "body": body, "user": {"login": by}, "created_at": at, "state": "open",
             "html_url": f"https://github.com/o/mirror/issues/{n}"}
        if pr:
            i["pull_request"] = {}
            self.gh["pulls"][n] = i
        self.gh["issues"][n] = i
        self.gh["comments"].setdefault(n, [])
        return i

    def comment(self, side, n, body, by):
        c = {"id": next(self.ids), "body": body, "user": {"login": by}, "html_url": f"https://github.com/o/mirror/issues/{n}#c"}
        getattr(self, side)["comments"].setdefault(n, []).append(c)
        return c

    def __enter__(self):
        forges = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def reply(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def handle_any(self, method):
                if forges.down:
                    forges.down -= 1
                    return self.reply({}, 503)
                u = urlparse(self.path); q = parse_qs(u.query); p = u.path
                forges.auth.append((p.split("/")[1], self.headers.get("Authorization")))
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                if q.get("page", ["1"])[0] != "1":
                    return self.reply([])
                g, f = forges.gh, forges.fj
                if p == "/gh/repos/o/mirror/issues":
                    return self.reply([i for i in g["issues"].values() if i["state"] == "open"])
                if p == "/gh/repos/o/mirror/pulls":
                    return self.reply([i for i in g["pulls"].values() if i["state"] == "open"])
                m = re.fullmatch(r"/gh/repos/o/mirror/issues/(\d+)(/comments)?", p)
                if m:
                    n = int(m.group(1))
                    if m.group(2) and method == "POST":
                        return self.reply(forges.comment("gh", n, data["body"], "bots"), 201)
                    if m.group(2):
                        return self.reply(g["comments"].get(n, []))
                    g["issues"][n].update(data)
                    return self.reply(g["issues"][n])
                if p == "/fj/api/v1/repos/o/work/labels":
                    if method == "POST":
                        f["labels"].append({**data, "id": next(forges.ids)})
                        return self.reply(f["labels"][-1], 201)
                    return self.reply(f["labels"])
                if p == "/fj/api/v1/repos/o/work/issues":
                    if method == "POST":
                        n = len(f["issues"]) + 500
                        f["issues"][n] = {**data, "number": n, "state": "open",
                                          "labels": [l for l in f["labels"] if l["id"] in data["labels"]]}
                        return self.reply(f["issues"][n], 201)
                    want = q.get("labels", [""])[0]
                    return self.reply([i for i in f["issues"].values() if any(l["name"] == want for l in i["labels"])])
                m = re.fullmatch(r"/fj/api/v1/repos/o/work/issues/(\d+)/comments", p)
                if m:
                    n = int(m.group(1))
                    if method == "POST":
                        return self.reply(forges.comment("fj", n, data["body"], "fleet"), 201)
                    return self.reply(f["comments"].get(n, []))
                self.reply({"message": "not found " + p}, 404)

            def do_GET(self): self.handle_any("GET")
            def do_POST(self): self.handle_any("POST")
            def do_PATCH(self): self.handle_any("PATCH")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        self.server.shutdown(); self.server.server_close()

    def run(self):
        env = {**os.environ, "GITHUB_API": self.url + "/gh", "BRIDGE_FORGEJO": self.url + "/fj", "BRIDGE_GITHUB": "o/mirror",
               "BRIDGE_FORGE_REPO": "o/work", "BRIDGE_SINCE": "2026-11-04T00:00:00Z", "GH_TOKEN": "ght", "FORGEJO_TOKEN": "fjt"}
        r = subprocess.run([sys.executable, str(CHECK)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["work"] is False, out
        return out


class Bridge(unittest.TestCase):
    def test_an_issue_crosses_once_and_the_conversation_goes_both_ways(self):
        with Forges() as f:
            f.gh_issue(7, "Crash on start", "It crashes.\n@jac please look")
            f.gh_issue(3, "Old, migrated", at="2026-10-01T00:00:00Z")      # before SINCE: left alone
            f.run()
            (twin,) = f.fj["issues"].values()
            self.assertEqual(twin["title"], "Crash on start")
            self.assertEqual([l["name"] for l in twin["labels"]], ["from-github"])
            self.assertIn("https://github.com/o/mirror/issues/7", twin["body"])
            self.assertIn("> It crashes.\n> `@jac` please look", twin["body"])
            self.assertIn("<!-- bridge github:o/mirror#7 -->", twin["body"])
            (told,) = f.gh["comments"][7]
            self.assertIn(f"tracked there as #{twin['number']}", told["body"])
            self.assertEqual(f.gh["comments"][3], [])
            self.assertIn(("gh", "Bearer ght"), f.auth)
            self.assertIn(("fj", "token fjt"), f.auth)

            f.run()      # nothing new: nothing posted twice
            self.assertEqual(len(f.fj["issues"]), 1)
            self.assertEqual(len(f.gh["comments"][7]), 1)

            n = twin["number"]
            f.comment("fj", n, "Fixed in PR #12, thanks @outsider.", "fleet")
            f.comment("gh", 7, "Still happens on 1.2.", "outsider")
            f.run()
            back = f.gh["comments"][7][-1]["body"]
            self.assertTrue(back.startswith("**fleet** on the forge:\n\nFixed in PR #12, thanks `@outsider`."), back)
            there = f.fj["comments"][n][-1]["body"]
            self.assertIn("`outsider` on GitHub", there)
            self.assertIn("> Still happens on 1.2.", there)
            counts = (len(f.gh["comments"][7]), len(f.fj["comments"][n]))
            f.run()      # copies are never copied back
            self.assertEqual((len(f.gh["comments"][7]), len(f.fj["comments"][n])), counts)

            twin["state"] = "closed"
            f.run()
            self.assertEqual(f.gh["issues"][7]["state"], "closed")
            self.assertIn(f"Closed on the forge (#{n})", f.gh["comments"][7][-1]["body"])
            f.gh["issues"][7]["state"] = "open"     # reopened by its author: not closed again
            f.run()
            self.assertEqual(f.gh["issues"][7]["state"], "open")

    def test_a_pull_request_is_told_where_code_goes_once(self):
        with Forges() as f:
            f.gh_issue(9, "Fix typo", pr=True)
            f.run(); f.run()
            (c,) = f.gh["comments"][9]
            self.assertIn("read-only mirror", c["body"])
            self.assertEqual(f.fj["issues"], {}, "a pull request gets no twin")

    def test_a_forge_out_of_reach_is_no_work_this_pass(self):
        with Forges() as f:
            f.gh_issue(7, "Crash")
            f.down = 1
            self.assertIn("503", f.run()["note"])
            self.assertEqual(f.fj["issues"], {})
            f.run()
            self.assertEqual(len(f.fj["issues"]), 1)


if __name__ == "__main__":
    unittest.main()
