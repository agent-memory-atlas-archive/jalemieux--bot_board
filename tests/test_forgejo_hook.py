"""#379: a Forgejo webhook delivery whose pull request or issue carries a thread:<id> label (or
a thread link in its body) becomes one line in that thread, posted by the board, mentioning nobody. Run with python -m unittest discover -s tests."""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main

SECRET = "forgejo-signs-with-this-0123"


def payload(event, thread, **over):
    """A Forgejo payload in the Gitea shape: the pull request (or issue) carries its labels and body.
    `thread` is the thread id for the label, or a string put in the body instead (no label)."""
    labels = [{"name": "bot:lola"}] + ([{"name": f"thread:{thread}"}] if isinstance(thread, int) else [])
    body = "Closes #7." if isinstance(thread, int) else f"Closes #7.\n\nBoard thread: {thread}\n"
    pr = {"number": 42, "title": "versions as a table", "html_url": "https://forge/jac/bot_board/pulls/42",
          "body": body, "merged": False, "state": "open", "labels": labels}
    base = {"repository": {"full_name": "jac/bot_board"}, "sender": {"login": "jac", "username": "jac"}}
    if event == "pull_request_review_rejected":
        base.update(action="reviewed", number=42, pull_request=pr,
                    review={"type": "rejected", "content": "@lola please add a test for the empty case\nand rebase"})
    elif event == "pull_request_review_approved":
        base.update(action="reviewed", number=42, pull_request=pr, review={"type": "approved", "content": ""})
    elif event == "issue_comment":
        base.update(action="created", is_pull=True, issue={**pr, "pull_request": {"merged": False}},
                    comment={"body": "looks close, one more pass"})
    elif event == "pull_request":
        base.update(action="opened", number=42, pull_request=pr)
    elif event == "issues":
        base.update(action="opened", number=7, issue={"number": 7, "title": "versions", "html_url": "https://forge/i/7",
                                                      "body": "see the thread", "labels": labels})
    elif event == "push":
        base.update(ref="refs/heads/main", commits=[])
    base.update(over)
    return base


class ForgejoHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "board.db")
        db._local = threading.local()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.jac = self.register("jac", "human")
        self.lola = self.register("lola", "claude-code")
        r = self.client.post("/api/messages", headers=self.jac, json={"channel": "general", "body": "versions table please"})
        self.tid = r.json()["message"]["id"]
        self.thread_url = r.json()["url"]
        self.assertEqual(self.client.post(f"/api/threads/{self.tid}/worker", headers=self.lola).status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def configure(self):
        r = self.client.put("/api/hooks/forgejo", headers=self.jac, json={"secret": SECRET})
        self.assertEqual(r.status_code, 200, r.text)

    def deliver(self, event, body=None, secret=SECRET, delivery="d-1", sign=True, event_type=None):
        raw = json.dumps(body if body is not None else payload(event, self.tid)).encode()
        headers = {"Content-Type": "application/json", "X-Forgejo-Event": event.split("_review_")[0]
                   if "_review_" in event else event, "X-Forgejo-Event-Type": event_type or event,
                   "X-Forgejo-Delivery": delivery}
        if sign:
            headers["X-Forgejo-Signature"] = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post("/api/hooks/forgejo", content=raw, headers=headers)

    def thread(self):
        return self.client.get(f"/api/threads/{self.tid}", headers=self.jac).json()["messages"]

    # ---------------------------------------------------------------- the secret

    def test_without_a_secret_the_hook_refuses_and_says_how_to_set_one(self):
        r = self.deliver("pull_request_review_rejected")
        self.assertEqual(r.status_code, 503)
        self.assertIn("PUT /api/hooks/forgejo", r.json()["error"])
        self.assertEqual(self.client.get("/api/hooks/forgejo", headers=self.lola).json(), {"name": "forgejo", "configured": False})

    def test_only_a_human_sets_the_secret_and_nobody_reads_it_back(self):
        r = self.client.put("/api/hooks/forgejo", headers=self.lola, json={"secret": SECRET})
        self.assertEqual(r.status_code, 403)
        self.configure()
        got = self.client.get("/api/hooks/forgejo", headers=self.lola).json()
        self.assertTrue(got["configured"])
        self.assertEqual(got["set_by"], "jac")
        self.assertNotIn(SECRET, json.dumps(got))
        self.assertIsNone(got["last_delivery"])
        self.assertEqual(self.client.get("/api/hooks/nope", headers=self.jac).status_code, 404)

    def test_a_bad_or_missing_signature_is_refused(self):
        self.configure()
        self.assertEqual(self.deliver("pull_request_review_rejected", secret="wrong-secret-wrong-secret").status_code, 401)
        self.assertEqual(self.deliver("pull_request_review_rejected", sign=False).status_code, 401)
        self.assertEqual(self.thread()[1:], [])

    def test_the_github_style_signature_header_works_too(self):
        self.configure()
        raw = json.dumps(payload("pull_request_review_approved", self.tid)).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        r = self.client.post("/api/hooks/forgejo", content=raw, headers={
            "X-Gitea-Event": "pull_request_approved", "X-Gitea-Delivery": "g-1", "X-Hub-Signature-256": sig})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("jac approved PR #42", self.thread()[-1]["body"])

    # -------------------------------------------------------------- the message

    def test_requested_changes_land_in_the_thread_for_its_worker_with_no_mention(self):
        self.configure()
        r = self.deliver("pull_request_review_rejected")
        self.assertEqual(r.status_code, 201, r.text)
        out = r.json()
        self.assertEqual(out["thread"], self.tid)
        self.assertEqual(out["url"], self.thread_url)
        msg = self.thread()[-1]
        self.assertEqual(msg["id"], out["message"])
        self.assertEqual(msg["author"], "board")
        self.assertEqual(msg["body"], "Forgejo: jac requested changes on PR #42 (versions as a table)"
                                      " https://forge/jac/bot_board/pulls/42\n> lola please add a test for the empty case")
        self.assertEqual(msg["mentions"], [])          # the review said @lola; the board does not
        self.assertEqual(msg["meta"], {"kind": "forgejo", "event": "pull_request_review_rejected", "action": "reviewed",
                                       "repo": "jac/bot_board", "number": 42,
                                       "url": "https://forge/jac/bot_board/pulls/42", "delivery": "d-1"})
        # the worker gets it as a reply in its thread
        inbox = self.client.get("/api/inbox", headers=self.lola, params={"since": self.tid}).json()["messages"]
        self.assertEqual([m["id"] for m in inbox], [msg["id"]])
        got = self.client.get("/api/hooks/forgejo", headers=self.jac).json()["last_delivery"]
        self.assertEqual(got["event"], "pull_request_review_rejected")
        self.assertEqual(got["outcome"], f"posted message {msg['id']} in thread {self.tid}")

    def test_the_same_delivery_again_posts_nothing_twice(self):
        self.configure()
        first = self.deliver("pull_request_review_rejected", delivery="d-7")
        again = self.deliver("pull_request_review_rejected", delivery="d-7")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json(), {"replayed": True, "message": first.json()["message"], "thread": self.tid})
        self.assertEqual(len(self.thread()), 2)
        # a different delivery is a different event
        self.assertEqual(self.deliver("pull_request_review_rejected", delivery="d-8").status_code, 201)
        self.assertEqual(len(self.thread()), 3)

    def test_each_event_reads_as_one_plain_line(self):
        self.configure()
        cases = [
            ("pull_request_review_approved", {}, "Forgejo: jac approved PR #42 (versions as a table) https://forge/jac/bot_board/pulls/42"),
            ("issue_comment", {}, "Forgejo: jac commented on PR #42 (versions as a table) https://forge/jac/bot_board/pulls/42\n> looks close, one more pass"),
            ("pull_request", {}, "Forgejo: jac opened PR #42 (versions as a table) https://forge/jac/bot_board/pulls/42\n> Closes #7."),
            ("issues", {}, "Forgejo: jac opened issue #7 (versions) https://forge/i/7\n> see the thread"),
        ]
        for i, (event, over, want) in enumerate(cases):
            r = self.deliver(event, payload(event, self.tid, **over), delivery=f"c-{i}")
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(self.thread()[-1]["body"], want, event)
        merged = payload("pull_request", self.tid, action="closed")
        merged["pull_request"]["merged"] = True
        self.deliver("pull_request", merged, delivery="m-1")
        self.assertEqual(self.thread()[-1]["body"].splitlines()[0], "Forgejo: jac merged PR #42 (versions as a table) https://forge/jac/bot_board/pulls/42")
        closed = payload("pull_request", self.tid, action="closed")
        self.deliver("pull_request", closed, delivery="m-2")
        self.assertIn("closed PR #42 (versions as a table) without merging", self.thread()[-1]["body"])
        pushed = payload("pull_request", self.tid, action="synchronized")
        self.deliver("pull_request", pushed, delivery="m-3")
        self.assertIn("pushed new commits to PR #42", self.thread()[-1]["body"])

    def test_a_long_excerpt_is_cut_and_keeps_its_first_line_only(self):
        self.configure()
        body = payload("pull_request_review_rejected", self.tid)
        body["review"]["content"] = "\n\n" + "x" * 500 + "\nsecond line"
        self.deliver("pull_request_review_rejected", body)
        quote = self.thread()[-1]["body"].split("\n> ")[1]
        self.assertEqual(len(quote), 200)
        self.assertTrue(quote.endswith("..."))

    # ---------------------------------------------------------------- ignored

    def test_events_with_nothing_to_say_are_accepted_and_ignored_with_a_reason(self):
        self.configure()
        r = self.deliver("push")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertIn("push", r.json()["ignored"])
        edited = payload("issue_comment", self.tid, action="edited")
        r = self.deliver("issue_comment", edited, delivery="e-1")
        self.assertEqual(r.status_code, 202)
        self.assertIn("(edited)", r.json()["ignored"])
        self.assertEqual(len(self.thread()), 1)
        self.assertTrue(self.client.get("/api/hooks/forgejo", headers=self.jac).json()["last_delivery"]["outcome"].startswith("ignored:"))

    def test_the_thread_link_in_the_body_is_the_fallback_when_there_is_no_label(self):
        self.configure()
        r = self.deliver("pull_request_review_rejected", payload("pull_request_review_rejected", self.thread_url))
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["thread"], self.tid)
        # the label wins over a link in the body
        other = self.client.post("/api/messages", headers=self.jac, json={"channel": "general", "body": "another"}).json()
        body = payload("pull_request_review_rejected", other["message"]["id"])
        body["pull_request"]["body"] += f"\nsee also {self.thread_url}"
        r = self.deliver("pull_request_review_rejected", body, delivery="l-2")
        self.assertEqual(r.json()["thread"], other["message"]["id"])

    def test_a_payload_that_names_no_thread_or_a_missing_one_is_ignored(self):
        self.configure()
        r = self.deliver("pull_request_review_rejected", payload("pull_request_review_rejected", "https://forge/no/thread/here"))
        self.assertEqual(r.status_code, 202)
        self.assertIn("thread:<id> label", r.json()["ignored"])
        missing = payload("pull_request_review_rejected", 99999)
        r = self.deliver("pull_request_review_rejected", missing, delivery="n-2")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["ignored"], "thread 99999 does not exist on this board")
        self.assertEqual(len(self.thread()), 1)

    def test_a_link_to_a_reply_lands_in_its_thread_and_a_completed_thread_reopens(self):
        self.configure()
        reply = self.client.post("/api/messages", headers=self.lola, json={"reply_to": self.tid, "body": "PR is up"}).json()["message"]
        self.assertEqual(self.client.post(f"/api/threads/{self.tid}/complete", headers=self.lola,
                                          json={"through": reply["id"]}).status_code, 200)
        r = self.deliver("pull_request_review_rejected", payload("pull_request_review_rejected", reply["id"]))
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["thread"], self.tid)
        msg = self.thread()[-1]
        self.assertEqual(msg["reply_to"], self.tid)
        self.assertTrue(msg["meta"].get("thread_reopened"))
        self.assertIsNone(db.conn().execute("SELECT 1 FROM thread_completions WHERE thread_id=?", (self.tid,)).fetchone())

    def test_a_body_that_is_not_json_is_refused(self):
        self.configure()
        raw = b"not json"
        r = self.client.post("/api/hooks/forgejo", content=raw, headers={
            "X-Forgejo-Event": "push", "X-Forgejo-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/api/hooks/nope", content=raw).status_code, 404)


if __name__ == "__main__":
    unittest.main()
