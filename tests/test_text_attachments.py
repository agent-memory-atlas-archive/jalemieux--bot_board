"""Text attachments stay out of feeds and are saved atomically with their message."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import db, main, web


class TextAttachments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "board.db"))
        self.local = patch.object(db, "_local", threading.local())
        self.reads = patch.object(main, "READ_TOKEN_REQUIRED", False)
        self.path.start()
        self.local.start()
        self.reads.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        r = self.client.post("/api/agents", json={"handle": "writer", "kind": "human"})
        self.auth = {"Authorization": "Bearer " + r.json()["token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        if hasattr(db._local, "conn"):
            db._local.conn.close()
        self.reads.stop()
        self.local.stop()
        self.path.stop()
        self.tmp.cleanup()

    def post(self, **kw):
        return self.client.post("/api/messages", headers=self.auth,
                                json={"channel": "lobby", "body": "Text attached.", **kw})

    def test_reply_preserves_original_text_and_small_feeds(self):
        reader = self.client.post("/api/agents", json={"handle": "reader"}).json()
        reader_auth = {"Authorization": "Bearer " + reader["token"]}
        root = self.post(body="Start a thread").json()["message"]
        text = " \r\n" + "Long text \u00e9 \U0001f600\n" * 2000 + "\n "
        r = self.post(body="@reader Text attached.", reply_to=root["id"], text_attachment=text)
        self.assertEqual(r.status_code, 201, r.text)
        message = r.json()["message"]
        self.assertEqual(message["thread_id"], root["id"])
        attachment = message["attachments"][0]
        self.assertEqual(attachment["size_bytes"], len(text.encode("utf-8")))
        self.assertEqual(attachment["name"], "pasted-text.txt")
        for path in ("/api/messages", "/api/inbox",
                     f'/api/threads/{root["id"]}', "/api/search"):
            for view in ("full", "compact"):
                with self.subTest(path=path, view=view):
                    params = {"view": view, **({"q": "attached"} if path == "/api/search" else {})}
                    page = self.client.get(path, params=params, headers=reader_auth)
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertLess(len(page.content), main.MAX_RESPONSE_BYTES)
                    reply = next(m for m in page.json()["messages"] if m["id"] == message["id"])
                    self.assertEqual(reply["attachments"], [attachment])
                    self.assertNotIn("Long text", page.text)
        download = self.client.get(attachment["url"])
        self.assertEqual(download.content, text.encode("utf-8"))
        self.assertEqual(download.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(download.headers["content-disposition"], 'attachment; filename="pasted-text.txt"')
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        self.assertIn("no-store", download.headers["cache-control"])
        # Reading the file back proves this is persisted, not an in-memory upload.
        self.assertEqual(Path(db.get_text_attachment(r.json()["message"]["id"])).read_bytes(), text.encode("utf-8"))
        search_page = self.client.get("/search", params={"q": "attached"}).text
        self.assertIn(f'href="{attachment["url"]}" data-attachment download', search_page)
        self.assertIn(web.ATTACHMENT_JS, search_page)

    def test_exact_ten_mib_limit_in_utf8(self):
        limit = main.MAX_TEXT_ATTACHMENT_BYTES
        for text in ("x" * limit, "\u00e9" * (limit // 2)):
            with self.subTest(bytes=len(text.encode("utf-8"))):
                r = self.post(text_attachment=text)
                self.assertEqual(r.status_code, 201, r.text[:200])
                self.assertEqual(r.json()["message"]["attachments"][0]["size_bytes"], limit)
                self.assertLess(len(r.content), 2000)
                r = self.post(text_attachment=text + "x")
                self.assertEqual(r.status_code, 422)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM text_attachments").fetchone()[0], 2)

    def test_normal_messages_and_body_validation_are_unchanged(self):
        message = self.post(body="  normal text  ").json()["message"]
        self.assertEqual(message["body"], "normal text")
        self.assertNotIn("attachments", message)
        self.assertEqual(self.post(body="x" * main.MAX_BODY).status_code, 201)
        r = self.post(body="x" * (main.MAX_BODY + 1))
        self.assertEqual(r.status_code, 422)
        self.assertIn("body exceeds", r.json()["detail"][0]["msg"])
        self.assertEqual(self.post(text_attachment=" \n ").status_code, 422)
        self.assertEqual(self.client.get(f'/api/messages/{message["id"]}/attachment').status_code, 404)
        self.assertEqual(self.client.get("/api/messages/999999/attachment").status_code, 404)

    def test_download_follows_private_read_policy(self):
        message = self.post(text_attachment="Private board text").json()["message"]
        url = message["attachments"][0]["url"]
        with patch.object(main, "READ_TOKEN_REQUIRED", True):
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers={"Authorization": "Bearer bad"}).status_code, 401)
            self.assertEqual(self.client.get(url, headers=self.auth).text, "Private board text")

    def test_failed_post_leaves_no_attachment_or_message(self):
        before = db.cursor()
        self.assertEqual(self.post(reply_to=999999, text_attachment="Not saved").status_code, 404)
        self.assertEqual(db.cursor(), before)
        db.conn().execute("""CREATE TRIGGER fail_attachment BEFORE INSERT ON text_attachments
                             BEGIN SELECT RAISE(ABORT, 'attachment write failed'); END""")
        db.conn().commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "attachment write failed"):
            self.post(text_attachment="Not saved either")
        self.assertEqual(db.cursor(), before)
        self.assertEqual(self.post(body="Next successful message").status_code, 201)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM text_attachments").fetchone()[0], 0)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_ui_receives_server_limits_and_protocol_documents_attachments(self):
        page = self.client.get("/").text
        self.assertIn(f'"max_body": {main.MAX_BODY}', page)
        self.assertIn(f'"max_text_attachment_bytes": {main.MAX_TEXT_ATTACHMENT_BYTES}', page)
        self.assertEqual(page.count('class="attachment-note"'), 2)
        self.assertIn("text_attachment", self.client.get("/agents.md").text)


@unittest.skipUnless(shutil.which("node"), "Node required for composer checks")
class Composer(unittest.TestCase):
    def test_actual_composer_submission_and_download(self):
        helpers = web.APP_JS[web.APP_JS.index("function composePayload("):
                             web.APP_JS.index("function wireCompose(")]
        wire = web.APP_JS[web.APP_JS.index("function wireCompose("):
                          web.APP_JS.index("wireCompose($('#compose')")]
        script = r"""
