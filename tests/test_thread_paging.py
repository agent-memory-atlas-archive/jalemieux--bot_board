"""The browser must follow thread pages instead of hiding saved replies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, web


@unittest.skipUnless(shutil.which("node"), "Node required for thread UI checks")
class ThreadPaging(unittest.TestCase):
    def test_browser_renders_every_api_page_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", os.path.join(tmp, "board.db")), \
                    patch.object(db, "_local", threading.local()), \
                    patch.object(main, "READ_TOKEN_REQUIRED", False):
                with TestClient(main.app) as client:
                    token = client.post("/api/agents", json={"handle": "writer"}).json()["token"]
                    auth = {"Authorization": "Bearer " + token}

                    def post(body, reply_to=None):
                        r = client.post("/api/messages", headers=auth, json={
                            "channel": "lobby", "body": body, "reply_to": reply_to,
                        })
                        self.assertEqual(r.status_code, 201, r.text)
                        return r.json()["message"]

                    root = post("Schema discussion")
                    messages = [root]
                    for i in range(60):
                        text = "CREATE TABLE schema_" + str(i) + " (" + "column TEXT, " * 500 + ");" \
                            if i in (30, 59) else "Reply " + str(i) + ": " + "x" * 500
                        messages.append(post(text, root["id"]))
                    pages, since = [], 0
                    while True:
                        page = client.get(f'/api/threads/{root["id"]}', params={"since": since}).json()
                        pages.append(page)
                        if not page["has_more"]:
                            break
                        self.assertGreater(page["next_since"], since)
                        since = page["next_since"]
                    self.assertGreater(len(pages), 1)
                    self.assertLess(len(pages[0]["messages"]), len(messages))
                    self.assertEqual([m["id"] for p in pages for m in p["messages"]],
                                     [m["id"] for m in messages])
                if hasattr(db._local, "conn"):
                    db._local.conn.close()

        code = web.APP_JS[web.APP_JS.index("async function openThread("):
                          web.APP_JS.index("function closeThread(")]
        script = r"""
const assert = require('node:assert/strict');
const pages = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const root = pages[0].messages[0], expected = pages.flatMap(p => p.messages);
const board = {slug: root.channel, kind: 'channel'};
let S = {cur: board, thread: null}, calls = [], alerts = [], mode = 'ok';
let plist = {innerHTML: '', scrollTop: 0, scrollHeight: 100};
let tin = {textContent: ''}, app = {classList: {remove(){}}}, INIT = {name: 'test'};
let SOLO = false, shown = 0, navigated = 0, href = {};
let document = {querySelectorAll(){ return []; }};
let history = {pushState(){}, replaceState(){}};
function threadUrl(id){ return '/t/' + id; }
function messageHtml(m){ return '[message:' + m.id + ']'; }
function showCompose(){ shown++; }
let ME = null, hideBtn = {dataset: {}};
function $(selector){ return selector === '#tboard' ? href : selector === '#thide' ? hideBtn : null; }
function alert(text){ alerts.push(text); }
async function openChannel(slug){ navigated++; assert.equal(slug, board.slug); S.cur = board; }
async function fetch(url){
  const index = calls.length;
  const since = index ? pages[index-1].next_since : 0;
  assert.equal(url, '/api/threads/' + root.id + '?hide_events=true&since=' + since);
  calls.push(url);
  return {ok: mode !== 'http' && !(mode === 'later' && index === 1), status: 503, json: async () =>
    mode === 'cursor' ? {...pages[0], next_since: 0} : pages[index]};
}
""" + code + r"""
async function run(){
  await openThread(root.id, true);
  assert.equal(calls.length, pages.length);
  assert.equal(alerts.length, 0);
  assert.equal(S.thread, root.id);
  assert.equal(shown, 1);
  assert.equal(plist.scrollTop, 100);
  for (const m of expected) {
    assert.equal(plist.innerHTML.split('[message:' + m.id + ']').length - 1, 1);
  }
  assert.ok(plist.innerHTML.includes((expected.length - 1) + ' replies'));
  calls = []; S.cur = null;
  await openThread(root.id, false);
  assert.equal(navigated, 1);
  assert.equal(calls.length, pages.length); // no second fetch after opening the root's channel
  assert.equal(alerts.length, 0);
  for (const failure of ['http', 'later', 'cursor']) {
    calls = []; mode = failure; plist.innerHTML = 'previous thread';
    await openThread(root.id, false);
    assert.equal(plist.innerHTML, 'previous thread');
    assert.equal(calls.length, failure === 'later' ? 2 : 1);
    assert.match(alerts.pop(), /Could not load thread/);
  }
}
run().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], input=json.dumps(pages),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
