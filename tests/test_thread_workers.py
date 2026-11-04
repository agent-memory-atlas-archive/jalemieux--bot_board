"""Atomic pickup and delivery: one worker, helpers only by mention."""
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, web


class ThreadWorkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "board.db"))
        self.local = patch.object(db, "_local", threading.local())
        self.path.start()
        self.local.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.human = self.register("operator", "human")
        self.worker = self.register("worker", "copilot")
        self.helper = self.register("helper", "copilot")
        self.other = self.register("other", "copilot")
        self.root = self.post(self.human, "Please build the script.")
        self.endpoint = f'/api/threads/{self.root["id"]}/worker'

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.local.stop()
        self.path.stop()
        self.tmp.cleanup()

    def register(self, handle, kind):
        r = self.client.post("/api/agents", json={"handle": handle, "kind": kind})
        self.assertEqual(r.status_code, 201, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def post(self, auth, body, reply=None, channel="work"):
        r = self.client.post("/api/messages", headers=auth, json={
            "channel": channel, "body": body, "reply_to": reply,
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["message"]

    def inbox(self, auth, since=0):
        r = self.client.get("/api/inbox", headers=auth, params={"since": since})
        self.assertEqual(r.status_code, 200, r.text)
        return [m["id"] for m in r.json()["messages"]]

    def claim(self, auth):
        return self.client.post(self.endpoint, headers=auth)

    def test_simultaneous_api_claims_have_one_winner_and_no_claim_messages(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda auth: self.claim(auth).status_code,
                                     [self.worker, self.helper]))
        self.assertEqual(sorted(statuses), [200, 409])
        state = self.client.get(self.endpoint).json()
        winner = self.worker if state["worker"] == "worker" else self.helper
        self.assertEqual(self.claim(winner).json(), state)
        thread = self.client.get(f'/api/threads/{self.root["id"]}').json()
        self.assertEqual(thread["worker"], state["worker"])
        self.assertEqual(len(thread["messages"]), 1)
        self.assertEqual(thread["messages"][0]["worker"], state["worker"])

    def test_independent_processes_cannot_both_claim(self):
        script = """
import sys
from app import db
db.DB_PATH = sys.argv[1]
agent = db.get_agent_by_handle(sys.argv[3])
try:
    db.claim_thread(int(sys.argv[2]), agent)
    print("won")
except ValueError:
    print("lost")
"""
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, db.DB_PATH, str(self.root["id"]), handle],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        ) for handle in ("worker", "helper")]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(stdout.strip())
        self.assertEqual(sorted(results), ["lost", "won"])

    def test_only_worker_follows_replies_and_mentions_do_not_subscribe_helpers(self):
        self.assertEqual(self.claim(self.worker).status_code, 200)
        rid = self.root["id"]
        invite = self.post(self.worker, "@helper, where is the mapping?", rid)
        self.assertEqual(self.inbox(self.helper), [invite["id"]])
        answer = self.post(self.helper, "Here is the mapping.", rid)
        followup = self.post(self.human, "Also support resume.", rid)
        tagged = self.post(self.human, "@helper, does it include Notes?", rid)
        final = self.post(self.helper, "Yes, it does.", tagged["id"])
        self.assertEqual(self.inbox(self.worker, rid),
                         [answer["id"], followup["id"], tagged["id"], final["id"]])
        self.assertEqual(self.inbox(self.helper), [invite["id"], tagged["id"]])
        self.assertEqual(self.inbox(self.other), [])
        self.assertEqual(self.inbox(self.human), [])
        self.assertEqual(self.client.get(self.endpoint).json()["worker"], "worker")

    def test_posting_and_backing_off_never_subscribe(self):
        self.post(self.helper, "Backing off.", self.root["id"])
        self.post(self.human, "More details.", self.root["id"])
        self.assertEqual(self.inbox(self.helper), [])

    def test_owner_survives_database_reinitialization_and_paging(self):
        self.assertEqual(self.claim(self.worker).status_code, 200)
        state = self.client.get(self.endpoint).json()
        db.init()
        self.assertEqual(self.client.get(self.endpoint).json(), state)
        self.post(self.human, "Follow-up", self.root["id"])
        page = self.client.get("/api/inbox", headers=self.worker, params={"limit": 1, "view": "compact"}).json()
        self.assertTrue(page["has_more"])
        self.assertEqual(page["messages"][0]["worker"], "worker")
        next_page = self.client.get("/api/inbox", headers=self.worker,
                                    params={"since": page["next_since"]}).json()
        self.assertEqual(len(next_page["messages"]), 1)

    def test_release_events_are_hidden_only_for_human_views(self):
        self.assertEqual(self.claim(self.worker).status_code, 200)
        reply = self.post(self.worker, "Useful handoff context", self.root["id"])
        self.assertEqual(self.client.delete(self.endpoint, headers=self.worker).status_code, 200)
        endpoint = f'/api/threads/{self.root["id"]}'
        raw = self.client.get(endpoint).json()["messages"]
        event = raw[-1]
        self.assertEqual(event["meta"]["kind"], "thread_released")
        visible = self.client.get(endpoint, params={"hide_events": True}).json()["messages"]
        self.assertEqual([m["id"] for m in visible], [self.root["id"], reply["id"]])
        self.assertEqual(visible[0]["replies"]["count"], 1)
        self.assertEqual(visible[0]["replies"]["last_id"], reply["id"])
        self.assertEqual(raw[0]["replies"]["count"], 2)
        for hidden, unread, last in [(False, 1, event["id"]), (True, 0, reply["id"])]:
            rows = self.client.get('/api/channels', params={"hide_events": hidden, "since": reply["id"]}).json()["channels"]
            channel = next(c for c in rows if c["slug"] == "work")
            self.assertEqual(channel["unread"], unread)
            self.assertEqual(channel["last_message_id"], last)
        page = self.client.get('/api/messages', params={"hide_events": True, "limit": 1,
                                                       "since": self.root["id"]}).json()
        self.assertEqual([m["id"] for m in page["messages"]], [reply["id"]])
        self.assertFalse(page["has_more"])
        page = self.client.get('/api/messages', params={"since": reply["id"]}).json()
        self.assertEqual(page["messages"][0]["id"], event["id"])
        for path in ['/a/worker', '/search?q=Thread+worker+released']:
            self.assertNotIn('Thread worker released; available for pickup.', self.client.get(path).text)
        self.assertEqual(self.client.get('/api/search', params={"q": "Thread worker released"}).json()["messages"][0]["id"], event["id"])
        self.assertIsNone(self.client.get(self.endpoint).json()["worker"])
        self.assertEqual(self.claim(self.helper).status_code, 200)

    def test_release_permissions_and_reassignment(self):
        self.assertEqual(self.claim(self.worker).status_code, 200)
        self.assertEqual(self.client.delete(self.endpoint, headers=self.helper).status_code, 403)
        self.assertEqual(self.client.delete(self.endpoint, headers=self.human).status_code, 200)
        self.assertIsNone(self.client.get(self.endpoint).json()["worker"])
        thread = self.client.get(f'/api/threads/{self.root["id"]}').json()["messages"]
        self.assertEqual(thread[-1]["meta"]["kind"], "thread_released")
        self.assertEqual(self.claim(self.helper).status_code, 200)
        self.assertEqual(self.claim(self.worker).status_code, 409)
        followup = self.post(self.human, "Continue here.", self.root["id"])
        self.assertNotIn(followup["id"], self.inbox(self.worker))
        self.assertIn(followup["id"], self.inbox(self.helper))
        self.assertEqual(self.client.delete(self.endpoint, headers=self.helper).status_code, 200)

    def test_a_thread_is_handed_to_a_named_bot_with_no_pickup_race(self):
        # #468: a release with ?to= moves the claim at once; nobody else can take it in between
        self.assertEqual(self.claim(self.worker).status_code, 200)
        self.assertEqual(self.client.delete(self.endpoint, headers=self.other, params={"to": "helper"}).status_code, 403)
        self.assertEqual(self.client.delete(self.endpoint, headers=self.worker, params={"to": "nobody"}).status_code, 404)
        # a target that could not claim it is refused, and the worker keeps it
        r = self.client.delete(self.endpoint, headers=self.worker, params={"to": "operator"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.client.get(self.endpoint).json()["worker"], "worker")
        r = self.client.delete(self.endpoint, headers=self.worker, params={"to": "@helper"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["worker"], "helper")
        self.assertEqual(self.claim(self.other).status_code, 409)
        last = self.client.get(f'/api/threads/{self.root["id"]}').json()["messages"][-1]
        self.assertEqual(last["body"], "Thread handed to @helper by @worker.")
        self.assertEqual(last["meta"], {"kind": "thread_handed", "to": "helper"})
        # the new worker gets the thread's replies; the old one does not. It has been seen: a
        # human reply frees a thread whose worker is away (progress.rescue), handed or claimed
        self.client.get("/api/me", headers=self.helper)
        followup = self.post(self.human, "Over to you.", self.root["id"])
        self.assertIn(followup["id"], self.inbox(self.helper))
        self.assertNotIn(followup["id"], self.inbox(self.worker))
        # the author may hand a thread nobody holds
        self.assertEqual(self.client.delete(self.endpoint, headers=self.helper).status_code, 200)
        r = self.client.delete(self.endpoint, headers=self.human, params={"to": "other"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["worker"], "other")

    def test_pickup_indicator_tracks_authoritative_worker_without_posting(self):
        rid = self.root["id"]

        def snapshot(worker):
            response = self.client.get("/api/thread-workers", params={"thread": rid})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["workers"], {str(rid): worker})
            root = self.client.get(f"/api/messages/{rid}").json()["message"]
            markup = web.message_html(root)
            if worker:
                self.assertEqual(markup.count("Picked up by"), 1)
                self.assertIn(f'href="/a/{worker}">@{worker}</a>', markup)
            else:
                self.assertNotIn("Picked up by", markup)
            for path in (f"/api/messages?channel=work&roots=true", f"/api/threads/{rid}"):
                self.assertEqual(self.client.get(path).json()["messages"][0]["worker"], worker)

        snapshot(None)
        self.assertEqual(self.claim(self.worker).status_code, 200)
        snapshot("worker")
        self.assertEqual(self.claim(self.worker).status_code, 200)
        self.assertEqual(self.claim(self.helper).status_code, 409)
        snapshot("worker")
        self.assertEqual(len(self.client.get(f"/api/threads/{rid}").json()["messages"]), 1)
        db.init()
        snapshot("worker")
        reply = self.post(self.worker, "Answered.", rid)
        self.assertNotIn("Picked up by", web.message_html(reply))
        snapshot("worker")
        self.assertEqual(self.client.delete(self.endpoint, headers=self.worker).status_code, 200)
        snapshot(None)
        self.assertEqual(self.claim(self.helper).status_code, 200)
        snapshot("helper")

    def test_snapshot_says_when_a_worker_is_not_running(self):
        rid = self.root["id"]
        self.assertEqual(self.claim(self.worker).status_code, 200)
        keeper = self.register("keeper-box", "keeper")

        def down(status=None):
            if status:
                r = self.client.put("/api/fleet/hosts/box", headers=keeper,
                                    json={"bots": [{"handle": "worker", "harness": "claude", "status": status}]})
                self.assertEqual(r.status_code, 200, r.text)
            return self.client.get("/api/thread-workers", params={"thread": rid}).json()["down"]

        self.assertEqual(down(), {})                      # not in the fleet, seen just now
        self.assertEqual(down("waiting"), {})
        self.assertEqual(down("stopped"), {"worker": "stopped"})
        self.assertEqual(down("running bot-board"), {})
        # a bot the fleet doesn't know, unseen for over an hour: away
        db.conn().execute("DELETE FROM fleet_hosts")
        db.conn().execute("UPDATE agents SET last_seen_at='2026-01-01T00:00:00+00:00'")
        db.conn().commit()
        self.assertEqual(down(), {"worker": "away"})

    def test_worker_snapshot_is_batched_bounded_and_obeys_read_auth(self):
        rid = self.root["id"]
        reply = self.post(self.human, "More context.", rid)
        other_root = self.post(self.human, "Another request.")
        self.assertEqual(self.claim(self.worker).status_code, 200)
        response = self.client.get("/api/thread-workers", params=[
            ("thread", rid), ("thread", rid), ("thread", other_root["id"]),
            ("thread", reply["id"]), ("thread", 999999),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["workers"], {str(rid): "worker", str(other_root["id"]): None})
        for params in ([], [("thread", "bad")], [("thread", rid)] * 201):
            self.assertEqual(self.client.get("/api/thread-workers", params=params).status_code, 422)
        with patch.object(main, "READ_TOKEN_REQUIRED", True):
            self.assertEqual(self.client.get("/api/thread-workers", params={"thread": rid}).status_code, 401)
            self.assertEqual(self.client.get("/api/thread-workers", params={"thread": rid},
                                             headers=self.human).status_code, 200)

    def test_completion_is_durable_silent_and_blocks_two_watchers(self):
        self.claim(self.worker)
        reply = self.post(self.worker, "The PR is merged; nothing remains.", self.root["id"])
        endpoint = f'/api/threads/{self.root["id"]}/complete'
        done = self.client.post(endpoint, headers=self.worker, json={"through":reply["id"]})
        self.assertEqual(done.status_code, 200, done.text)
        for auth in (self.helper, self.other, self.worker):
            self.assertEqual(self.claim(auth).status_code, 409)
        thread = self.client.get(f'/api/threads/{self.root["id"]}').json()
        self.assertEqual(len(thread['messages']), 2)
        self.assertTrue(all(m['completed_through'] == reply['id'] for m in thread['messages']))
        compact = self.client.get('/api/messages', params={'channel':'work', 'view':'compact'}).json()
        self.assertEqual(compact['messages'][0]['completed_through'], reply['id'])
        # Releasing a finished claim is quiet, not an invitation to another watcher.
        self.assertEqual(self.client.delete(self.endpoint, headers=self.worker).status_code, 200)
        self.assertEqual(len(self.client.get(f'/api/threads/{self.root["id"]}').json()['messages']), 2)
        self.assertEqual(self.claim(self.helper).status_code, 409)
        # A genuine new request reopens the thread, even if its claim was removed.
        request = self.post(self.human, 'Please also add a regression test.', self.root['id'])
        self.assertTrue(request['meta']['thread_reopened'])
        self.assertIsNone(request['completed_through'])
        self.assertEqual(self.claim(self.helper).status_code, 200)

    def test_completion_rejects_unreviewed_work_and_unrelated_callers(self):
        self.claim(self.worker)
        endpoint = f'/api/threads/{self.root["id"]}/complete'
        reply = self.post(self.human, 'One more requirement', self.root['id'])
        self.assertEqual(self.client.post(endpoint, headers=self.worker, json={'through':self.root['id']}).status_code, 409)
        self.assertEqual(self.client.post(endpoint, headers=self.other, json={'through':reply['id']}).status_code, 403)
        self.assertEqual(self.client.post(endpoint, headers=self.worker, json={'through':999999}).status_code, 409)
        self.assertEqual(self.client.post(endpoint, json={'through':reply['id']}).status_code, 401)
        self.assertEqual(self.client.post(endpoint, headers=self.worker, json={'through':reply['id']}).status_code, 200)
        # A cross-channel reply is refused, so it cannot clear another channel’s completion.
        r = self.client.post('/api/messages', headers=self.worker,
                             json={'channel': 'elsewhere', 'body': 'old release notice', 'reply_to': self.root['id']})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.claim(self.helper).status_code, 409)
        newer = self.post(self.human, 'Actual follow-up', self.root['id'])
        self.assertTrue(newer['meta']['thread_reopened'])
        self.assertEqual(self.client.get(f'/api/messages/{reply["id"]}').json()['message']['completed_through'], None)
    def test_message_receipts_follow_delivery_not_thread_ownership(self):
        self.claim(self.worker)
        reply = self.post(self.human, "@helper please review", self.root["id"])
        ids = [self.root["id"], reply["id"]]
        def receipts():
            return self.client.get("/api/message-receipts", params=[("message", i) for i in ids]).json()["receipts"]
        self.assertEqual(receipts(), {str(i): [] for i in ids})
        endpoint = "/api/message-receipts"
        payload = {"messages": ids, "active": True}
        self.assertEqual(self.client.post(endpoint, headers=self.worker, json=payload).status_code, 200)
        self.assertTrue(receipts()[str(reply["id"])][0]["looking"])
        self.assertEqual(self.client.post(endpoint, headers=self.other, json=payload).status_code, 403)
        # A rejected batch rolls back the earlier valid receipt too.
        self.assertEqual(self.client.post(endpoint, headers=self.helper, json={"messages": [reply["id"], self.root["id"]]}).status_code, 403)
        self.assertEqual(len(receipts()[str(reply["id"])]), 1)
        self.assertEqual(self.client.post(endpoint, headers=self.helper, json={"messages": [reply["id"]]}).status_code, 200)
        self.client.post(endpoint, headers=self.worker, json={**payload, "active": False})
        rs = receipts()[str(reply["id"])]
        self.assertEqual({r["handle"]: r["looking"] for r in rs}, {"helper": True, "worker": False})
        with db.conn() as c:
            c.execute("UPDATE message_receipts SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertFalse(any(r["looking"] for r in receipts()[str(reply["id"])]))
        self.assertEqual(self.client.post(endpoint, headers=self.human, json=payload).status_code, 403)
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 401)
        with patch.object(main, "READ_TOKEN_REQUIRED", True):
            self.assertEqual(self.client.get(endpoint, params={"message": ids[0]}).status_code, 401)

    def test_pickup_handle_is_escaped_in_server_markup(self):
        markup = web.message_html({**self.root, "worker": 'bad"><script>'})
        self.assertIn('aria-hidden="true"', markup)
        self.assertIn('Picked up by <a href="/a/bad&quot;&gt;&lt;script&gt;">', markup)
        self.assertNotIn("<script>", markup)

    def test_claim_validation_and_targeted_roots(self):
        self.assertEqual(self.claim(self.human).status_code, 403)
        self.assertEqual(self.client.post("/api/threads/999999/worker", headers=self.worker).status_code, 404)
        reply = self.post(self.human, "Details.", self.root["id"])
        self.assertEqual(self.client.post(f'/api/threads/{reply["id"]}/worker',
                                         headers=self.worker).status_code, 409)
        targeted = self.post(self.human, "@helper please handle this.")
        path = f'/api/threads/{targeted["id"]}/worker'
        self.assertEqual(self.client.post(path, headers=self.worker).status_code, 403)
        self.assertEqual(self.client.post(path, headers=self.helper).status_code, 200)

    def test_conversation_membership_is_not_a_reply_subscription(self):
        conversation = self.client.post("/api/conversations", headers=self.worker,
                                       json={"participants": ["helper"]}).json()["conversation"]
        root = self.post(self.worker, "@helper, inspect this.", channel=conversation["slug"])
        path = f'/api/threads/{root["id"]}/worker'
        self.assertEqual(self.client.post(path, headers=self.other).status_code, 403)
        self.assertEqual(self.client.post(path, headers=self.helper).status_code, 200)
        self.post(self.helper, "Done.", root["id"], channel=conversation["slug"])
        self.assertEqual(self.inbox(self.worker), [])


if __name__ == "__main__":
    unittest.main()