const assert = require('node:assert/strict');
const INIT = {max_body: 16000, max_text_attachment_bytes: 10 * 1024 * 1024};
let ME = {token: 'test-token'}, response = {ok: true, status: 201, data: {}}, sent;
let S = {agents: {}, cursor: 0}, requests = 0, clicked = false, clickHandler;
const localStorage = {getItem(){ return JSON.stringify(ME); }};
function element(){ return {value: '', style: {}, textContent: '', hidden: true,
  disabled: false, handlers: {}, addEventListener(k, f){ this.handlers[k] = f; }}; }
const document = {addEventListener(k, f){ clickHandler = f; },
  createElement(){ return {click(){ clicked = true; }, remove(){}}; }, body: {appendChild(){}}};
function $(key, form){ return form.elements[key]; }
function grow(){} function openSignin(){} function onMessage(){}
function alert(message){ throw Error(message); }
async function fetch(url, options){
  sent = {url, options}; requests++;
  return {ok: response.ok, status: response.status, json: async () => response.data,
          blob: async () => new Blob(['downloaded'])};
}
""" + helpers + wire + web.ATTACHMENT_JS + r"""
async function flush(){ for(let i=0; i<12; i++) await Promise.resolve(); }
async function run(){
  assert.deepEqual(composePayload('  short  '), {body: 'short'});
  assert.equal(composePayload('x'.repeat(16000)).text_attachment, undefined);
  assert.equal(composePayload('\u{1f600}'.repeat(16000)).text_attachment, undefined);
  const original = ' \r\n' + 'x'.repeat(16001) + '\n ';
  assert.equal(composePayload(original).text_attachment, original);
  assert.equal(composePayload('x'.repeat(INIT.max_text_attachment_bytes)).text_attachment.length,
               INIT.max_text_attachment_bytes);
  assert.throws(() => composePayload('\u00e9'.repeat(INIT.max_text_attachment_bytes/2) + 'x'), /10 MiB/);
  assert.equal(postError([{msg: 'body exceeds 16000 characters'}], 422), 'body exceeds 16000 characters');
  assert.equal(postError('archived', 403), 'archived');
  assert.equal(postError(null, 500), 'post failed (500)');
  for (const reply of [null, 42]) {
    const form = element();
    form.elements = Object.fromEntries(['textarea', '.send', '.err', '.mentions', '.signin', '.attachment-note']
      .map(k => [k, element()]));
    wireCompose(form, () => ({channel: 'lobby', reply_to: reply}));
    const ta = form.elements.textarea, err = form.elements['.err'], send = form.elements['.send'];
    ta.value = original;
    ta.handlers.input();
    assert.match(form.elements['.attachment-note'].textContent, /Will post as pasted-text.txt/);
    response = {ok: false, status: 422, data: {detail: [{msg: 'specific validation error'}]}};
    form.handlers.submit({preventDefault(){}});
    assert.equal(send.disabled, true);
    await flush();
    assert.equal(ta.value, original);
    assert.equal(err.textContent, 'specific validation error');
    assert.equal(send.disabled, false);
    const payload = JSON.parse(sent.options.body);
    assert.equal(payload.text_attachment, original);
    assert.equal(payload.reply_to, reply);
    assert.equal(payload.channel, 'lobby');
    assert.ok(payload.body.length < INIT.max_body);
    ta.value = 'x'.repeat(INIT.max_text_attachment_bytes + 1);
    const before = requests;
    form.handlers.submit({preventDefault(){}});
    assert.equal(requests, before);
    assert.match(err.textContent, /10 MiB/);
    ta.value = original;
    response = {ok: true, status: 201, data: {}};
    form.handlers.submit({preventDefault(){}});
    await flush();
    assert.equal(ta.value, '');
    assert.equal(form.elements['.attachment-note'].textContent, '');
  }
  clickHandler({target: {closest(){ return {getAttribute(){ return '/api/messages/42/attachment'; }}; }},
                preventDefault(){}});
  await flush();
  assert.equal(sent.options.headers.Authorization, 'Bearer test-token');
  assert.equal(clicked, true);
}
run().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
